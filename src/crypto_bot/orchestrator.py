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
from .core.enums import Mode, RejectReason
from .core.exceptions import OrchestratorError
from .core.logging_setup import get_logger
from .core.policy import timeframe_to_seconds
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
from .simulation.price_simulator import PriceSimulator
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
    is_paper = settings.runtime.mode == Mode.PAPER

    price_sim = PriceSimulator()
    executor_lock = asyncio.Lock()
    sim_task: asyncio.Task[None] | None = None

    async def _sim_check_positions() -> None:
        """Called from background tick every ~1s under lock."""
        sym_tfs = executor.tracker.open_symbol_timeframes()
        if not sym_tfs:
            return
        async with executor_lock:
            current_prices = price_sim.get_current_prices(sym_tfs)
            if current_prices:
                stats = executor.check_positions(current_prices)
                total = stats["closed_by_sl"] + stats["closed_by_tp"]
                if total:
                    logger.info(
                        "tick SL/TP: %d closed (SL=%d pnl=%.2f, TP=%d pnl=%.2f)",
                        total,
                        stats["closed_by_sl"], stats["closed_by_sl_pnl"],
                        stats["closed_by_tp"], stats["closed_by_tp_pnl"],
                    )

    try:
        async with MarketDataClient(config) as client:
            feed = Feed(client, config, db)
            while True:
                cycle_start = time.time()
                logger.info("Starting scan cycle at %s", datetime.now(tz=UTC).isoformat())

                try:
                    t0 = time.time()
                    logger.info("--- scan cycle starts ---")
                    symbols = await resolve_symbols(client, config, feed.exchange_available)
                    t1 = time.time()
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
                    t2 = time.time()

                    feeds = await feed.fetch_many(symbols)
                    symbol_candles = {f.symbol: f.by_timeframe for f in feeds}
                    t3 = time.time()

                    features_by_symbol = build_features_batch(
                        symbol_candles,
                        feature_builder,
                        market_by_symbol,
                        quote=quote,
                        trigger_tf=trigger_tf,
                    )
                    t4 = time.time()

                    result = pipeline.process(features_by_symbol, strategy, per_timeframe=per_timeframe_mode)
                    t5 = time.time()

                    logger.info(
                        "timings: resolve=%.1f tickers=%.1f feed=%.1f features=%.1f pipeline=%.1f",
                        t1 - t0, t2 - t1, t3 - t2, t4 - t3, t5 - t4,
                    )
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

                    # ---- update price simulator anchors BEFORE opening positions ----
                    if is_paper:
                        for sym in symbols:
                            if tickers and sym in tickers:
                                last = tickers[sym].get("last", 0.0)
                                if last > 0:
                                    fs_dict = features_by_symbol.get(sym, {})
                                    fs = fs_dict.get(trigger_tf) if fs_dict else None
                                    atr_pct = fs.atr_pct if fs is not None else None
                                    price_sim.set_anchor(sym, price=last, atr_pct=atr_pct,
                                                         candle_seconds=timeframe_to_seconds(trigger_tf))
                        # fallback: символы без тикера — из последней свечи
                        for sf in feeds:
                            if sf.symbol in price_sim.tracked_symbols():
                                continue
                            for candles in sf.by_timeframe.values():
                                if candles:
                                    price_sim.set_anchor(sf.symbol, price=candles[-1].close)
                                    break

                    # ---- handle selected signals (under lock) ----
                    async with executor_lock:
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

                        # ---- paper mode: open positions for ALL accepted reports ----
                        if is_paper:
                            accepted = result.get("accepted", [])
                            for report in accepted:
                                if report in result["selected"]:
                                    continue
                                exec_result = executor.handle_selected(report)
                                if exec_result.handled:
                                    tf = report.features.get("timeframe", "?")
                                    logger.info(
                                        "  paper+ %s %s tf=%s score=%.1f — %s",
                                        report.signal.value,
                                        report.symbol,
                                        tf,
                                        report.total_score,
                                        exec_result.message,
                                    )

                        for report in result["rejected"]:
                            executor.handle_rejected(_normalize_rejected(report))

                    # ---- start background tick loop after first anchor ----
                    if is_paper and sim_task is None and price_sim.tracked_symbols():
                        logger.info(
                            "starting price simulator background tick for %d symbols",
                            len(price_sim.tracked_symbols()),
                        )
                        sim_task = asyncio.create_task(
                            price_sim.run_forever(
                                symbols_provider=price_sim.tracked_symbols,
                                on_cycle_end=_sim_check_positions,
                            )
                        )

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
        if sim_task is not None:
            sim_task.cancel()
        # Log final summary on shutdown
        if is_paper:
            summary = executor.tracker.get_summary()
            logger.info(
                "=== FINAL paper pnl: trades=%d win_rate=%.1f%% total=%.2f (%.2f%%)"
                "  SL=%d(%.2f) TP=%d(%.2f) max_dd=%.2f%%",
                summary.total_trades,
                summary.win_rate * 100,
                summary.total_pnl_abs,
                summary.total_pnl_pct,
                summary.closed_by_sl, summary.pnl_from_sl,
                summary.closed_by_tp, summary.pnl_from_tp,
                summary.max_drawdown_pct,
            )
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
