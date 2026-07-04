"""Main trading loop — Decision Intelligence Layer integration.

Orchestrates market data fetch, feature generation, decision pipeline,
journaling, and optional paper-trading simulation. No live orders in stage 3.
"""
from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

from .config.env import Config
from .core.enums import RejectReason
from .core.exceptions import OrchestratorError
from .core.logging_setup import get_logger
from .data.exchange import MarketDataClient
from .data.feed import Feed, resolve_symbols
from .decision.decision_report import DecisionReport
from .features.batch import build_features_batch
from .features.builder import builder_from_settings
from .features.context import market_context_from_ticker
from .pipeline.factory import (
    build_decision_pipeline,
    build_strategy_manager,
    get_active_strategy,
)
from .simulation.executor import SignalExecutor
from .simulation.pnl import PnLTracker
from .storage.db import Database, Repositories

logger = get_logger(__name__)


def _strategy_reject_reason(signal_reason: str) -> RejectReason:
    reason = signal_reason.lower()
    if "conflict" in reason:
        return RejectReason.CONFLICTING_TIMEFRAMES
    if "partial" in reason:
        return RejectReason.LOW_CONFIDENCE
    if "no directional" in reason or "missing timeframes" in reason:
        return RejectReason.NO_DIRECTION
    return RejectReason.LOW_SCORE


async def run_orchestrator(config: Config) -> None:
    """Main scan loop using the stage-3 decision pipeline."""
    settings = config.settings
    logger.info("Starting orchestrator in %s mode", settings.runtime.mode)
    strategy_name = getattr(settings.runtime, "strategy", "confluence")
    per_timeframe_mode = strategy_name == "per_timeframe"
    logger.info("Strategy: %s%s", strategy_name, " (per-timeframe)" if per_timeframe_mode else "")

    db = Database(settings.storage.db_path)
    repos = Repositories(db)
    feature_builder = builder_from_settings(settings)
    pipeline = build_decision_pipeline(settings)
    strategy_manager = build_strategy_manager(settings, strategy_name=strategy_name)
    strategy = get_active_strategy(strategy_manager)
    executor = SignalExecutor(config, repos, PnLTracker())

    quote = settings.universe.quote.upper()
    trigger_tf = settings.timeframes.primary[0]

    try:
        async with MarketDataClient(config) as client:
            feed = Feed(client, config, db)
            while True:
                cycle_start = time.time()
                logger.info("Starting scan cycle at %s", datetime.now(tz=UTC).isoformat())

                try:
                    logger.info("--- scan cycle starts ---")
                    symbols = await resolve_symbols(client, config, feed.exchange_available)
                    if not symbols:
                        logger.warning("Universe is empty; skipping cycle")
                        continue

                    if feed.exchange_available:
                        tickers = await client.fetch_tickers(symbols)
                        if tickers:
                            feed.exchange_available = True
                        else:
                            logger.warning("No tickers from exchange; using DB cache fallback")
                            feed.exchange_available = False
                    else:
                        tickers = {}

                    market_by_symbol = {
                        sym: market_context_from_ticker(tickers.get(sym, {}))
                        for sym in symbols
                    }

                    feeds = await feed.fetch_many(symbols)
                    symbol_candles = {f.symbol: f.by_timeframe for f in feeds}

                    features_by_symbol = build_features_batch(
                        symbol_candles,
                        feature_builder,
                        market_by_symbol,
                        quote=quote,
                        trigger_tf=trigger_tf,
                    )

                    result = pipeline.process(features_by_symbol, strategy, per_timeframe=per_timeframe_mode)
                    stats: dict[str, Any] = result["stats"]
                    logger.info(
                        "--- cycle: processed=%d selected=%d rejected=%d avg_score=%.1f",
                        result["total_processed"],
                        stats.get("selected_count", 0),
                        stats.get("rejected_count", 0),
                        stats.get("avg_score", 0.0),
                    )
                    reject_reasons = stats.get("reject_reasons") or {}
                    if reject_reasons:
                        logger.info("--- reject reasons: %s", reject_reasons)

                    for report in result["selected"]:
                        exec_result = executor.handle_selected(report)
                        tf = report.features.get("timeframe", "?")
                        logger.info(
                            ">>> %s %s tf=%s score=%.1f conf=%.2f — %s",
                            report.signal.value,
                            report.symbol,
                            tf,
                            report.total_score,
                            report.confidence,
                            exec_result.message,
                        )

                    for report in result["rejected"]:
                        executor.handle_rejected(_normalize_rejected(report))

                except Exception as exc:  # noqa: BLE001
                    logger.error("Cycle error: %s", exc, exc_info=True)

                elapsed = time.time() - cycle_start
                sleep_time = max(0.0, settings.runtime.loop_interval_seconds - elapsed)
                await asyncio.sleep(sleep_time)

    except asyncio.CancelledError:
        logger.info("Orchestrator stopped gracefully")
    except Exception as exc:
        logger.critical("Orchestrator crashed", exc_info=True)
        raise OrchestratorError("Orchestrator failure") from exc
    finally:
        db.close()


def _normalize_rejected(report: DecisionReport) -> DecisionReport:
    """Ensure rejected reports carry a proper reject reason for journaling."""
    if report.reject_reason is not None:
        return report
    if report.explanation:
        from dataclasses import replace

        return replace(
            report,
            reject_reason=_strategy_reject_reason(report.explanation),
        )
    return report