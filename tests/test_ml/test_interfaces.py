"""Tests for ML interface stubs."""
from __future__ import annotations

from crypto_bot.core.enums import Signal
from crypto_bot.core.types import FeatureSet
from crypto_bot.ml.feature_pipeline import FeaturePipeline
from crypto_bot.ml.model_registry import ModelRegistry
from crypto_bot.ml.predictor import Predictor


def test_predictor_returns_hold_stub():
    pred = Predictor().predict({"symbol": "BTC/USDT"})
    assert pred.signal == Signal.HOLD
    assert pred.model_name == "stub"


def test_feature_pipeline_transforms_feature_set():
    fs = FeatureSet(
        symbol="BTC/USDT",
        timeframe="15m",
        trend_score=0.8,
        momentum_score=0.7,
        volatility_score=0.6,
        volume_score=0.5,
        adx=25,
        rsi=55,
        atr_pct=1.2,
        ema_fast=1,
        ema_mid=1,
        ema_slow=1,
        liquidity_score=0.9,
        spread_pct=0.05,
    )
    vector = FeaturePipeline().transform(fs)
    assert vector.symbol == "BTC/USDT"
    assert "trend_score" in vector.features
    assert vector.features["trend_score"] == 0.8


def test_model_registry():
    registry = ModelRegistry()
    assert registry.list_models() == []
