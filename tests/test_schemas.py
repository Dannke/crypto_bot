"""Tests for the pydantic configuration contract (stage 1 schemas).

These verify structural validation: bad ranges, ordering invariants and the
``extra='forbid'`` policy. Cross-field/policy checks live in test_validators.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from crypto_bot.config.schemas import (
    RiskParams,
    ScoringWeights,
    Settings,
    TimeframesConfig,
    TrendParams,
    VolatilityParams,
)
from crypto_bot.core.enums import StrategyType


def test_default_settings_are_valid():
    s = Settings()
    assert s.runtime.mode.value == "signal_only"
    assert s.scoring.min_score == 65.0
    assert s.runtime.strategy_type == StrategyType.CANDIDATE


def test_runtime_strategy_type_is_limited_to_candidate_or_portfolio():
    settings = Settings.model_validate({"runtime": {"strategy_type": "portfolio"}})
    assert settings.runtime.strategy_type == StrategyType.PORTFOLIO
    with pytest.raises(ValidationError):
        Settings.model_validate({"runtime": {"strategy_type": "unknown"}})


def test_unknown_yaml_key_is_rejected():
    with pytest.raises(ValidationError):
        Settings.model_validate({"runtime": {"mode": "signal_only"}, "nope": 1})


def test_unknown_nested_yaml_key_is_rejected():
    with pytest.raises(ValidationError):
        Settings.model_validate(
            {
                "runtime": {"mode": "signal_only", "surprise": True},
            }
        )


def test_trend_emas_must_be_strictly_ordered():
    with pytest.raises(ValidationError):
        TrendParams(ema_fast=50, ema_mid=50, ema_slow=200)
    with pytest.raises(ValidationError):
        TrendParams(ema_fast=100, ema_mid=50, ema_slow=200)


def test_volatility_atr_band_must_be_ordered():
    with pytest.raises(ValidationError):
        VolatilityParams(atr_min_pct=5.0, atr_max_pct=5.0)
    with pytest.raises(ValidationError):
        VolatilityParams(atr_min_pct=6.0, atr_max_pct=2.0)


def test_scoring_weights_must_sum_positive():
    # All-zero weights sum to 0, which the model_validator rejects. The check
    # fires at construction time (ScoringWeights), so we expect it there.
    with pytest.raises(ValidationError):
        ScoringWeights(
            trend=0,
            momentum=0,
            volume=0,
            volatility=0,
            liquidity=0,
            spread=0,
            risk=0,
        )


def test_risk_drawdown_ladder_must_ascend():
    with pytest.raises(ValidationError):
        RiskParams(max_daily_drawdown_pct=5.0, emergency_drawdown_pct=5.0)
    with pytest.raises(ValidationError):
        RiskParams(max_daily_drawdown_pct=7.0, emergency_drawdown_pct=6.0)


def test_timeframes_non_empty():
    with pytest.raises(ValidationError):
        TimeframesConfig(primary=[])


def test_risk_per_trade_capped():
    # 1% is the default and valid; 10% exceeds the cap.
    with pytest.raises(ValidationError):
        RiskParams(risk_per_trade_pct=10.0)


def test_portfolio_risk_params_defaults_are_sane():
    from crypto_bot.config.schemas import PortfolioRiskParams

    limits = PortfolioRiskParams()
    assert limits.max_positions == 5
    assert limits.max_position_weight == 0.5
    assert limits.max_gross_exposure == 1.0
    assert limits.max_net_exposure == 1.0


def test_portfolio_risk_params_bounds_are_enforced():
    from crypto_bot.config.schemas import PortfolioRiskParams

    with pytest.raises(ValidationError):
        PortfolioRiskParams(max_positions=0)
    with pytest.raises(ValidationError):
        PortfolioRiskParams(max_position_weight=1.5)
    with pytest.raises(ValidationError):
        PortfolioRiskParams(max_gross_exposure=4.0)


def test_portfolio_risk_limits_dataclass_requires_all_fields():
    from crypto_bot.portfolio import PortfolioRiskLimits

    with pytest.raises(TypeError):
        PortfolioRiskLimits(max_positions=5, max_position_weight=0.3, max_gross_exposure=1.5)
    with pytest.raises(ValueError):
        PortfolioRiskLimits(
            max_positions=-1,
            max_position_weight=0.3,
            max_gross_exposure=1.5,
            max_net_exposure=1.5,
        )


def test_settings_portfolio_section_accepts_risk_limits_and_validates_them():
    from crypto_bot.config.schemas import PortfolioConfig

    cfg = PortfolioConfig.model_validate({
        "strategy_name": "long_only_trend",
        "volatility_sizing": True,
        "risk": {"max_positions": 8, "max_position_weight": 0.25},
    })
    assert cfg.risk.max_positions == 8
    assert cfg.risk.max_position_weight == 0.25

    with pytest.raises(ValidationError):
        PortfolioConfig.model_validate({"risk": {"max_position_weight": 2.0}})
