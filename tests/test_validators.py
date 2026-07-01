"""Tests for cross-field validators and the live-trading gate (core/validators).

Focus: a bad config must FAIL at validation time, not slip through silently.
The live gate must block LIVE mode unless every condition is met.
"""
from __future__ import annotations

import pytest

from crypto_bot.config.env import Config
from crypto_bot.core import validators
from crypto_bot.core.enums import Mode
from crypto_bot.core.exceptions import ConfigError, LiveTradingForbiddenError


# --------------------------------------------------------------------------- #
# Timeframes
# --------------------------------------------------------------------------- #
def test_timeframes_must_be_allowed_values(env_factory, settings_factory):
    s = settings_factory(timeframes__primary=["15m", "1h", "4h"])
    cfg = Config(settings=s, env=env_factory())
    validators.validate_timeframes(cfg)  # ok


def test_timeframes_reject_unknown(env_factory, settings_factory):
    s = settings_factory(timeframes__primary=["15m", "1h", "99h"])
    cfg = Config(settings=s, env=env_factory())
    with pytest.raises(ConfigError, match="unsupported timeframe"):
        validators.validate_timeframes(cfg)


def test_timeframes_must_ascend_in_granularity(env_factory, settings_factory):
    s = settings_factory(timeframes__primary=["4h", "1h", "15m"])
    cfg = Config(settings=s, env=env_factory())
    with pytest.raises(ConfigError, match="ascending granularity"):
        validators.validate_timeframes(cfg)


def test_timeframes_reject_duplicates(env_factory, settings_factory):
    s = settings_factory(timeframes__primary=["15m", "15m", "1h", "4h"])
    cfg = Config(settings=s, env=env_factory())
    with pytest.raises(ConfigError, match="duplicates"):
        validators.validate_timeframes(cfg)


# --------------------------------------------------------------------------- #
# Strategy periods vs candle history
# --------------------------------------------------------------------------- #
def test_candle_history_too_small(env_factory, settings_factory):
    # Largest period is ema_slow=200 by default; need 210 bars; give 50.
    s = settings_factory(timeframes__candles_per_tf=50)
    cfg = Config(settings=s, env=env_factory())
    with pytest.raises(ConfigError, match="too small"):
        validators.validate_strategy_periods(cfg)


def test_candle_history_sufficient(env_factory, settings_factory):
    s = settings_factory(timeframes__candles_per_tf=250)
    cfg = Config(settings=s, env=env_factory())
    validators.validate_strategy_periods(cfg)  # ok


# --------------------------------------------------------------------------- #
# Universe
# --------------------------------------------------------------------------- #
def test_universe_rejects_stablecoin_when_excluded(env_factory, settings_factory):
    s = settings_factory(
        universe__symbols=["BTC", "USDC"],
        universe__exclude_stablecoins=True,
    )
    cfg = Config(settings=s, env=env_factory())
    with pytest.raises(ConfigError, match="stablecoin"):
        validators.validate_universe(cfg)


def test_universe_rejects_leveraged_token(env_factory, settings_factory):
    s = settings_factory(
        universe__symbols=["BTC", "BTCUP"],
        universe__exclude_leveraged_tokens=True,
    )
    cfg = Config(settings=s, env=env_factory())
    with pytest.raises(ConfigError, match="leveraged"):
        validators.validate_universe(cfg)


def test_universe_rejects_base_equal_to_quote(env_factory, settings_factory):
    s = settings_factory(universe__quote="USDT", universe__symbols=["USDT"])
    cfg = Config(settings=s, env=env_factory())
    with pytest.raises(ConfigError, match="equals its quote"):
        validators.validate_universe(cfg)


def test_universe_rejects_duplicate_symbol(env_factory, settings_factory):
    s = settings_factory(universe__symbols=["BTC", "BTC"])
    cfg = Config(settings=s, env=env_factory())
    with pytest.raises(ConfigError, match="duplicate"):
        validators.validate_universe(cfg)


def test_universe_rejects_symbol_that_is_excluded(env_factory, settings_factory):
    s = settings_factory(universe__symbols=["BTC", "ETH"], universe__exclude=["ETH"])
    cfg = Config(settings=s, env=env_factory())
    with pytest.raises(ConfigError, match="excluded"):
        validators.validate_universe(cfg)


