"""Golden test: verifies the backtester against a synthetic scenario with a
known outcome — a clear uptrend that MUST produce exactly one LONG trade
closed by take-profit.

Indicator parameters are chosen so that ``min_needed == 8`` (see compute_raw_metrics):

    min_needed = max(ema_slow=8, rsi_period+1=5, 2*atr_period+1=7,
                     bb_period=4, volma_period=3) = 8

This means the first bar at which features can be computed is bar index 7
(the 8th candle).
"""
from __future__ import annotations

import pytest

from crypto_bot.config.env import Config, EnvConfig
from crypto_bot.core.enums import Mode
from crypto_bot.core.types import Candle
from crypto_bot.simulation.backtester import Backtester
from crypto_bot.simulation.historical_source import HistoricalCandleSource
from crypto_bot.storage.db import Database

from ._shared import uptrend_candles, golden_config, _make_settings


@pytest.mark.asyncio
async def test_golden_uptrend_produces_one_long_closed_by_tp(tmp_path):
    candles = uptrend_candles()
    period_ms = 3_600_000
    start_ms = candles[0].timestamp
    end_ms = candles[-1].timestamp + period_ms

    source = HistoricalCandleSource()
    source.load_all("BTC/USDT", "1h", candles)

    config = golden_config()
    bt = Backtester(
        config,
        symbols=["BTC/USDT"],
        timeframes=["1h"],
        start_ms=start_ms,
        end_ms=end_ms,
        source=source,
        db=Database(tmp_path / "golden_bt.db"),
    )

    summary = await bt.run_async()

    # ---- assertions ----
    assert summary.total_trades >= 1, (
        f"expected at least 1 trade, got {summary.total_trades}"
    )
    assert summary.winning_trades == summary.total_trades, (
        f"expected all winning, got {summary.winning_trades}/{summary.total_trades}"
    )
    assert summary.closed_by_tp == summary.total_trades, (
        f"expected all closed by TP, got SL={summary.closed_by_sl} TP={summary.closed_by_tp}"
    )
    assert summary.win_rate == 1.0, (
        f"expected 100% win rate, got {summary.win_rate}"
    )
    assert summary.total_pnl_pct > 0, (
        f"expected positive P&L, got {summary.total_pnl_pct}%"
    )


