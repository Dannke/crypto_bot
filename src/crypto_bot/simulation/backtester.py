"""Replay-based backtester.

Walks historical candle data bar-by-bar through the *same* decision pipeline
that the live orchestrator uses — no copied logic, no shortcuts.

Supports multi-symbol backtesting with real BTC/ETH correlation features.

Usage (from CLI)::

    crypto-bot backtest BTC/USDT 1h --start 2025-01-01 --end 2025-02-01
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime

from ..config.env import Config
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
from .backtest_clock import BacktestClock
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

        # Primary timeframe drives the clock
        self._trigger_tf = self._timeframes[0]

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

            # Clock is driven by the trigger timeframe
            trigger_candles = self._source.slice(self._end_ms, self._symbols[0], self._trigger_tf) if self._symbols else []
            period_ms = timeframe_to_seconds(self._trigger_tf) * 1000
            close_ts = [c.timestamp + period_ms for c in trigger_candles]
            clock = BacktestClock(timestamps=close_ts, index=0)
            clock.timestamps = [t for t in clock.timestamps if self._start_ms <= t <= self._end_ms]

            bar_count = 0
            for as_of in clock:
                try:
                    # Build features for all symbols
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

                    # Compute real correlation features from timestamp-aligned closes
                    tf_for_corr = self._trigger_tf
                    btc_closes = closes_by_timestamp(symbol_candles, self._btc_sym, tf_for_corr) if self._btc_sym in symbol_candles else {}
                    eth_closes = closes_by_timestamp(symbol_candles, self._eth_sym, tf_for_corr) if self._eth_sym in symbol_candles else {}

                    for sym in self._symbols:
                        if sym not in symbol_candles:
                            continue
                        sym_closes = closes_by_timestamp(symbol_candles, sym, tf_for_corr)
                        corr_btc = 1.0 if sym == self._btc_sym else aligned_correlation(sym_closes, btc_closes)
                        corr_eth = 1.0 if sym == self._eth_sym else aligned_correlation(sym_closes, eth_closes)
                        market = self._market_map.get(sym)

                        try:
                            features_map[sym] = self._builder.build_all(
                                sym,
                                symbol_candles[sym],
                                market=market,
                                correlation_btc=corr_btc,
                                correlation_eth=corr_eth,
                            )
                        except Exception as exc:
                            logger.info("bt: skip sym=%s ts=%d — %s", sym, as_of, exc)

                    if not features_map:
                        # No features for any symbol — still need to check SL/TP
                        first_sym = self._symbols[0]
                        trigger_bar = trigger_candles[bar_count] if bar_count < len(trigger_candles) else None
                        if trigger_bar:
                            self._executor.check_positions_range(
                                first_sym, self._trigger_tf,
                                trigger_bar.low, trigger_bar.high,
                                open_price=trigger_bar.open,
                                conflict_resolution=self._conflict,
                                bar_timestamp_ms=trigger_bar.timestamp,
                            )
                        bar_count += 1
                        continue
                except Exception as exc:
                    logger.info("bt: skip ts=%d — %s", as_of, exc)
                    bar_count += 1
                    continue

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

                    for report in selected:
                        self._executor.handle_selected(report)
                    for report in rejected:
                        self._executor.handle_rejected(report)
                except Exception as exc:
                    logger.exception("bt: pipeline failed at ts=%d: %s", as_of, exc)

                # Check SL/TP for all open positions (intrabar range)
                for pos in list(self._executor.tracker.positions):
                    if not pos.is_open:
                        continue
                    if pos.timeframe not in self._timeframes:
                        continue
                    chk_candles = self._source.slice(as_of, pos.symbol, pos.timeframe)
                    if not chk_candles:
                        continue
                    bar = chk_candles[-1]
                    self._executor.check_positions_range(
                        pos.symbol, pos.timeframe,
                        bar.low, bar.high,
                        open_price=bar.open,
                        conflict_resolution=self._conflict,
                        bar_timestamp_ms=bar.timestamp,
                    )
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