def test_universe_rejects_unsupported_quote(env_factory, settings_factory):
    s = settings_factory(universe__quote="XYZ")
    cfg = Config(settings=s, env=env_factory())
    with pytest.raises(ConfigError, match="not supported"):
        validators.validate_universe(cfg)


# --------------------------------------------------------------------------- #
# Live gate (the critical safety contract)
# --------------------------------------------------------------------------- #
def test_live_mode_blocked_when_release_flag_off(env_factory, settings_factory, live_released):
    s = settings_factory(runtime__mode=Mode.LIVE)
    cfg = Config(
        settings=s,
        env=env_factory(
            crypto_bot_mode=Mode.LIVE,
            enable_trading=True,
            enable_live_trading=True,
            exchange_sandbox=False,
            exchange_api_key="k",
            exchange_api_secret="s",
        ),
    )
    with live_released(False), pytest.raises(LiveTradingForbiddenError, match="not been released"):
        validators.validate_mode_compatibility(cfg)


def test_live_mode_blocked_when_env_flag_off(env_factory, settings_factory, live_released):
    s = settings_factory(runtime__mode=Mode.LIVE)
    cfg = Config(
        settings=s,
        env=env_factory(
            enable_trading=True,
            enable_live_trading=False,
            exchange_sandbox=False,
            exchange_api_key="k",
            exchange_api_secret="s",
        ),
    )
    with live_released(True), pytest.raises(LiveTradingForbiddenError, match="ENABLE_LIVE_TRADING"):
        validators.validate_mode_compatibility(cfg)


def test_live_mode_blocked_when_sandbox_on(env_factory, settings_factory, live_released):
    s = settings_factory(runtime__mode=Mode.LIVE)
    cfg = Config(
        settings=s,
        env=env_factory(
            enable_trading=True,
            enable_live_trading=True,
            exchange_sandbox=True,  # still on sandbox -> must block
            exchange_api_key="k",
            exchange_api_secret="s",
        ),
    )
    with live_released(True), pytest.raises(LiveTradingForbiddenError, match="sandbox"):
        validators.validate_mode_compatibility(cfg)


def test_live_mode_blocked_when_yaml_sandbox_on(env_factory, settings_factory, live_released):
    s = settings_factory(runtime__mode=Mode.LIVE, exchange__sandbox=True)
    cfg = Config(
        settings=s,
        env=env_factory(
            enable_trading=True,
            enable_live_trading=True,
            exchange_sandbox=None,
            exchange_api_key="k",
            exchange_api_secret="s",
        ),
    )
    with live_released(True), pytest.raises(LiveTradingForbiddenError, match="sandbox"):
        validators.validate_mode_compatibility(cfg)


def test_live_mode_blocked_when_credentials_missing(env_factory, settings_factory, live_released):
    s = settings_factory(runtime__mode=Mode.LIVE)
    cfg = Config(
        settings=s,
        env=env_factory(
            enable_trading=True,
            enable_live_trading=True,
            exchange_sandbox=False,
            exchange_api_key="",
            exchange_api_secret="",
        ),
    )
    with live_released(True), pytest.raises(LiveTradingForbiddenError, match="not set"):
        validators.validate_mode_compatibility(cfg)


def test_live_mode_requires_enable_trading(env_factory, settings_factory, live_released):
    s = settings_factory(runtime__mode=Mode.LIVE)
    cfg = Config(
        settings=s,
        env=env_factory(enable_trading=False),  # master switch off
    )
    with live_released(True), pytest.raises(LiveTradingForbiddenError, match="ENABLE_TRADING"):
        validators.validate_mode_compatibility(cfg)


def test_signal_only_passes_without_any_flags(env_factory, settings_factory):
    s = settings_factory(runtime__mode=Mode.SIGNAL_ONLY)
    cfg = Config(settings=s, env=env_factory())  # everything off
    validators.validate_mode_compatibility(cfg)  # must NOT raise


def test_paper_requires_enable_trading(env_factory, settings_factory):
    s = settings_factory(runtime__mode=Mode.PAPER)
    cfg = Config(settings=s, env=env_factory(enable_trading=False))
    with pytest.raises(LiveTradingForbiddenError, match="ENABLE_TRADING"):
        validators.validate_mode_compatibility(cfg)