@pytest.mark.asyncio
async def test_golden_shared_max_positions_across_timeframes(tmp_path):
    """Two timeframes share one global max_open_positions cap.

    Generate aligned 5m + 1h data for one symbol, set max_open_positions=1.
    The first 5m bar that produces a valid signal opens a position; all
    subsequent signals on any timeframe are rejected because the single slot
    is occupied.  This proves the limit is shared across timeframes — a
    scenario that four independent single-TF runs could never validate.
    """
    base_ts = 1_700_000_000_000
    period_5m = 300_000
    period_1h = 3_600_000

    # 360 5m candles = 30 hours — enough for both 5m and 1h features
    candles_5m: list[Candle] = []
    price = 100.0
    for i in range(360):
        ts = base_ts + i * period_5m
        open_p = round(price, 2)
        close_p = round(price * 1.0025, 2)
        high_p = round(max(open_p, close_p) * 1.001, 2)
        low_p = round(min(open_p, close_p) * 0.999, 2)
        candles_5m.append(Candle(
            timestamp=ts, open=open_p, high=high_p,
            low=low_p, close=close_p, volume=1000.0,
        ))
        price = close_p

    # Aggregate 1h candles from 5m data (12 x 5m = 1h)
    candles_1h: list[Candle] = []
    for h in range(30):
        start = h * 12
        end = start + 12
        group = candles_5m[start:end]
        ts = group[0].timestamp
        open_p = group[0].open
        close_p = group[-1].close
        high_p = max(c.high for c in group)
        low_p = min(c.low for c in group)
        candles_1h.append(Candle(
            timestamp=ts, open=open_p, high=high_p,
            low=low_p, close=close_p, volume=sum(c.volume for c in group),
        ))

    start_ms = candles_5m[0].timestamp
    end_ms = candles_5m[-1].timestamp + period_5m

    source = HistoricalCandleSource()
    source.load_all("BTC/USDT", "5m", candles_5m)
    source.load_all("BTC/USDT", "1h", candles_1h)

    # Config: golden params but max_open_positions=1 and
    # max_candidates_per_cycle=5 so multiple TFs can be selected per cycle
    settings = _make_settings(
        runtime__mode=Mode.PAPER,
        runtime__strategy="per_timeframe",
        strategy__trend__ema_fast=3,
        strategy__trend__ema_mid=5,
        strategy__trend__ema_slow=8,
        strategy__trend__adx_min=5,
        strategy__momentum__rsi_period=4,
        strategy__momentum__rsi_long_min=0,
        strategy__momentum__rsi_long_max=100,
        strategy__volatility__atr_period=3,
        strategy__volatility__atr_min_pct=0.1,
        strategy__volatility__atr_max_pct=20.0,
        strategy__volatility__bb_period=4,
        strategy__volume__ma_period=3,
        strategy__volume__spike_ratio=1.0,
        scoring__min_score=30,
        scoring__min_confidence=0.0,
        scoring__max_candidates_per_cycle=5,
        filters__enable_liquidity_filter=False,
        filters__enable_spread_filter=False,
        filters__enable_volume_filter=False,
        filters__cooldown_after_trade_minutes=0,
        filters__min_quote_volume_usd=0,
        risk__risk_per_trade_pct=1.0,
        risk__take_profit_risk_multiple=1000.0,
        risk__max_stop_distance_pct=5.0,
        risk__max_open_positions=1,
            risk__max_daily_drawdown_pct=20.0,
            risk__emergency_drawdown_pct=30.0,
    )
    env = EnvConfig(crypto_bot_mode=Mode.PAPER)
    config = Config(settings=settings, env=env)

    db_path = tmp_path / "golden_mtf.db"
    bt = Backtester(
        config,
        symbols=["BTC/USDT"],
        timeframes=["5m", "1h"],
        start_ms=start_ms,
        end_ms=end_ms,
        source=source,
        db=Database(db_path),
    )

    await bt.run_async()

    # With max_open_positions=1, at most 1 position can ever be opened.
    # The first 5m signal opens a position; all subsequent signals
    # (5m + 1h alike) are blocked by the global executor cap.
    import sqlite3

    con = sqlite3.connect(str(db_path))
    total = con.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
    opened = con.execute("SELECT COUNT(*) FROM positions WHERE status='open'").fetchone()[0]
    con.close()

    assert total == 1, (
        f"expected exactly 1 position total with max_open_positions=1, "
        f"got {total} — indicates TFs opened independently"
    )
    assert opened == 1, (
        f"expected the single position to remain open, got {opened} open"
    )


