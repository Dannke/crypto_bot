"""CSM configuration (task 10): schema, duration parsing, factory wiring.

The config contract mirrors the task example:

    portfolio:
      strategy_name: cross_sectional_momentum_v0
      csm:
        timeframe: 1h
        lookbacks: ["24h", "72h", "168h"]
        long_percentile: 0.90
        short_percentile: 0.10
        weighting: equal
        rebalance_hours: 24
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from crypto_bot.config.env import Config
from crypto_bot.config.schemas import CsmConfig, Settings
from crypto_bot.core import policy, validators
from crypto_bot.core.exceptions import ConfigError
from crypto_bot.pipeline.factory import build_portfolio_strategy
from crypto_bot.strategy import CrossSectionalMomentumStrategy
from crypto_bot.strategy.portfolio_strategies import (
    MOMENTUM_V0_STRATEGY_NAME,
    LongOnlyTrendPortfolioStrategy,
)


# --------------------------------------------------------------------------- #
# Duration parsing (core.policy)
# --------------------------------------------------------------------------- #
class TestParseDuration:
    def test_parses_common_units(self) -> None:
        assert policy.parse_duration_seconds("30m") == 1800
        assert policy.parse_duration_seconds("24h") == 86_400
        assert policy.parse_duration_seconds("72h") == 259_200
        assert policy.parse_duration_seconds("168h") == 604_800
        assert policy.parse_duration_seconds("3d") == 259_200
        assert policy.parse_duration_seconds("1w") == 604_800

    def test_case_insensitive_and_whitespace_tolerant(self) -> None:
        assert policy.parse_duration_seconds(" 24H ") == 86_400

    @pytest.mark.parametrize("bad", ["5x", "", "24", "-1h", "0h", "h", None])
    def test_rejects_malformed_durations(self, bad) -> None:
        with pytest.raises(ValueError):
            policy.parse_duration_seconds(bad)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# CsmConfig schema
# --------------------------------------------------------------------------- #
def test_csm_defaults_match_the_example_contract():
    csm = CsmConfig()
    assert csm.timeframe == "1h"
    assert csm.lookbacks == ["24h"]
    assert csm.long_percentile == 0.90
    assert csm.short_percentile is None  # long-only by default
    assert csm.weighting == "equal"
    assert csm.rebalance_hours == 24


def test_csm_example_from_task_parses():
    csm = CsmConfig.model_validate({
        "timeframe": "1h",
        "lookbacks": ["24h", "72h", "168h"],
        "long_percentile": 0.90,
        "short_percentile": 0.10,
        "weighting": "equal",
        "rebalance_hours": 24,
    })
    assert csm.lookbacks == ["24h", "72h", "168h"]


def test_csm_rejects_bad_lookbacks():
    with pytest.raises(ValidationError):
        CsmConfig.model_validate({"lookbacks": []})
    with pytest.raises(ValidationError):
        CsmConfig.model_validate({"lookbacks": ["5x"]})
    with pytest.raises(ValidationError):
        CsmConfig.model_validate({"lookbacks": ["0h"]})
    # 90 minutes is not an integer number of 1h bars.
    with pytest.raises(ValidationError, match="integer multiple"):
        CsmConfig.model_validate({"lookbacks": ["90m"]})


def test_csm_rejects_bad_percentiles_and_ordering():
    with pytest.raises(ValidationError):
        CsmConfig(long_percentile=0.0)
    with pytest.raises(ValidationError):
        CsmConfig(long_percentile=1.0)
    with pytest.raises(ValidationError):
        CsmConfig(short_percentile=0.0)
    with pytest.raises(ValidationError, match="exceed"):
        CsmConfig(long_percentile=0.5, short_percentile=0.5)
    with pytest.raises(ValidationError, match="exceed"):
        CsmConfig(long_percentile=0.10, short_percentile=0.90)


def test_csm_rejects_non_equal_weighting():
    with pytest.raises(ValidationError):
        CsmConfig.model_validate({"weighting": "volatility_scaled"})


def test_csm_rejects_bad_timeframe_and_rebalance():
    with pytest.raises(ValidationError, match="timeframe"):
        CsmConfig.model_validate({"timeframe": "99m"})
    with pytest.raises(ValidationError):
        CsmConfig.model_validate({"rebalance_hours": 0})


# --------------------------------------------------------------------------- #
# Factory wiring
# --------------------------------------------------------------------------- #
def _settings(**csm_overrides) -> Settings:
    csm = {"lookbacks": ["24h", "72h", "168h"], "short_percentile": 0.10}
    csm.update(csm_overrides)
    return Settings.model_validate({
        "portfolio": {
            "strategy_name": MOMENTUM_V0_STRATEGY_NAME,
            "csm": csm,
        }
    })


def test_build_portfolio_strategy_constructs_csm_from_settings():
    strategy = build_portfolio_strategy(_settings())
    assert isinstance(strategy, CrossSectionalMomentumStrategy)
    assert strategy.lookbacks_bars == (24, 72, 168)
    assert strategy.top_fraction == pytest.approx(0.10)   # 1 - long_percentile
    assert strategy.short_fraction == pytest.approx(0.10)


def test_build_portfolio_strategy_long_only_when_short_omitted():
    strategy = build_portfolio_strategy(_settings(short_percentile=None))
    assert strategy.short_fraction is None
    assert strategy.top_fraction == pytest.approx(0.10)


def test_build_portfolio_strategy_trend_default():
    strategy = build_portfolio_strategy(Settings())
    assert isinstance(strategy, LongOnlyTrendPortfolioStrategy)


def test_build_portfolio_strategy_rejects_unknown_name():
    settings = Settings.model_validate({"portfolio": {"strategy_name": "nope"}})
    with pytest.raises(ConfigError, match="unsupported portfolio strategy"):
        build_portfolio_strategy(settings)


# --------------------------------------------------------------------------- #
# Cross-field validator
# --------------------------------------------------------------------------- #
def test_csm_lookbacks_must_fit_candle_history(settings_factory, env_factory):
    s = settings_factory(
        portfolio={
            "strategy_name": MOMENTUM_V0_STRATEGY_NAME,
            "csm": {"lookbacks": ["24h", "168h"]},
        },
        timeframes__candles_per_tf=100,
    )
    cfg = Config(settings=s, env=env_factory())
    with pytest.raises(ConfigError, match="candles_per_tf"):
        validators.validate_portfolio_csm(cfg)


def test_csm_lookbacks_fit_is_accepted(settings_factory, env_factory):
    s = settings_factory(
        portfolio={
            "strategy_name": MOMENTUM_V0_STRATEGY_NAME,
            "csm": {"lookbacks": ["24h", "168h"]},
        },
        timeframes__candles_per_tf=200,
    )
    cfg = Config(settings=s, env=env_factory())
    validators.validate_portfolio_csm(cfg)  # 168h on 1h = 168 bars + 1 <= 200


def test_csm_validator_is_noop_for_other_strategies(settings_factory, env_factory):
    s = settings_factory(timeframes__candles_per_tf=100)
    cfg = Config(settings=s, env=env_factory())
    validators.validate_portfolio_csm(cfg)  # default long_only_trend: no check
