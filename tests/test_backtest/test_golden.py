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

from crypto_bot.core.types import Candle
from crypto_bot.simulation.backtester import Backtester
from crypto_bot.simulation.historical_source import HistoricalCandleSource
from crypto_bot.storage.db import Database

from ._shared import uptrend_candles, golden_config


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