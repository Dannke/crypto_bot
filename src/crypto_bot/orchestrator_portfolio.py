"""Portfolio mode orchestrator - Decision Intelligence Layer integration.

Orchestrates market data fetch, feature generation, portfolio decision pipeline,
journaling, and optional paper-trading simulation. No live orders in stage 3.

R8 features:
- Restart-safe rebalance scheduler: persists next_rebalance_ts to SQLite
- Separate regime cadence from rebalance cadence
- Regime recalculation on independent cadence
- Restart recovery: loads persisted state on startup
"""
from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

from .config.env import Config
from .core.enums import Mode, RejectReason
from .core.exceptions import LiveTradingForbiddenError, OrchestratorError
from .core.logging_setup import get_logger
from .core.policy import timeframe_to_seconds
from .core.validators import validate_mode_compatibility
from .data.exchange import MarketDataClient
from .data.feed import Feed, resolve_symbols
from .data.instruments import build_instrument_cache
from .features.batch import build_features_batch
from .features.builder import builder_from_settings
from .features.context import market_context_from_ticker
from .pipeline.factory import (
    build_portfolio_decision_pipeline,
    build_portfolio_risk_engine,
    build_portfolio_strategy,
)
from .portfolio import PortfolioState
from .portfolio.models import UniverseSnapshot
from .simulation.pnl import PnLTracker
from .simulation.portfolio_executor import PortfolioExecutor
from .simulation.price_simulator import PriceSimulator
from .storage.db import Database, Repositories

logger = get_logger(__name__)


# R8: Key for persisting rebalance scheduler state in SQLite
REBALANCE_STATE_KEY = "portfolio_rebalance_state"
REGIME_STATE_KEY = "portfolio_regime_state"


async def _load_rebalance_state(db: Database) -> tuple[int | None, int | None]:
    """Load persisted rebalance and regime scheduler state from SQLite."""
    try:
        row = db.conn.execute(
            "SELECT value FROM state WHERE key IN (?, ?)",
            (REBALANCE_STATE_KEY, REGIME_STATE_KEY),
        ).fetchall()
        rebalance_ts = None
        regime_ts = None
        for r in row:
            if r[0] is not None:
                # We store as JSON: {"next_rebalance_ms": ..., "next_regime_ms": ...}
                import json
                data = json.loads(r[0])
                rebalance_ts = data.get("next_rebalance_ms")
                regime_ts = data.get("next_regime_ms")
                break
        return rebalance_ts, regime_ts
    except Exception as exc:
        logger.warning("Failed to load rebalance/regime state: %s", exc)
        return None, None


async def _save_rebalance_state(
    db: Database, next_rebalance_ms: int | None, next_regime_ms: int | None
) -> None:
    """Persist rebalance and regime scheduler state to SQLite."""
    try:
        import json
        data = json.dumps(
            {"next_rebalance_ms": next_rebalance_ms, "next_regime_ms": next_regime_ms}
        )
        db.conn.execute(
            "INSERT OR REPLACE INTO state (key, value, updated_at) VALUES (?, ?, ?)",
            (
                REBALANCE_STATE_KEY,
                data,
                int(time.time()),
            ),
        )
        db.conn.commit()
    except Exception as exc:
        logger.warning("Failed to save rebalance/regime state: %s", exc)


def _strategy_reject_reason(signal_reason: str) -> RejectReason:
    reason = signal_reason.lower()
    if "conflict" in reason:
        return RejectReason.CONFLICTING_TIMEFRAMES
    if "partial" in reason:
        return RejectReason.LOW_CONFIDENCE
    if "no directional" in reason or "missing timeframes" in reason:
        return RejectReason.NO_DIRECTION
    return RejectReason.LOW_SCORE


