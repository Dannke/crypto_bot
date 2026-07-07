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
from crypto_bot.config.schemas import Settings
from crypto_bot.core.enums import Mode
from crypto_bot.core.types import Candle
from crypto_bot.simulation.backtester import Backtester
from crypto_bot.simulation.historical_source import HistoricalCandleSource


def _uptrend_candles() -> list[Candle]:
    """30 1h candles in a strong monotonic uptrend (≈1.5 % / bar)."""
    price = 100.0
    base_ts = 1_700_000_000_000
    period_ms = 3_600_000
    candles: list[Candle] = []
    for i in range(30):
        ts = base_ts + i * period_ms
        open_p = round(price, 2)
        close_p = round(price * 1.015, 2)
        high_p = round(max(open_p, close_p) * 1.005, 2)
        low_p = round(min(open_p, close_p) * 0.995, 2)
        candles.append(Candle(
            timestamp=ts,
            open=open_p,
            high=high_p,
            low=low_p,
            close=close_p,
            volume=1000.0,
        ))
        price = close_p
    return candles


def _make_settings(**overrides) -> Settings:
    base = Settings().model_dump()
    for key, value in overrides.items():
        parts = key.split("__")
        d = base
        for p in parts[:-1]:
            d = d[p]
        d[parts[-1]] = value
    return Settings.model_validate(base)


def _make_env(**overrides) -> EnvConfig:
    data = dict(
        crypto_bot_mode=None,
        enable_trading=False,
        enable_live_trading=False,
        exchange_name=None,
        exchange_api_key="",
        exchange_api_secret="",
        exchange_sandbox=None,
        db_path=None,
        log_level=None,
        log_file=None,
        log_json=None,
    )
    data.update(overrides)
    return EnvConfig.model_validate(data)


def _golden_config() -> Config:
    """Build a Settings + EnvConfig tuned for the golden test.

    Most importantly:
    * ultra-short indicator periods so min_needed == 8
    * liquidity / spread / volume filters disabled (no real market context)
    * very low min_score (30) to let the signal through
    """
    settings = _make_settings(
        runtime__mode=Mode.PAPER,
        runtime__strategy="per_timeframe",
        # Indicator periods — deliberately short
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
        strategy__volatility__bb_std=2.0,
        strategy__volume__ma_period=3,
        strategy__volume__spike_ratio=1.0,
        # Scoring — let the signal through easily
        scoring__min_score=30,
        scoring__min_confidence=0.0,
        scoring__max_candidates_per_cycle=1,
        # Filters — disable checks that need real market context
        filters__enable_liquidity_filter=False,
        filters__enable_spread_filter=False,
        filters__enable_volume_filter=False,
        filters__cooldown_after_trade_minutes=0,
        filters__min_quote_volume_usd=0,
        # Risk params
        risk__risk_per_trade_pct=1.0,
        risk__take_profit_risk_multiple=2.0,
        risk__max_stop_distance_pct=5.0,
        risk__max_open_positions=5,
        risk__max_daily_drawdown_pct=20.0,
        risk__emergency_drawdown_pct=30.0,
    )
    env = _make_env(crypto_bot_mode=Mode.PAPER)
    return Config(settings=settings, env=env)


@pytest.mark.asyncio
async def test_golden_uptrend_produces_one_long_closed_by_tp():
    candles = _uptrend_candles()
    period_ms = 3_600_000
    start_ms = candles[0].timestamp
    end_ms = candles[-1].timestamp + period_ms

    source = HistoricalCandleSource()
    source.load_all("BTC/USDT", "1h", candles)

    config = _golden_config()
    bt = Backtester(
        config,
        symbol="BTC/USDT",
        timeframe="1h",
        start_ms=start_ms,
        end_ms=end_ms,
        source=source,
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
