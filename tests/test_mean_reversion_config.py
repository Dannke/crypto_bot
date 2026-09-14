"""Mean Reversion configuration (Task 1): schema, duration parsing, factory wiring.

The config contract mirrors the pre-registration example:

    portfolio:
      strategy_name: mean_reversion_v0
      mean_reversion:
        timeframe: 1h
        zscore_window_bars: 48
        signal_lookback: "4h"
        entry_threshold: 2.0
        exit_threshold: 0.5
        max_holding_bars: 24
        weighting: inverse_vol
        rebalance_hours: 1
        seed: 42
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from crypto_bot.config.env import Config
from crypto_bot.config.schemas import MeanReversionConfig, Settings
from crypto_bot.core import policy, validators
from crypto_bot.core.exceptions import ConfigError
from crypto_bot.pipeline.factory import build_portfolio_strategy
from crypto_bot.strategy import MeanReversionStrategy
from crypto_bot.strategy.portfolio_strategies import MEAN_REVERSION_V0_STRATEGY_NAME


# --------------------------------------------------------------------------- #
# MeanReversionConfig schema
# --------------------------------------------------------------------------- #
def test_mr_defaults_match_the_example_contract():
    mr = MeanReversionConfig()
    assert mr.timeframe == "1h"
    assert mr.zscore_window_bars == 48
    assert mr.signal_lookback == "4h"
    assert mr.entry_threshold == 2.0
    assert mr.exit_threshold == 0.5
    assert mr.max_holding_bars == 24
    assert mr.weighting == "inverse_vol"
    assert mr.rebalance_hours == 1


def test_mr_example_from_preregistration_parses():
    mr = MeanReversionConfig.model_validate({
        "timeframe": "1h",
        "zscore_window_bars": 48,
        "signal_lookback": "4h",
        "entry_threshold": 2.0,
        "exit_threshold": 0.5,
        "max_holding_bars": 24,
        "weighting": "inverse_vol",
        "rebalance_hours": 1,
        "seed": 42,
    })
    assert mr.zscore_window_bars == 48
    assert mr.signal_lookback == "4h"


def test_mr_rejects_bad_zscore_window():
    with pytest.raises(ValidationError):
        MeanReversionConfig.model_validate({"zscore_window_bars": 5})
    with pytest.raises(ValidationError):
        MeanReversionConfig.model_validate({"zscore_window_bars": 0})


def test_mr_rejects_bad_thresholds():
    with pytest.raises(ValidationError):
        MeanReversionConfig.model_validate({"entry_threshold": 0.0})
    with pytest.raises(ValidationError):
        MeanReversionConfig.model_validate({"entry_threshold": -1.0})
    with pytest.raises(ValidationError):
        MeanReversionConfig.model_validate({"exit_threshold": -0.1})
    # entry must exceed exit
    with pytest.raises(ValidationError, match="entry_threshold must exceed exit_threshold"):
        MeanReversionConfig.model_validate({"entry_threshold": 1.0, "exit_threshold": 1.0})
    with pytest.raises(ValidationError, match="entry_threshold must exceed exit_threshold"):
        MeanReversionConfig.model_validate({"entry_threshold": 0.5, "exit_threshold": 1.0})


def test_mr_rejects_bad_max_holding():
    with pytest.raises(ValidationError):
        MeanReversionConfig.model_validate({"max_holding_bars": 0})


def test_mr_rejects_bad_weighting():
    with pytest.raises(ValidationError):
        MeanReversionConfig.model_validate({"weighting": "volatility_scaled"})


def test_mr_rejects_bad_timeframe_and_rebalance():
    with pytest.raises(ValidationError, match="timeframe"):
        MeanReversionConfig.model_validate({"timeframe": "99m"})
    with pytest.raises(ValidationError):
        MeanReversionConfig.model_validate({"rebalance_hours": 0})


def test_mr_signal_lookback_must_align_with_timeframe():
    # 90m is not an integer multiple of 1h
    with pytest.raises(ValidationError, match="integer multiple"):
        MeanReversionConfig.model_validate({"timeframe": "1h", "signal_lookback": "90m"})
    # signal_lookback must be >= rebalance_hours * timeframe
    with pytest.raises(ValidationError, match="rebalance_hours"):
        MeanReversionConfig.model_validate({
            "timeframe": "1h",
            "signal_lookback": "1h",
            "rebalance_hours": 2,
        })


# --------------------------------------------------------------------------- #
# Factory wiring
# --------------------------------------------------------------------------- #
def _settings(**mr_overrides) -> Settings:
    mr = {
        "timeframe": "1h",
        "zscore_window_bars": 48,
        "signal_lookback": "4h",
        "entry_threshold": 2.0,
        "exit_threshold": 0.5,
        "max_holding_bars": 24,
        "weighting": "inverse_vol",
        "rebalance_hours": 1,
        "seed": 42,
    }
    mr.update(mr_overrides)
    return Settings.model_validate({
        "portfolio": {
            "strategy_name": MEAN_REVERSION_V0_STRATEGY_NAME,
            "mean_reversion": mr,
        }
    })


def test_build_portfolio_strategy_constructs_mr_from_settings():
    strategy = build_portfolio_strategy(_settings())
    assert isinstance(strategy, MeanReversionStrategy)
    assert strategy.zscore_window_bars == 48
    assert strategy.signal_lookback_bars == 4  # 4h on 1h tf
    assert strategy.entry_threshold == 2.0
    assert strategy.exit_threshold == 0.5
    assert strategy.max_holding_bars == 24
    assert strategy.weighting == "inverse_vol"


def test_build_portfolio_strategy_mr_with_equal_weighting():
    strategy = build_portfolio_strategy(_settings(weighting="equal"))
    assert strategy.weighting == "equal"


def test_build_portfolio_strategy_mr_different_timeframe():
    strategy = build_portfolio_strategy(_settings(timeframe="4h", signal_lookback="16h"))
    assert strategy.signal_lookback_bars == 4  # 16h on 4h tf


# --------------------------------------------------------------------------- #
# Cross-field validator (candles_per_tf check)
# --------------------------------------------------------------------------- #
def test_mr_validator_rejects_insufficient_candles(settings_factory, env_factory):
    s = settings_factory(
        portfolio={
            "strategy_name": MEAN_REVERSION_V0_STRATEGY_NAME,
            "mean_reversion": {
                "timeframe": "1h",
                "zscore_window_bars": 48,
                "signal_lookback": "4h",
            },
        },
        timeframes__candles_per_tf=50,  # 50 is the minimum allowed by schema
    )
    cfg = Config(settings=s, env=env_factory())
    # The validator checks if candles_per_tf >= window_bars + signal_lookback_bars + 1
    # 50 < 48 + 4 + 1 = 53, so it should fail
    with pytest.raises(ConfigError, match="candles_per_tf"):
        validators.validate_portfolio_csm(cfg)


def test_mr_validator_accepts_sufficient_candles(settings_factory, env_factory):
    s = settings_factory(
        portfolio={
            "strategy_name": MEAN_REVERSION_V0_STRATEGY_NAME,
            "mean_reversion": {
                "timeframe": "1h",
                "zscore_window_bars": 48,
                "signal_lookback": "4h",
            },
        },
        timeframes__candles_per_tf=60,
    )
    cfg = Config(settings=s, env=env_factory())
    validators.validate_portfolio_csm(cfg)


def test_mr_validator_is_noop_for_other_strategies(settings_factory, env_factory):
    s = settings_factory(timeframes__candles_per_tf=100)
    cfg = Config(settings=s, env=env_factory())
    validators.validate_portfolio_csm(cfg)  # default long_only_trend: no check