"""Replay-based backtester.

Walks historical candle data bar-by-bar through the *same* decision pipeline
that the live orchestrator uses — no copied logic, no shortcuts.

Usage (from CLI)::

    crypto-bot backtest BTC/USDT 1h --start 2025-01-01 --end 2025-02-01
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from ..config.env import Config
from ..core.logging_setup import get_logger
from ..core.policy import timeframe_to_seconds
from ..core.types import FeatureSet
from ..features.builder import FeatureBuilder, builder_from_settings
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

    symbol: str
    timeframe: str
    start_ms: int
    end_ms: int
    conflict_resolution: str
    intrabar_note: str = "Intrabar SL/TP resolution: pessimistic (default: SL wins on conflict)"


class Backtester:
    """Replay backtester — drives the decision pipeline over historical data.

    Design principle: every component (``FeatureBuilder``, ``DecisionPipeline``,
    ``SignalExecutor``) is the *exact same instance* used in the live loop.
    The only difference is the data source and the clock.
    """

    def __init__(
        self,
        config: Config,
        symbol: str,
        timeframe: str,
        start_ms: int,
        end_ms: int,
        *,
        conflict_resolution: str = "pessimistic",
        db: Database | None = None,
        source: HistoricalCandleSource | None = None,
    ) -> None:
        self._config = config
        self._symbol = symbol
        self._timeframe = timeframe
        self._start_ms = start_ms
        self._end_ms = end_ms
        self._conflict = conflict_resolution
        self._manifest = BacktestManifest(
            symbol=symbol,
            timeframe=timeframe,
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
        self._db = db or Database(settings.storage.db_path)
        self._repos = Repositories(self._db)
        self._builder: FeatureBuilder = builder_from_settings(settings)
        self._pipeline = build_decision_pipeline(settings)
        self._strategy_mgr = build_strategy_manager(settings, strategy_name="per_timeframe")
        self._strategy = get_active_strategy(self._strategy_mgr)
        self._executor = SignalExecutor(
            config, self._repos, PnLTracker(),
        )
        self._source = source or HistoricalCandleSource(self._repos.candles)

    async def run_async(self) -> PnLSummary:
        """Execute the replay loop asynchronously."""
        logger.info(
            "bt: start %s %s [%d .. %d] conflict=%s",
            self._symbol, self._timeframe,
            self._start_ms, self._end_ms, self._conflict,
        )

        # Start with a clean slate — no stale positions for this symbol from prior runs
        self._repos.positions.delete_for_symbol(self._symbol)

        if not self._source.loaded:
            await self._source.load_all_async(self._symbol, self._timeframe)

        all_candles = self._source.slice(self._end_ms)
        period_ms = timeframe_to_seconds(self._timeframe) * 1000
        # Clock ticks at bar-close moments so that at tick N we have N+1
        # closed bars available — just like a live scan after the bar closes.
        close_ts = [c.timestamp + period_ms for c in all_candles]
        clock = BacktestClock(timestamps=close_ts, index=0)
        clock.timestamps = [t for t in clock.timestamps if self._start_ms <= t <= self._end_ms]

        bar_count = 0
        for as_of in clock:
            candles = self._source.slice(as_of)
            if len(candles) < 2:
                continue

            try:
                features_by_tf: dict[str, FeatureSet] = self._builder.build_all(
                    self._symbol,
                    {self._timeframe: candles},
                )
            except Exception as exc:
                logger.info("bt: skip ts=%d — %s", as_of, exc)
                bar = candles[-1]
                self._executor.check_positions_range(
                    self._symbol, self._timeframe,
                    bar.low, bar.high,
                    open_price=bar.open,
                    conflict_resolution=self._conflict,
                    bar_timestamp_ms=bar.timestamp,
                )
                bar_count += 1
                continue

            try:
                features_map: dict[str, dict[str, FeatureSet]] = {
                    self._symbol: features_by_tf,
                }
                result = self._pipeline.process(
                    features_map,
                    self._strategy,
                    per_timeframe=True,
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
                    "bt: pipeline ts=%d processed=%d selected=%d rejected=%d reasons=%s nslices=%d",
                    as_of, processed, len(selected), len(rejected),
                    reject_reasons, len(candles),
                )

                for report in selected:
                    self._executor.handle_selected(report)
                for report in rejected:
                    self._executor.handle_rejected(report)
            except Exception as exc:
                logger.exception("bt: pipeline failed at ts=%d: %s", as_of, exc)

            bar = candles[-1]
            self._executor.check_positions_range(
                self._symbol, self._timeframe,
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

    def run(self) -> PnLSummary:
        """Synchronous convenience wrapper."""
        return asyncio.run(self.run_async())

    @property
    def manifest(self) -> BacktestManifest:
        return self._manifest
