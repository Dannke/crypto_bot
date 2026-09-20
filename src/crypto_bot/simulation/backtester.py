"""Replay-based backtester.

Walks historical candle data bar-by-bar through the *same* decision pipeline
that the live orchestrator uses — no copied logic, no shortcuts.

Supports multi-symbol multi-timeframe backtesting with a unified clock that
ticks on *every* bar close across *all* configured timeframes — the same
shared-capital regime as the live SignalExecutor.

Two decision layers run through the SAME replay loop, selected with the
``strategy_mode`` argument::

    Backtester
        ├── candidate  (default) — DecisionPipeline + SignalExecutor
        └── portfolio  — PortfolioDecisionPipeline + risk engine + PortfolioExecutor

Usage (from CLI)::

    crypto-bot backtest BTC/USDT 1h --start 2025-01-01 --end 2025-02-01
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

from ..config.env import Config
from ..config.schemas import RegimeConfig
from ..core.enums import Mode, Side, Signal, StrategyType
from ..core.logging_setup import get_logger
from ..core.policy import timeframe_to_seconds
from ..core.types import Candle, DecisionRecord, FeatureSet
from ..data.funding import FundingRepository, HistoricalFundingSource
from ..data.instruments import InstrumentCache, build_instrument_cache
from ..features.batch import aligned_correlation, closes_by_timestamp
from ..features.builder import FeatureBuilder, builder_from_settings
from ..features.context import SymbolMarketContext
from ..pipeline.factory import (
    build_decision_pipeline,
    build_portfolio_decision_pipeline,
    build_portfolio_risk_engine,
    build_portfolio_strategy,
    build_strategy_manager,
    get_active_strategy,
)
from ..portfolio import (
    CrossSectionalFeatureSnapshot,
    MarketSnapshot,
    PortfolioRiskEngine,
    PortfolioRiskLimits,
    PortfolioRiskReport,
    PortfolioState,
    UniverseSnapshot,
    VolatilitySizingParams,
)
from ..simulation.executor import SignalExecutor
from ..simulation.pnl import PnLSummary, PnLTracker
from ..simulation.portfolio_executor import PortfolioExecutor
from ..storage.db import Database, Repositories
from ..strategy.base import PortfolioStrategy
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
    strategy_mode: str = StrategyType.CANDIDATE.value
    intrabar_note: str = "Intrabar SL/TP resolution: pessimistic (default: SL wins on conflict)"


class Backtester:
    """Replay backtester — drives the decision pipeline over historical data.

    Supports multiple symbols and timeframes simultaneously, with real
    cross-asset correlation features for BTC and ETH.

    Design principle: every component (``FeatureBuilder``, decision pipeline,
    executor) is the *exact same instance* used in the live loop.  The only
    difference is the data source and the clock.  ``strategy_mode`` selects
    which decision layer is replayed; the loop, clock, SL/TP resolution and
    equity recording are shared between both modes.
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
        strategy_mode: StrategyType | str = StrategyType.CANDIDATE,
        portfolio_limits: PortfolioRiskLimits | None = None,
        regime_config: RegimeConfig | None = None,
    ) -> None:
        self._config = config
        self._symbols = [symbols] if isinstance(symbols, str) else symbols
        self._timeframes = [timeframes] if isinstance(timeframes, str) else timeframes
        self._start_ms = start_ms
        self._end_ms = end_ms
        self._conflict = conflict_resolution
        self._market_map = market_map or {}
        try:
            self._mode = StrategyType(strategy_mode)
        except ValueError as exc:
            raise ValueError(
                f"strategy_mode must be {StrategyType.CANDIDATE.value!r} or "
                f"{StrategyType.PORTFOLIO.value!r}, got {strategy_mode!r}"
            ) from exc
        self._manifest = BacktestManifest(
            symbols=self._symbols,
            timeframes=self._timeframes,
            start_ms=start_ms,
            end_ms=end_ms,
            conflict_resolution=conflict_resolution,
            strategy_mode=self._mode.value,
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
        self._source = source or HistoricalCandleSource(self._repos.candles)
        # Funding source for accruing funding payments on positions
        self._funding_source = HistoricalFundingSource(FundingRepository(self._db))
        # R0.4: Instrument cache for qty rounding and minNotional checks (built lazily)
        self._instrument_cache: InstrumentCache | None = None
        # R4: Regime config for portfolio fusion
        self._regime_config = regime_config

        if self._mode == StrategyType.CANDIDATE:
            self._pipeline = build_decision_pipeline(settings, last_trade_time=last_trade_time)
            self._strategy_mgr = build_strategy_manager(settings, strategy_name="per_timeframe")
            self._strategy = get_active_strategy(self._strategy_mgr)
            self._executor = SignalExecutor(
                config, self._repos, PnLTracker(),
            )
        else:
            self._portfolio_pipeline = build_portfolio_decision_pipeline(settings, regime_config=regime_config)
            self._portfolio_strategy = build_portfolio_strategy(settings)
            self._risk_engine: PortfolioRiskEngine = (
                build_portfolio_risk_engine(settings)
                if portfolio_limits is None
                else PortfolioRiskEngine(portfolio_limits)
            )
            self._volatility_sizing = (
                VolatilitySizingParams() if settings.portfolio.volatility_sizing else None
            )
            # Instrument cache will be set in run_async before use
            self._executor = PortfolioExecutor(
                config, self._repos, PnLTracker(), instrument_cache=None
            )
            # Market-snapshot strategies (e.g. cross_sectional_momentum_v0)
            # evaluate closed bars via ``evaluate_market`` and rebalance on a
            # cadence; feature strategies keep the tick-by-tick evaluation.
            self._market_strategy = (
                type(self._portfolio_strategy).evaluate_market
                is not PortfolioStrategy.evaluate_market
            )
            csm = getattr(settings.portfolio, "csm", None)
            self._rebalance_ms = (
                (csm.rebalance_hours if csm is not None else 24) * 3_600_000
                if self._market_strategy
                else 0
            )
            self._last_rebalance_ms: int | None = None

            # Validate timeframe consistency for market-snapshot strategies
            if self._market_strategy and csm is not None and csm.timeframe not in self._timeframes:
                raise ValueError(
                    f"csm.timeframe ({csm.timeframe!r}) must be one of the "
                    f"backtest timeframes {self._timeframes!r}"
                )

    @property
    def strategy_mode(self) -> StrategyType:
        """The decision layer this backtest replays."""
        return self._mode

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
            drawdown_pct=0.0, mode=Mode.PAPER, ts_ms=as_of,
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

            # R0.4: Build instrument cache for portfolio mode
            if self._mode == StrategyType.PORTFOLIO:
                self._instrument_cache = await build_instrument_cache(self._config)
                # Set it on the executor
                self._executor._instrument_cache = self._instrument_cache

            # Load all required candle data. Reference symbols (BTC/ETH) may
            # not be available — that's fine, correlations default to 0.
            for sym in self._all_symbols:
                for tf in self._timeframes:
                    if not self._source.is_loaded(sym, tf):
                        try:
                            await self._source.load_all_async(sym, tf)
                        except (AssertionError, Exception) as exc:
                            logger.info("bt: cannot load %s %s — %s", sym, tf, exc)

            # Load funding data for all test symbols
            for sym in self._symbols:
                try:
                    self._funding_source.load_from_repo(sym, self._start_ms, self._end_ms)
                except Exception as exc:
                    logger.info("bt: cannot load funding for %s — %s", sym, exc)

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
                        if self._mode == StrategyType.PORTFOLIO and self._market_strategy:
                            pass  # market strategies rebalance without features
                        else:
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

                # Post-only: заявки, размещённые на прошлых барах, получают шанс
                # исполниться на текущем — до того, как будет принято новое
                # решение. Без этого вызова они висят вечно, и стратегия с
                # entry_execution=post_only не совершает ни одной сделки.
                self._process_post_only(as_of)

                # Pipeline — skip entirely if halted (flag already set)
                if not self._emergency_halt_triggered:
                    try:
                        if self._mode == StrategyType.CANDIDATE:
                            self._run_candidate_tick(features_map, as_of)
                        else:
                            self._run_portfolio_tick(features_map, symbol_candles, as_of)
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

                # Accrue funding for all open positions
                self._executor.accrue_funding(self._funding_source, as_of)

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

    def _run_candidate_tick(self, features_map: dict[str, dict[str, FeatureSet]], as_of: int) -> None:
        """Replay one tick through the candidate decision pipeline (unchanged)."""
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

    def _run_portfolio_tick(
        self,
        features_map: dict[str, dict[str, FeatureSet]],
        symbol_candles: dict[str, dict[str, list]],
        as_of: int,
    ) -> None:
        """Replay one tick through the portfolio decision layer.

        Flow per tick: UniverseSnapshot -> PortfolioDecisionPipeline (features
        + strategy intent) -> PortfolioRiskEngine (limits + optional volatility
        sizing) -> PortfolioExecutor (weight-sized positions, DB first).

        Market-snapshot strategies (``evaluate_market``) run on a rebalance
        cadence over a closed-bar MarketSnapshot instead of features; on
        non-rebalance ticks their pipeline is skipped entirely and positions
        are only held, SL/TP-checked and marked to market.  On rebalance
        ticks positions that left the target book (or flipped side) are
        closed before the new intents are opened.
        """
        fastest_tf = min(self._timeframes, key=lambda tf: timeframe_to_seconds(tf))
        tracker = self._executor.tracker
        equity = tracker.current_equity
        universe = UniverseSnapshot(
            as_of_ms=as_of,
            symbols=tuple(self._symbols),
            quote_currency=self._quote,
        )
        state = PortfolioState(
            as_of_ms=as_of,
            mode=Mode.PAPER,
            equity=equity,
            cash=equity,
        )

        if self._market_strategy:
            if not self._rebalance_due(as_of):
                return
            snapshot = self._market_snapshot(symbol_candles, fastest_tf, as_of)
            if snapshot is None:
                return
            intent = self._portfolio_pipeline.process_market(
                snapshot, state, self._portfolio_strategy,
                historical_candles=symbol_candles,
            )
            cross_section = None
        else:
            intent = self._portfolio_pipeline.process(
                universe,
                symbol_candles,
                self._market_map,
                state,
                self._portfolio_strategy,
                trigger_tf=fastest_tf,
            )
            cross_section = None
            primary_tf = fastest_tf if fastest_tf in self._timeframes else self._timeframes[0]
            if features_map:
                snapshot_features = {
                    sym: by_tf[primary_tf]
                    for sym, by_tf in features_map.items()
                    if primary_tf in by_tf
                }
                if snapshot_features:
                    cross_section = CrossSectionalFeatureSnapshot(
                        as_of_ms=as_of,
                        universe=universe,
                        features_by_symbol=snapshot_features,
                    )

        report = self._risk_engine.evaluate(
            intent, state, cross_section, sizing=self._volatility_sizing
        )

        self._journal_portfolio_report(report, as_of, fastest_tf)

        if self._market_strategy:
            self._rebalance_positions(report, as_of)

        for position_intent in report.adjusted_intent.intents:
            tf = position_intent.timeframe or fastest_tf
            feature = features_map.get(position_intent.symbol, {}).get(tf)
            if self._market_strategy:
                bars = symbol_candles.get(position_intent.symbol, {}).get(tf)
                if not bars:
                    logger.info(
                        "bt: portfolio skip %s ts=%d — no bars on %s",
                        position_intent.symbol, as_of, tf,
                    )
                    continue
                bar = bars[-1]
                entry = bar.close
                if entry <= 0:
                    continue
                atr_pct = (bar.high - bar.low) / bar.low * 100.0 if bar.low > 0 else 1.0
                spread_pct = 0.0
            elif feature is not None:
                entry = feature.close if feature.close > 0 else feature.ema_fast
                atr_pct = feature.atr_pct
                spread_pct = feature.spread_pct
            else:
                logger.info(
                    "bt: portfolio skip %s ts=%d — no features on %s",
                    position_intent.symbol, as_of, tf,
                )
                continue
            self._executor.open_position(
                position_intent,
                entry_price=entry,
                atr_pct=atr_pct,
                spread_pct=spread_pct,
                timestamp_ms=as_of,
            )

        logger.info(
            "bt: portfolio ts=%d intents=%d accepted=%d rejected=%d gross=%.4f net=%.4f",
            as_of,
            len(intent.intents),
            sum(1 for r in report.position_results if r.accepted),
            sum(1 for r in report.position_results if not r.accepted),
            report.gross_exposure,
            report.net_exposure,
        )

    def _process_post_only(self, as_of: int) -> None:
        """Дать висящим post-only заявкам шанс исполниться на текущем баре.

        Заявка выставляется по цене закрытия своего бара, поэтому исполниться
        она может только на следующем. Входы исполняются по лимиту (maker),
        выходы по истечении таймаута уходят в рынок по цене закрытия бара.
        """
        if self._mode != StrategyType.PORTFOLIO:
            return
        for symbol, tf in self._executor.pending_post_only:
            if not self._source.is_loaded(symbol, tf):
                continue
            bar = self._source.slice(as_of, symbol, tf)
            if not bar:
                continue
            last = bar[-1]
            self._executor.process_post_only_entries(
                symbol, tf, last.low, last.high, as_of,
            )
            self._executor.process_post_only_exits(
                symbol, tf, last.low, last.high, as_of, close=last.close,
            )

    def _rebalance_due(self, as_of: int) -> bool:
        """True when the market strategy must re-evaluate at ``as_of``."""
        if self._last_rebalance_ms is None or as_of - self._last_rebalance_ms >= self._rebalance_ms:
            self._last_rebalance_ms = as_of
            return True
        return False

    def _market_snapshot(
        self,
        symbol_candles: dict[str, dict[str, list]],
        fastest_tf: str,
        as_of: int,
    ) -> MarketSnapshot | None:
        """Closed-bar cross-sectional snapshot for symbols with bars at ``as_of``."""
        candles_by_symbol: dict[str, tuple[Candle, ...]] = {}
        for sym in self._symbols:
            bars = symbol_candles.get(sym, {}).get(fastest_tf)
            if bars:
                candles_by_symbol[sym] = tuple(bars)
        if not candles_by_symbol:
            return None
        return MarketSnapshot(
            as_of_ms=as_of,
            timeframe=fastest_tf,
            candles_by_symbol=candles_by_symbol,
        )

    def _rebalance_positions(self, report: PortfolioRiskReport, as_of: int) -> None:
        """Close open positions that left the target book (dropped or flipped).

        Positions whose (symbol, side) is still in the risk-adjusted intent
        are held untouched; everything else is closed at the latest close
        with fees, and the new intents are opened right after.
        
        Exit reasons are taken from the strategy's PortfolioIntent.closes field.
        """
        desired = {
            (pi.symbol, pi.side) for pi in report.adjusted_intent.intents
        }
        
        # Build exit reason map from strategy's closes
        exit_reasons: dict[tuple[str, str], str] = {}
        for symbol, timeframe, reason in report.adjusted_intent.closes:
            exit_reasons[(symbol, timeframe)] = reason

        for paper_pos in list(self._executor.tracker.positions):
            if not paper_pos.is_open:
                continue
            if (paper_pos.symbol, paper_pos.side) in desired:
                continue
            bar = self._source.slice(as_of, paper_pos.symbol, paper_pos.timeframe)
            exit_price = bar[-1].close if bar else paper_pos.entry_price
            # Use strategy-provided exit reason, fallback to "rebalance"
            reason = exit_reasons.get((paper_pos.symbol, paper_pos.timeframe), "rebalance")
            stats = self._executor.close_position_for_symbol(
                paper_pos.symbol,
                paper_pos.timeframe,
                reason=reason,
                exit_price=exit_price,
                closed_at_ms=as_of,
            )
            if stats["closed"]:
                logger.info(
                    "bt: close %s %s (%s) @ %.4f pnl=%.4f",
                    paper_pos.symbol, paper_pos.timeframe, reason, exit_price, stats["closed_pnl"],
                )

    def _journal_portfolio_report(
        self,
        report: PortfolioRiskReport,
        as_of: int,
        fallback_tf: str,
    ) -> None:
        """Journal accepted/rejected portfolio positions with their reasons."""
        records: list[DecisionRecord] = []
        for result in report.position_results:
            signal = (
                Signal.BUY if result.side == Side.LONG else Signal.SELL
            ) if result.accepted else None
            detail = (
                "portfolio position accepted"
                if result.accepted
                else f"portfolio:{result.reason.value if result.reason else 'rejected'}"
            )
            records.append(
                DecisionRecord(
                    timestamp=datetime.fromtimestamp(as_of / 1000, tz=UTC),
                    symbol=result.symbol,
                    timeframe=result.timeframe or fallback_tf,
                    accepted=result.accepted,
                    reason=None,
                    detail=detail,
                    signal=signal,
                    outcome="position_opened" if result.accepted else "no_position",
                )
            )
        if records:
            self._repos.decisions.insert_many(records)

    @property
    def manifest(self) -> BacktestManifest:
        return self._manifest

    @property
    def emergency_halt_triggered(self) -> bool:
        return self._emergency_halt_triggered
