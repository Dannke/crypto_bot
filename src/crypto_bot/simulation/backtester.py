"""Replay-based backtester.

Walks historical candle data bar-by-bar through the *same* decision pipeline
that the live orchestrator uses — no copied logic, no shortcuts.

Supports multi-symbol multi-timeframe backtesting with a unified clock that
ticks on *every* bar close across *all* configured timeframes — the same
shared-capital regime as the live SignalExecutor.

Usage (from CLI)::

    crypto-bot backtest BTC/USDT 1h --start 2025-01-01 --end 2025-02-01
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

from ..config.env import Config
from ..core.enums import Mode
from ..core.logging_setup import get_logger
from ..core.policy import timeframe_to_seconds
from ..core.types import FeatureSet
from ..features.batch import aligned_correlation, closes_by_timestamp
from ..features.builder import FeatureBuilder, builder_from_settings
from ..features.context import SymbolMarketContext
from ..pipeline.factory import (
    build_decision_pipeline,
    build_strategy_manager,
    get_active_strategy,
)
from ..simulation.executor import SignalExecutor
from ..simulation.pnl import PnLSummary, PnLTracker
from ..storage.db import Database, Repositories
from .historical_source import HistoricalCandleSource

logger = get_logger(__name__)


@dataclass(slots=True)
class BacktestManifest:
    """Provenance metadata emitted alongside every backtest run."""

    symbols: list[str]
    timeframes: list[str]
    start_ms: int
    end_ms: int
    conflict_resolution: str
    intrabar_note: str = "Intrabar SL/TP resolution: pessimistic (default: SL wins on conflict)"


class Backtester:
    """Replay backtester — drives the decision pipeline over historical data.

    Supports multiple symbols and timeframes simultaneously, with real
    cross-asset correlation features for BTC and ETH.

    Design principle: every component (``FeatureBuilder``, ``DecisionPipeline``,
    ``SignalExecutor``) is the *exact same instance* used in the live loop.
    The only difference is the data source and the clock.
    """

    def __init__(
        self,
        config: Config,
        symbols: str | list[str],
        timeframes: str | list[str],
        start_ms: int,
        end_ms: int,
        *,
        conflict_resolution: str = "pessimistic",
        db: Database | None = None,
        source: HistoricalCandleSource | None = None,
        last_trade_time: dict[str, datetime] | None = None,
        market_map: dict[str, SymbolMarketContext] | None = None,
    ) -> None:
        self._config = config
        self._symbols = [symbols] if isinstance(symbols, str) else symbols
        self._timeframes = [timeframes] if isinstance(timeframes, str) else timeframes
        self._start_ms = start_ms
        self._end_ms = end_ms
        self._conflict = conflict_resolution
        self._market_map = market_map or {}
        self._manifest = BacktestManifest(
            symbols=self._symbols,
            timeframes=self._timeframes,
            start_ms=start_ms,
            end_ms=end_ms,
            conflict_resolution=conflict_resolution,
            intrabar_note=(
                "Intrabar SL/TP resolution: pessimistic"
                if conflict_resolution == "pessimistic"
                else f"Intrabar SL/TP resolution: open_proximity ({conflict_resolution})"
            ),
        )

        # Emergency drawdown halt flag (sticky - once triggered, never re-triggers)
        self._emergency_halt_triggered = False

        settings = config.settings
        quote = settings.universe.quote.upper()

        # Build the full symbol set: test symbols + reference assets for correlations
        self._all_symbols = list(self._symbols)
        btc_sym = f"BTC/{quote}"
        eth_sym = f"ETH/{quote}"
        for ref_sym in [btc_sym, eth_sym]:
            if ref_sym not in self._all_symbols:
                self._all_symbols.append(ref_sym)
        self._btc_sym = btc_sym
        self._eth_sym = eth_sym
        self._quote = quote

        if db is None:
            raise ValueError(
                "Backtester requires an explicit db= argument. "
                "Refusing to silently fall back to settings.storage.db_path "
                "(the live bot's database). Pass e.g. "
                "db=Database(tmp_path / 'backtest_run.db')."
            )
        self._db = db
        self._repos = Repositories(self._db)
        self._builder: FeatureBuilder = builder_from_settings(settings)
        self._pipeline = build_decision_pipeline(settings, last_trade_time=last_trade_time)
        self._strategy_mgr = build_strategy_manager(settings, strategy_name="per_timeframe")
        self._strategy = get_active_strategy(self._strategy_mgr)
        self._executor = SignalExecutor(
            config, self._repos, PnLTracker(),
        )
        self._source = source or HistoricalCandleSource(self._repos.candles)

    def _build_unified_clock(
        self,
        reference_symbol: str,
        timeframes: list[str],
        start_ms: int,
        end_ms: int,
    ) -> list[tuple[int, list[str]]]:
        """Build a single clock from close timestamps of ALL timeframes.

        Each tick corresponds to the moment when at least one timeframe's bar
        closes.  The returned list contains ``(as_of_ms, [fired_timeframes])``
        pairs sorted by time, so the caller can build features only for those
        timeframes that actually produced a new bar on each tick.

        Bar boundaries are calendar-based and symbol-independent — a 1h bar
        closes at ``:00`` for every symbol.  A single reference symbol with
        continuous history is sufficient to compute the tick grid for all
        symbols.
        """
        ts_to_tfs: dict[int, set[str]] = {}
        for tf in timeframes:
            period_ms = timeframe_to_seconds(tf) * 1000
            candles = self._source.slice_between(start_ms, end_ms, reference_symbol, tf)
            for c in candles:
                close_ts = c.timestamp + period_ms
                ts_to_tfs.setdefault(close_ts, set()).add(tf)
        return sorted(
            (ts, sorted(tfs)) for ts, tfs in ts_to_tfs.items()
            if start_ms <= ts <= end_ms
        )

    def _record_equity(self, as_of: int) -> None:
        current_prices: dict[tuple[str, str], float] = {}
        for pos in self._executor.tracker.positions:
            if not pos.is_open:
                continue
            chk = self._source.slice(as_of, pos.symbol, pos.timeframe)
            if chk:
                current_prices[(pos.symbol, pos.timeframe)] = chk[-1].close
        tracker = self._executor.tracker
        realized_pnl = sum(p.pnl_abs or 0.0 for p in tracker.positions if p.is_closed)
        equity_now = tracker.initial_equity + realized_pnl + tracker.unrealized_pnl(current_prices)
        tracker.record_equity(
            datetime.fromtimestamp(as_of / 1000, tz=UTC), equity_now,
        )
        self._repos.equity.insert(
            currency=self._quote, equity=equity_now,
            drawdown_pct=None, mode=Mode.PAPER, ts_ms=as_of,
        )

    async def run_async(self) -> PnLSummary:
        """Execute the replay loop asynchronously."""
        try:
            logger.info(
                "bt: start symbols=%s timeframes=%s [%d .. %d] conflict=%s",
                self._symbols, self._timeframes,
                self._start_ms, self._end_ms, self._conflict,
            )

            # Clean stale positions for test symbols (not reference assets)
            for sym in self._symbols:
                self._repos.positions.delete_for_symbol(sym)

            # Load all required candle data. Reference symbols (BTC/ETH) may
            # not be available — that's fine, correlations default to 0.
            for sym in self._all_symbols:
                for tf in self._timeframes:
                    if not self._source.is_loaded(sym, tf):
                        try:
                            await self._source.load_all_async(sym, tf)
                        except (AssertionError, Exception) as exc:
                            logger.info("bt: cannot load %s %s — %s", sym, tf, exc)

            # Unified clock: ticks on every bar close across ALL timeframes
            unified_clock = self._build_unified_clock(
                self._symbols[0], self._timeframes,
                self._start_ms, self._end_ms,
            )
            fastest_tf = min(self._timeframes, key=lambda tf: timeframe_to_seconds(tf))

            bar_count = 0
            for as_of, fired_tfs in unified_clock:
                try:
                    # 1. Build features for ALL symbols (needed for correlation),
                    #    but only pass fired TFs into the pipeline
                    features_map: dict[str, dict[str, FeatureSet]] = {}
                    symbol_candles: dict[str, dict[str, list]] = {}

                    for sym in self._all_symbols:
                        sym_tf_candles: dict[str, list] = {}
                        for tf in self._timeframes:
                            candles = self._source.slice(as_of, sym, tf)
                            if len(candles) >= 2:
                                sym_tf_candles[tf] = candles
                        if sym_tf_candles:
                            symbol_candles[sym] = sym_tf_candles

                    # Correlation reference timeframe — use fastest loaded
                    tf_for_corr = fastest_tf
                    btc_closes = closes_by_timestamp(symbol_candles, self._btc_sym, tf_for_corr) if self._btc_sym in symbol_candles else {}
                    eth_closes = closes_by_timestamp(symbol_candles, self._eth_sym, tf_for_corr) if self._eth_sym in symbol_candles else {}

                    for sym in self._symbols:
                        if sym not in symbol_candles:
                            continue
                        # Only include timeframes that actually fired on this tick
                        fired_features = {
                            tf: symbol_candles[sym][tf]
                            for tf in fired_tfs if tf in symbol_candles[sym]
                        }
                        if not fired_features:
                            continue

                        sym_closes = closes_by_timestamp(symbol_candles, sym, tf_for_corr)
                        corr_btc = 1.0 if sym == self._btc_sym else aligned_correlation(sym_closes, btc_closes)
                        corr_eth = 1.0 if sym == self._eth_sym else aligned_correlation(sym_closes, eth_closes)
                        market = self._market_map.get(sym)

                        try:
                            features_map[sym] = self._builder.build_all(
                                sym,
                                fired_features,
                                market=market,
                                correlation_btc=corr_btc,
                                correlation_eth=corr_eth,
                            )
                        except Exception as exc:
                            logger.info("bt: skip sym=%s ts=%d — %s", sym, as_of, exc)

                    if not features_map:
                        self._record_equity(as_of)
                        bar_count += 1
                        continue
                except Exception as exc:
                    logger.info("bt: skip ts=%d — %s", as_of, exc)
                    bar_count += 1
                    continue

                # Emergency drawdown check FIRST — before pipeline (sticky halt)
                # Uses mark-to-market equity with THIS tick's bar close so we
                # catch intrabar equity drops BEFORE SL/TP fires at the same
                # timestamp.
                tracker = self._executor.tracker
                peak = tracker.peak_equity
                mtm_prices: dict[tuple[str, str], float] = {}
                for pos in tracker.positions:
                    if pos.is_open:
                        chk = self._source.slice(as_of, pos.symbol, pos.timeframe)
                        if chk:
                            mtm_prices[(pos.symbol, pos.timeframe)] = chk[-1].close
                realized_pnl = sum(p.pnl_abs or 0.0 for p in tracker.positions if p.is_closed)
                current = tracker.initial_equity + realized_pnl + tracker.unrealized_pnl(mtm_prices)
                if (not self._emergency_halt_triggered and peak > 0 and current < peak):
                    dd_pct = (peak - current) / peak * 100.0
                    if dd_pct >= self._config.settings.risk.emergency_drawdown_pct:
                        self._emergency_halt_triggered = True
                        self._executor.emergency_halt = True
                        logger.warning(
                            "bt: emergency drawdown %.2f%% >= %.2f%% — closing ALL positions (halt sticky)",
                            dd_pct, self._config.settings.risk.emergency_drawdown_pct,
                        )
                        current_prices: dict[str, dict[str, float]] = {}
                        for (sym, tf), price in mtm_prices.items():
                            current_prices.setdefault(sym, {})[tf] = price
                        self._executor.close_all_positions(
                            "emergency_drawdown", current_prices, closed_at_ms=as_of,
                        )

                # Pipeline — skip entirely if halted (flag already set)
                if not self._emergency_halt_triggered:
                    try:
                        result = self._pipeline.process(
                            features_map,
                            self._strategy,
                            per_timeframe=True,
                            as_of_ms=as_of,
                        )

                        selected = result.get("selected", [])
                        rejected = result.get("rejected", [])
                        processed = result.get("total_processed", 0)
                        reject_reasons = [
                            (r.reject_reason.value if r.reject_reason else "none",
                             r.rejected_by or "?",
                             r.explanation[:80] if r.explanation else "")
                            for r in rejected[:3]
                        ]
                        logger.info(
                            "bt: pipeline ts=%d processed=%d selected=%d rejected=%d reasons=%s",
                            as_of, processed, len(selected), len(rejected),
                            reject_reasons,
                        )

                        # current prices for unrealized P&L gate in executor
                        current_prices: dict[tuple[str, str], float] = {}
                        for pos in self._executor.tracker.positions:
                            if not pos.is_open:
                                continue
                            chk = self._source.slice(as_of, pos.symbol, pos.timeframe)
                            if chk:
                                current_prices[(pos.symbol, pos.timeframe)] = chk[-1].close

                        for report in selected:
                            self._executor.handle_selected(report, current_prices=current_prices)
                        self._executor.handle_rejected_many(rejected)
                    except Exception as exc:
                        logger.exception("bt: pipeline failed at ts=%d: %s", as_of, exc)

                # SL/TP for ALL open positions
                for pos in list(self._executor.tracker.positions):
                    if not pos.is_open:
                        continue
                    price_tf = fastest_tf if fastest_tf in self._timeframes else pos.timeframe
                    if not self._source.is_loaded(pos.symbol, price_tf):
                        price_tf = pos.timeframe
                    bar = self._source.slice(as_of, pos.symbol, price_tf)
                    if bar:
                        self._executor.check_positions_range(
                            pos.symbol, pos.timeframe,
                            bar[-1].low, bar[-1].high,
                            open_price=bar[-1].open,
                            conflict_resolution=self._conflict,
                            bar_timestamp_ms=as_of,
                        )

                # Record equity on every tick
                self._record_equity(as_of)
                bar_count += 1


            summary = self._executor.tracker.get_summary()
            closed_positions = [p for p in self._executor.tracker.positions if p.is_closed]
            logger.info(
                "bt: done — %d bars, %d trades (closed=%d open=%d), equity=%.2f",
                bar_count, summary.total_trades,
                len(closed_positions),
                sum(1 for p in self._executor.tracker.positions if p.is_open),
                self._executor.tracker.current_equity,
            )
            for pos in closed_positions:
                logger.info(
                    "  closed %s %s entry=%.2f exit=%.2f pnl_abs=%.2f closed_by=%s",
                    pos.symbol, pos.timeframe,
                    pos.entry_price, pos.exit_price or 0,
                    pos.pnl_abs or 0, pos.closed_by or "?",
                )
            return summary
        finally:
            self._db.close()

    def run(self) -> PnLSummary:
        """Synchronous convenience wrapper."""
        return asyncio.run(self.run_async())

    @property
    def manifest(self) -> BacktestManifest:
        return self._manifest

    @property
    def emergency_halt_triggered(self) -> bool:
        return self._emergency_halt_triggered
