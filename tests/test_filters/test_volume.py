"""Tests for VolumeFilter."""
from crypto_bot.core.types import FeatureSet
from crypto_bot.filters.volume import VolumeFilter


def test_volume_filter_pass():
    filter_instance = VolumeFilter(min_volume_score=0.5, min_absolute_volume=1000.0)
    features = FeatureSet(
        symbol="BTC/USDT",
        timeframe="15m",
        trend_score=0.8,
        momentum_score=0.7,
        volatility_score=0.6,
        volume_score=0.9,
        adx=25.0,
        rsi=55.0,
        atr_pct=1.5,
        ema_fast=100.0,
        ema_mid=99.0,
        ema_slow=98.0,
        volume=5000.0,
    )
    result = filter_instance.evaluate(features)
    assert result.passed


def test_volume_filter_reject_low_score():
    filter_instance = VolumeFilter(min_volume_score=0.5, min_absolute_volume=1000.0)
    features = FeatureSet(
        symbol="BTC/USDT",
        timeframe="15m",
        trend_score=0.8,
        momentum_score=0.7,
        volatility_score=0.6,
        volume_score=0.3,
        adx=25.0,
        rsi=55.0,
        atr_pct=1.5,
        ema_fast=100.0,
        ema_mid=99.0,
        ema_slow=98.0,
        volume=5000.0,
    )
    result = filter_instance.evaluate(features)
    assert not result.passed
    assert result.reason == "low_volume_score"


def test_volume_filter_reject_low_absolute():
    filter_instance = VolumeFilter(min_volume_score=0.5, min_absolute_volume=1000.0)
    features = FeatureSet(
        symbol="BTC/USDT",
        timeframe="15m",
        trend_score=0.8,
        momentum_score=0.7,
        volatility_score=0.6,
        volume_score=0.9,
        adx=25.0,
        rsi=55.0,
        atr_pct=1.5,
        ema_fast=100.0,
        ema_mid=99.0,
        ema_slow=98.0,
        volume=500.0,
    )
    result = filter_instance.evaluate(features)
    assert not result.passed
    assert result.reason == "low_absolute_volume"
