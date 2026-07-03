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


def test_default_settings_are_valid():
    s = Settings()
    assert s.runtime.mode.value == "signal_only"
    assert s.scoring.min_score == 65.0


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
