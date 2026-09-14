"""Contract test: portfolio mode must block live orders.

This test verifies the safety contract that portfolio mode cannot place live orders
unless ALL live-trading gates are satisfied (release flag, env flags, sandbox off,
credentials present). This is a critical safety requirement - portfolio mode must
never touch real capital without explicit release.
"""
from __future__ import annotations

import pytest

from crypto_bot.config.env import Config
from crypto_bot.core.enums import Mode, StrategyType
from crypto_bot.core.exceptions import LiveTradingForbiddenError
from crypto_bot.core.validators import validate_all, validate_mode_compatibility


def test_portfolio_live_mode_blocked_when_release_flag_off(
    env_factory, settings_factory, live_released
):
    """Portfolio LIVE mode blocked when LIVE_TRADING_RELEASED=False."""
    s = settings_factory(
        runtime__mode=Mode.LIVE,
        runtime__strategy_type=StrategyType.PORTFOLIO,
        portfolio__strategy_name="cross_sectional_momentum_v0",
    )
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
        validate_mode_compatibility(cfg)


def test_portfolio_live_mode_blocked_when_env_flag_off(
    env_factory, settings_factory, live_released
):
    """Portfolio LIVE mode blocked when ENABLE_LIVE_TRADING=false."""
    s = settings_factory(
        runtime__mode=Mode.LIVE,
        runtime__strategy_type=StrategyType.PORTFOLIO,
        portfolio__strategy_name="cross_sectional_momentum_v0",
    )
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
        validate_mode_compatibility(cfg)


def test_portfolio_live_mode_blocked_when_sandbox_on(
    env_factory, settings_factory, live_released
):
    """Portfolio LIVE mode blocked when sandbox/testnet endpoints are on."""
    s = settings_factory(
        runtime__mode=Mode.LIVE,
        runtime__strategy_type=StrategyType.PORTFOLIO,
        portfolio__strategy_name="cross_sectional_momentum_v0",
    )
    cfg = Config(
        settings=s,
        env=env_factory(
            enable_trading=True,
            enable_live_trading=True,
            exchange_sandbox=True,
            exchange_api_key="k",
            exchange_api_secret="s",
        ),
    )
    with live_released(True), pytest.raises(LiveTradingForbiddenError, match="sandbox"):
        validate_mode_compatibility(cfg)


def test_portfolio_live_mode_blocked_when_yaml_sandbox_on(
    env_factory, settings_factory, live_released
):
    """Portfolio LIVE mode blocked when YAML sandbox=true even if env doesn't override."""
    s = settings_factory(
        runtime__mode=Mode.LIVE,
        runtime__strategy_type=StrategyType.PORTFOLIO,
        portfolio__strategy_name="cross_sectional_momentum_v0",
        exchange__sandbox=True,
    )
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
        validate_mode_compatibility(cfg)


def test_portfolio_live_mode_blocked_when_credentials_missing(
    env_factory, settings_factory, live_released
):
    """Portfolio LIVE mode blocked when API credentials are not set."""
    s = settings_factory(
        runtime__mode=Mode.LIVE,
        runtime__strategy_type=StrategyType.PORTFOLIO,
        portfolio__strategy_name="cross_sectional_momentum_v0",
    )
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
        validate_mode_compatibility(cfg)


def test_portfolio_live_mode_requires_enable_trading(
    env_factory, settings_factory, live_released
):
    """Portfolio LIVE mode requires ENABLE_TRADING=true."""
    s = settings_factory(
        runtime__mode=Mode.LIVE,
        runtime__strategy_type=StrategyType.PORTFOLIO,
        portfolio__strategy_name="cross_sectional_momentum_v0",
    )
    cfg = Config(
        settings=s,
        env=env_factory(
            enable_trading=False,
            enable_live_trading=True,
            exchange_sandbox=False,
            exchange_api_key="k",
            exchange_api_secret="s",
        ),
    )
    with live_released(True), pytest.raises(LiveTradingForbiddenError, match="ENABLE_TRADING"):
        validate_mode_compatibility(cfg)


def test_portfolio_paper_mode_allowed_without_live_gate(
    env_factory, settings_factory
):
    """Portfolio PAPER mode is allowed without live gate (only needs ENABLE_TRADING)."""
    s = settings_factory(
        runtime__mode=Mode.PAPER,
        runtime__strategy_type=StrategyType.PORTFOLIO,
        portfolio__strategy_name="cross_sectional_momentum_v0",
    )
    cfg = Config(
        settings=s,
        env=env_factory(
            enable_trading=True,
            enable_live_trading=False,
            exchange_sandbox=True,
            exchange_api_key="",
            exchange_api_secret="",
        ),
    )
    validate_mode_compatibility(cfg)


def test_portfolio_signal_only_mode_allowed_without_flags(
    env_factory, settings_factory
):
    """Portfolio SIGNAL_ONLY mode is allowed without any trading flags."""
    s = settings_factory(
        runtime__mode=Mode.SIGNAL_ONLY,
        runtime__strategy_type=StrategyType.PORTFOLIO,
        portfolio__strategy_name="cross_sectional_momentum_v0",
    )
    cfg = Config(
        settings=s,
        env=env_factory(
            enable_trading=False,
            enable_live_trading=False,
            exchange_sandbox=True,
            exchange_api_key="",
            exchange_api_secret="",
        ),
    )
    validate_mode_compatibility(cfg)


def test_validate_all_includes_portfolio_live_gate(
    env_factory, settings_factory, live_released
):
    """Full validation suite blocks portfolio LIVE when gate not satisfied."""
    s = settings_factory(
        runtime__mode=Mode.LIVE,
        runtime__strategy_type=StrategyType.PORTFOLIO,
        portfolio__strategy_name="cross_sectional_momentum_v0",
    )
    cfg = Config(
        settings=s,
        env=env_factory(
            enable_trading=True,
            enable_live_trading=True,
            exchange_sandbox=False,
            exchange_api_key="k",
            exchange_api_secret="s",
        ),
    )
    with live_released(False), pytest.raises(LiveTradingForbiddenError):
        validate_all(cfg)
