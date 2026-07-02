"""Tests for VolatilityFilter."""
from crypto_bot.core.types import FeatureSet
from crypto_bot.filters.volatility import VolatilityFilter


def test_volatility_filter_pass():
    filter_instance = VolatilityFilter(min_atr_pct=0.5, max_atr_pct=8.0)
    features = FeatureSet(
        symbol="BTC/USDT",
        timeframe="15m",
        trend_score=0.8,
        momentum_score=0.7,
        volatility_score=0.6,
        volume_score=0.9,
        adx=25.0,
        rsi=55.0,
        atr_pct=2.0,
        ema_fast=100.0,
        ema_mid=99.0,
        ema_slow=98.0,
    )
    result = filter_instance.evaluate(features)
    assert result.passed


def test_volatility_filter_reject_low():
    filter_instance = VolatilityFilter(min_atr_pct=0.5, max_atr_pct=8.0)
    features = FeatureSet(
        symbol="BTC/USDT",
        timeframe="15m",
        trend_score=0.8,
        momentum_score=0.7,
        volatility_score=0.6,
        volume_score=0.9,
        adx=25.0,
        rsi=55.0,
        atr_pct=0.2,
        ema_fast=100.0,
        ema_mid=99.0,
        ema_slow=98.0,
    )
    result = filter_instance.evaluate(features)
    assert not result.passed
    assert result.reason == "volatility_too_low"


def test_volatility_filter_reject_high():
    filter_instance = VolatilityFilter(min_atr_pct=0.5, max_atr_pct=8.0)
    features = FeatureSet(
        symbol="BTC/USDT",
        timeframe="15m",
        trend_score=0.8,
        momentum_score=0.7,
        volatility_score=0.6,
        volume_score=0.9,
        adx=25.0,
        rsi=55.0,
        atr_pct=10.0,
        ema_fast=100.0,
        ema_mid=99.0,
        ema_slow=98.0,
    )
    result = filter_instance.evaluate(features)
    assert not result.passed
    assert result.reason == "volatility_too_high"