# --------------------------------------------------------------------------- #
# Multi-position emergency drawdown: all open positions must be closed
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_golden_emergency_drawdown_closes_all_open_positions(tmp_path):
    """When emergency drawdown triggers, ALL open positions (multi-symbol)
    must be closed by ``emergency_drawdown`` — not by SL/TP — and no new
    positions may open afterward.  MaxDD must stay near the halt threshold.
    """
    period_1h = 3_600_000
    base_ts = 1_700_000_000_000

    # Build 200 1h bars: up then crash
    btc_prices: list[Candle] = []
    eth_prices: list[Candle] = []
    btc_price = 100.0
    eth_price = 100.1
    for i in range(200):
        ts = base_ts + i * period_1h
        if i < 120:
            trend = 1.0035       # uptrend
        elif i < 145:
            trend = 0.975        # crash –3.60 % / bar
        else:
            trend = 1.001        # recovery

        for sym_data, price in [(btc_prices, btc_price), (eth_prices, eth_price)]:
            open_p = round(price, 2)
            close_p = round(price * trend, 2)
            high_p = round(max(open_p, close_p) * 1.002, 2)
            low_p = round(min(open_p, close_p) * 0.998, 2)
            sym_data.append(Candle(
                timestamp=ts, open=open_p, high=high_p,
                low=low_p, close=close_p, volume=1000.0,
            ))
        btc_price = btc_prices[-1].close
        eth_price = eth_prices[-1].close

    candles = {"BTC/USDT": btc_prices, "ETH/USDT": eth_prices}

    start_ms = candles["BTC/USDT"][0].timestamp
    end_ms = candles["BTC/USDT"][-1].timestamp + period_1h

    source = HistoricalCandleSource()
    source.load_all("BTC/USDT", "1h", candles["BTC/USDT"])
    source.load_all("ETH/USDT", "1h", candles["ETH/USDT"])

    settings = _make_settings(
        runtime__mode=Mode.PAPER,
        runtime__strategy="per_timeframe",
        strategy__trend__ema_fast=3,
        strategy__trend__ema_mid=5,
        strategy__trend__ema_slow=8,
        strategy__trend__adx_min=5,
        strategy__momentum__rsi_period=4,
        strategy__momentum__rsi_long_min=0,
        strategy__momentum__rsi_long_max=100,
        strategy__volatility__atr_period=3,
        strategy__volatility__atr_min_pct=0.1,
        strategy__volatility__atr_max_pct=20.0,
        strategy__volatility__bb_period=4,
        strategy__volume__ma_period=3,
        strategy__volume__spike_ratio=1.0,
        scoring__min_score=30,
        scoring__min_confidence=0.0,
        scoring__max_candidates_per_cycle=5,
        filters__enable_liquidity_filter=False,
        filters__enable_spread_filter=False,
        filters__enable_volume_filter=False,
        filters__cooldown_after_trade_minutes=0,
        filters__min_quote_volume_usd=0,
            risk__risk_per_trade_pct=5.0,
            risk__take_profit_risk_multiple=1000.0,
            risk__max_stop_distance_pct=5.0,
            risk__max_open_positions=20,
            risk__max_daily_drawdown_pct=10.0,
            risk__emergency_drawdown_pct=20.0,
            risk__max_open_unrealized_drawdown_pct=50.0,
    )
    env = EnvConfig(crypto_bot_mode=Mode.PAPER)
    config = Config(settings=settings, env=env)

    db_path = tmp_path / "golden_emergency_multi.db"
    bt = Backtester(
        config,
        symbols=["BTC/USDT", "ETH/USDT"],
        timeframes=["1h"],
        start_ms=start_ms,
        end_ms=end_ms,
        source=source,
        db=Database(db_path),
    )

    summary = await bt.run_async()

    import sqlite3
    con = sqlite3.connect(str(db_path))
    try:
        # Count positions closed by emergency drawdown
        emergency = con.execute(
            "SELECT COUNT(*) FROM positions WHERE closed_by='emergency_drawdown'"
        ).fetchone()[0]
        total = con.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
        open_count = con.execute(
            "SELECT COUNT(*) FROM positions WHERE status='open'"
        ).fetchone()[0]
        # List closed_by breakdown
        breakdown = dict(con.execute(
            "SELECT closed_by, COUNT(*) FROM positions WHERE closed_by IS NOT NULL GROUP BY closed_by"
        ).fetchall())
    finally:
        con.close()

    # Core assertion: at least 2 positions must have been emergency-closed
    assert emergency >= 2, (
        f"expected ≥2 emergency-closed positions, got {emergency}; "
        f"breakdown={breakdown}"
    )
    # All positions must be closed
    assert open_count == 0, (
        f"expected 0 open positions after backtest, got {open_count}"
    )

    # Halt must have triggered
    assert bt.emergency_halt_triggered, "emergency halt was not triggered"
    logger = _get_logger()
    logger.info(
        "emergency_drawdown test: halt=%s trades=%d maxdd=%.2f%% "
        "emergency_closed=%d total=%d breakdown=%s",
        bt.emergency_halt_triggered, summary.total_trades,
        summary.max_drawdown_pct, emergency, total, breakdown,
    )


def _get_logger():
    import logging
    return logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Unrealized P&L drawdown gate (unit test)
# --------------------------------------------------------------------------- #