async def run_portfolio_orchestrator(config: Config) -> None:
    """Main scan loop using the portfolio decision pipeline."""
    validate_mode_compatibility(config)

    settings = config.settings
    logger.info("Starting portfolio orchestrator in %s mode", settings.runtime.mode)
    strategy_name = getattr(settings.portfolio, "strategy_name", "cross_sectional_momentum_v0")
    logger.info("Portfolio strategy: %s", strategy_name)

    # R8: Load persisted scheduler state
    instrument_cache = await build_instrument_cache(config)

    db = Database(settings.storage.db_path)
    repos = Repositories(db)

    # R8: Load persisted scheduler state
    persisted_rebalance_ms, persisted_regime_ms = await _load_rebalance_state(db)

    feature_builder = builder_from_settings(settings)
    portfolio_pipeline = build_portfolio_decision_pipeline(settings)
    portfolio_strategy = build_portfolio_strategy(settings)
    risk_engine = build_portfolio_risk_engine(settings)
    executor = PortfolioExecutor(
        config, repos, PnLTracker(), instrument_cache=instrument_cache
    )

    quote = settings.universe.quote.upper()
    trigger_tf = settings.timeframes.primary[0]
    is_paper = settings.runtime.mode == Mode.PAPER

    # R8: Rebalance scheduler with persistence
    csm = getattr(settings.portfolio, "csm", None)
    rebalance_hours = (csm.rebalance_hours if csm is not None else 24)
    rebalance_ms = rebalance_hours * 3_600_000

    # R8: Separate regime cadence (default 1 hour, configurable)
    regime_cadence_hours = getattr(settings.portfolio, "regime_cadence_hours", 1)
    regime_cadence_ms = regime_cadence_hours * 3_600_000

    # R8: Initialize scheduler timestamps
    now_ms = int(time.time() * 1000)
    next_rebalance_ms = persisted_rebalance_ms or (now_ms + rebalance_ms)
    next_regime_ms = persisted_regime_ms or (now_ms + regime_cadence_ms)

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
                now_ms = int(time.time() * 1000)
                logger.info("Starting portfolio scan cycle at %s", datetime.now(tz=UTC).isoformat())

                try:
                    t0 = time.time()
                    logger.info("--- portfolio scan cycle starts ---")
                    symbols = await resolve_symbols(
                        client, config, feed.exchange_available, instrument_cache
                    )
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

                    equity = executor.tracker.current_equity
                    state = PortfolioState(
                        as_of_ms=now_ms,
                        mode=Mode.PAPER if is_paper else Mode.SIGNAL_ONLY,
                        equity=equity,
                        cash=equity,
                        positions=executor.open_positions,
                    )

                    universe = UniverseSnapshot(
                        as_of_ms=now_ms,
                        symbols=tuple(symbols),
                        quote_currency=quote,
                    )

                    # R8: Check if regime recalculation is due
                    regime_due = now_ms >= next_regime_ms
                    if regime_due:
                        logger.info("Regime recalculation due at %s", datetime.now(tz=UTC).isoformat())
                        next_regime_ms = now_ms + regime_cadence_ms

                    intent = portfolio_pipeline.process(
                        universe,
                        symbol_candles,
                        market_by_symbol,
                        state,
                        portfolio_strategy,
                        trigger_tf=trigger_tf,
                    )
                    t5 = time.time()

                    report = risk_engine.evaluate(intent, state)
                    t6 = time.time()

                    logger.info(
                        "timings: resolve=%.1f tickers=%.1f "
                        "feed=%.1f features=%.1f pipeline=%.1f risk=%.1f",
                        t1 - t0, t2 - t1, t3 - t2, t4 - t3, t5 - t4, t6 - t5,
                    )
                    logger.info(
                        "--- portfolio cycle: intents=%d accepted=%d "
                        "rejected=%d gross=%.4f net=%.4f",
                        len(report.adjusted_intent.intents),
                        sum(1 for r in report.position_results if r.accepted),
                        sum(1 for r in report.position_results if not r.accepted),
                        report.gross_exposure,
                        report.net_exposure,
                    )

                    # R8: Check if rebalance is due
                    rebalance_due = now_ms >= next_rebalance_ms
                    if rebalance_due:
                        logger.info("Portfolio rebalance due at %s", datetime.now(tz=UTC).isoformat())
                        next_rebalance_ms = now_ms + rebalance_ms

                    # R8: Persist scheduler state after each cycle
                    await _save_rebalance_state(db, next_rebalance_ms, next_regime_ms)

                    if is_paper:
                        for sym in symbols:
                            if tickers and sym in tickers:
                                last = tickers[sym].get("last", 0.0)
                                if last > 0:
                                    fs_dict = features_by_symbol.get(sym, {})
                                    fs = fs_dict.get(trigger_tf) if fs_dict else None
                                    atr_pct = fs.atr_pct if fs is not None else None
                                    price_sim.set_anchor(
                                        sym, price=last, atr_pct=atr_pct,
                                        candle_seconds=timeframe_to_seconds(trigger_tf)
                                    )
                                for sf in feeds:
                                    if sf.symbol in price_sim.tracked_symbols():
                                        continue
                                    for candles in sf.by_timeframe.values():
                                        if candles:
                                            price_sim.set_anchor(sf.symbol, price=candles[-1].close)
                                            break

                    async with executor_lock:
                        for position_intent in report.adjusted_intent.intents:
                            tf = position_intent.timeframe or trigger_tf
                            feature = features_by_symbol.get(position_intent.symbol, {}).get(tf)
                            if feature is not None:
                                entry = feature.close if feature.close > 0 else feature.ema_fast
                                atr_pct = feature.atr_pct
                                spread_pct = feature.spread_pct
                            else:
                                bars = symbol_candles.get(position_intent.symbol, {}).get(tf, [])
                                if not bars:
                                    logger.info(
                                        "portfolio skip %s ts=%d - no bars on %s",
                                        position_intent.symbol, now_ms, tf,
                                    )
                                    continue
                                bar = bars[-1]
                                entry = bar.close
                                if entry <= 0:
                                    continue
                                if bar.low > 0:
                                    atr_pct = (bar.high - bar.low) / bar.low * 100.0
                                else:
                                    atr_pct = 1.0
                                spread_pct = 0.0

                            exec_result = executor.open_position(
                                position_intent,
                                entry_price=entry,
                                atr_pct=atr_pct,
                                spread_pct=spread_pct,
                                timestamp_ms=now_ms,
                            )
                            logger.info(
                                ">>> %s %s tf=%s weight=%.4f - %s",
                                position_intent.side.value,
                                position_intent.symbol,
                                tf,
                                position_intent.target_weight,
                                exec_result.message,
                            )

                        if is_paper:
                            current_prices: dict[tuple[str, str], float] = {}
                            for pos in executor.tracker.positions:
                                if not pos.is_open:
                                    continue
                                price = (
                                    tickers.get(pos.symbol, {}).get("last")
                                    if tickers else None
                                )
                                if price is None or price <= 0:
                                    for sf in feeds:
                                        if sf.symbol != pos.symbol:
                                            continue
                                        candles = sf.by_timeframe.get(pos.timeframe, [])
                                        if candles:
                                            price = candles[-1].close
                                        break
                                if price is not None and price > 0:
                                    current_prices[(pos.symbol, pos.timeframe)] = price
                            equity_now = executor.tracker.mark_to_market_equity(current_prices)
                            executor.tracker.record_equity(datetime.now(tz=UTC), equity_now)
                            repos.equity.insert(
                                currency=quote, equity=equity_now,
                                drawdown_pct=0.0, mode=Mode.PAPER, ts_ms=now_ms,
                            )

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
        logger.info("Portfolio orchestrator stopped gracefully")
    except LiveTradingForbiddenError as exc:
        logger.critical("Live trading forbidden: %s", exc)
        raise OrchestratorError("Live trading gate blocked") from exc
    except Exception as exc:
        logger.critical("Portfolio orchestrator crashed", exc_info=True)
        raise OrchestratorError("Portfolio orchestrator failure") from exc
    finally:
        if sim_task is not None:
            sim_task.cancel()
        # R8: Save final state on shutdown
        await _save_rebalance_state(db, next_rebalance_ms, next_regime_ms)
        if is_paper:
            summary = executor.tracker.get_summary()
            logger.info(
                "=== FINAL portfolio paper pnl: trades=%d win_rate=%.1f%% total=%.2f (%.2f%%)"
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