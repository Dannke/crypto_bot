"""Shared fixtures and helpers for backtest validation tests.

Reusable across:
- test_golden.py          (golden × uptrend)
- test_live_vs_backtester_synthetic.py  (golden/production × uptrend/downtrend/flat)
- test_cross_validate_snapshot.py       (snapshot fixtures)
"""
from __future__ import annotations

from crypto_bot.config.env import Config, EnvConfig
from crypto_bot.config.schemas import Settings
from crypto_bot.core.enums import Mode
from crypto_bot.core.types import Candle

# --------------------------------------------------------------------------- #
# Candles
# --------------------------------------------------------------------------- #

def uptrend_candles() -> list[Candle]:
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


def downtrend_candles() -> list[Candle]:
    """30 1h candles in a strong monotonic downtrend (≈1.5 % / bar)."""
    price = 100.0
    base_ts = 1_700_000_000_000
    period_ms = 3_600_000
    candles: list[Candle] = []
    for i in range(30):
        ts = base_ts + i * period_ms
        open_p = round(price, 2)
        close_p = round(price * 0.985, 2)
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


def flat_candles() -> list[Candle]:
    """30 1h candles with low amplitude random walk — weak/no trend.
    ATR stays sub-0.3%; ADX stays sub-10.  Expected rejects: no_direction
    and/or volatility_out_of_range.
    """
    prices = [100.0 + 0.05 * i + 0.3 * ((i % 5) - 2) for i in range(30)]
    base_ts = 1_700_000_000_000
    period_ms = 3_600_000
    candles: list[Candle] = []
    for i, p in enumerate(prices):
        ts = base_ts + i * period_ms
        candles.append(Candle(
            timestamp=ts,
            open=round(p, 2),
            high=round(p + 0.15, 2),
            low=round(p - 0.15, 2),
            close=round(p + 0.02, 2),
            volume=1000.0,
        ))
    return candles


# --------------------------------------------------------------------------- #
# Synthetic market context for production-config tests
# --------------------------------------------------------------------------- #
MARKET_QUOTE_VOLUME: dict[str, float] = {
    "BTC/USDT": 20_000_000_000,
    "ETH/USDT": 10_000_000_000,
    "DOGE/USDT": 500_000_000,
}

# --------------------------------------------------------------------------- #
# Config builders
# --------------------------------------------------------------------------- #

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


def golden_config() -> Config:
    """Golden test config: ultra-short indicators, filters disabled, min_score=30."""
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
        risk__risk_per_trade_pct=1.0,
        risk__take_profit_risk_multiple=2.0,
        risk__max_stop_distance_pct=5.0,
        risk__max_open_positions=5,
        risk__max_daily_drawdown_pct=20.0,
        risk__emergency_drawdown_pct=30.0,
    )
    env = _make_env(crypto_bot_mode=Mode.PAPER)
    return Config(settings=settings, env=env)


def production_config(settings_override: dict | None = None, env_override: dict | None = None) -> Config:
    """Build a Config from current config/settings.yaml defaults with optional overrides.

    Args:
        settings_override: Keys like ``"scoring__min_score"`` → value.
        env_override: EnvConfig fields.

    Use for synthetic tests that need real filter thresholds but synthetic candles.
    """
    overrides = dict(
        runtime__mode=Mode.PAPER,
        runtime__strategy="per_timeframe",
    )
    if settings_override:
        overrides.update(settings_override)

    env_data = dict(crypto_bot_mode=Mode.PAPER)
    if env_override:
        env_data.update(env_override)

    return Config(
        settings=_make_settings(**overrides),
        env=_make_env(**env_data),
    )


# --------------------------------------------------------------------------- #
# Snapshot config keys for serialisation (full subtrees, not hand-picked fields)
# --------------------------------------------------------------------------- #
CONFIG_SNAPSHOT_KEYS = ["strategy", "scoring", "filters", "risk", "timeframes"]


def snapshot_config_subtree(settings: Settings) -> dict:
    """Extract the config subtrees needed to reproduce a snapshot test."""
    return settings.model_dump(include=set(CONFIG_SNAPSHOT_KEYS))