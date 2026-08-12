import sqlite3

import pytest

from crypto_bot.config.env import Config, EnvConfig
from crypto_bot.config.schemas import Settings
from crypto_bot.core.enums import Mode
from crypto_bot.core.types import Candle
from crypto_bot.simulation.backtester import Backtester
from crypto_bot.simulation.historical_source import HistoricalCandleSource
from crypto_bot.storage.db import Database


def _candles() -> list[Candle]:
    """130 1h bars: brief uptrend → sharp crash → strong reversal."""
    base_ts = 1_700_000_000_000
    period_ms = 3_600_000
    candles: list[Candle] = []

    price = 50000.0
    for i in range(30):
        ts = base_ts + i * period_ms
        o = round(price, 2)
        c = round(price * 1.0025, 2)
        h = round(max(o, c) * 1.001, 2)
        l = round(min(o, c) * 0.999, 2)
        candles.append(Candle(timestamp=ts, open=o, high=h, low=l, close=c, volume=1000.0))
        price = c

    for i in range(20):
        ts = base_ts + (30 + i) * period_ms
        o = round(price, 2)
        c = round(price * 0.95, 2)
        h = round(max(o, c) * 1.003, 2)
        l = round(min(o, c) * 0.997, 2)
        candles.append(Candle(timestamp=ts, open=o, high=h, low=l, close=c, volume=1000.0))
        price = c

    for i in range(80):
        ts = base_ts + (50 + i) * period_ms
        o = round(price, 2)
        c = round(price * 1.005, 2)
        h = round(max(o, c) * 1.002, 2)
        l = round(min(o, c) * 0.998, 2)
        candles.append(Candle(timestamp=ts, open=o, high=h, low=l, close=c, volume=1000.0))
        price = c

    return candles


def _settings(**overrides) -> Settings:
    base = Settings().model_dump()
    for key, value in overrides.items():
        parts = key.split("__")
        d = base
        for p in parts[:-1]:
            d = d[p]
        d[parts[-1]] = value
    return Settings.model_validate(base)


def _config(settings_overrides: dict | None = None) -> Config:
    overrides = dict(
        runtime__mode=Mode.PAPER,
        runtime__strategy="per_timeframe",
        strategy__trend__ema_fast=3,
        strategy__trend__ema_mid=5,
        strategy__trend__ema_slow=8,
        strategy__trend__adx_min=5,
        strategy__momentum__rsi_period=4,
        strategy__momentum__rsi_long_min=0,
        strategy__momentum__rsi_long_max=100,
        strategy__momentum__rsi_short_min=100,
        strategy__momentum__rsi_short_max=100,
        strategy__volatility__atr_period=3,
        strategy__volatility__atr_min_pct=0.1,
        strategy__volatility__atr_max_pct=20.0,
        strategy__volatility__bb_period=4,
        strategy__volatility__bb_std=2.0,
        strategy__volume__ma_period=3,
        strategy__volume__spike_ratio=1.0,
        scoring__min_score=30,
        scoring__min_confidence=0.0,
        scoring__max_candidates_per_cycle=1,
        filters__enable_liquidity_filter=False,
        filters__enable_spread_filter=False,
        filters__enable_volume_filter=False,
        filters__cooldown_after_trade_minutes=0,
        filters__min_quote_volume_usd=0,
        risk__risk_per_trade_pct=5.0,
        risk__take_profit_risk_multiple=2.0,
        risk__max_stop_distance_pct=3.0,
        risk__max_open_positions=5,
        risk__max_daily_drawdown_pct=5.0,
        risk__emergency_drawdown_pct=6.0,
    )
    if settings_overrides:
        overrides.update(settings_overrides)
    settings = _settings(**overrides)
    env = EnvConfig(crypto_bot_mode=Mode.PAPER)
    return Config(settings=settings, env=env)


@pytest.mark.asyncio
async def test_halt_blocks_positions_during_reversal(tmp_path):
    candles = _candles()
    period_ms = 3_600_000
    start_ms = candles[0].timestamp
    end_ms = candles[-1].timestamp + period_ms

    source = HistoricalCandleSource()
    source.load_all("TEST/USDT", "1h", candles)

    bt = Backtester(
        _config(),
        symbols="TEST/USDT",
        timeframes="1h",
        start_ms=start_ms,
        end_ms=end_ms,
        source=source,
        db=Database(tmp_path / "halt_test.db"),
    )

    summary = await bt.run_async()

    assert bt.emergency_halt_triggered, (
        f"halt not triggered — maxDD={summary.max_drawdown_pct:.2f}%"
    )

    con = sqlite3.connect(str(tmp_path / "halt_test.db"))
    try:
        positions = con.execute(
            "SELECT id, opened_at_ms, status FROM positions"
        ).fetchall()
    finally:
        con.close()

    assert len(positions) > 0, "should have opened positions before halt"

    phase3_start_ms = int(candles[0].timestamp + 50 * period_ms)
    blocked = [p for p in positions if p[1] >= phase3_start_ms]
    assert len(blocked) == 0, (
        f"{len(blocked)} position(s) opened after halt should have fired"
    )
