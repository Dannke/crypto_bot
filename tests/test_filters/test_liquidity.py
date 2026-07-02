"""Tests for LiquidityFilter."""
from crypto_bot.core.types import FeatureSet
from crypto_bot.filters.liquidity import LiquidityFilter


def test_liquidity_filter_pass():
    filter_instance = LiquidityFilter(min_liquidity_score=0.3)
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
        liquidity_score=0.8,
    )
    result = filter_instance.evaluate(features)
    assert result.passed


def test_liquidity_filter_reject_low_score():
    filter_instance = LiquidityFilter(min_liquidity_score=0.5)
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
        liquidity_score=0.3,
    )
    result = filter_instance.evaluate(features)
    assert not result.passed
    assert result.reason == "insufficient_liquidity"
