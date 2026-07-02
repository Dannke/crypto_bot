"""Tests for SpreadFilter."""
from crypto_bot.core.types import FeatureSet
from crypto_bot.filters.spread import SpreadFilter


def test_spread_filter_pass():
    filter_instance = SpreadFilter(max_spread_pct=0.5)
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
        spread_pct=0.1,
    )
    result = filter_instance.evaluate(features)
    assert result.passed


def test_spread_filter_reject_wide():
    filter_instance = SpreadFilter(max_spread_pct=0.5)
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
        spread_pct=1.0,
    )
    result = filter_instance.evaluate(features)
    assert not result.passed
    assert result.reason == "spread_too_wide"
