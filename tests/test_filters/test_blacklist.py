"""Tests for BlacklistFilter."""
from crypto_bot.core.types import FeatureSet
from crypto_bot.filters.blacklist import BlacklistFilter


def test_blacklist_filter_pass():
    filter_instance = BlacklistFilter(blacklist={"USDC/USDT", "BTCUP/USDT"})
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
    )
    result = filter_instance.evaluate(features)
    assert result.passed


def test_blacklist_filter_reject():
    filter_instance = BlacklistFilter(blacklist={"USDC/USDT", "BTCUP/USDT"})
    features = FeatureSet(
        symbol="USDC/USDT",
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
    )
    result = filter_instance.evaluate(features)
    assert not result.passed
    assert result.reason == "blacklisted"


def test_blacklist_add_remove():
    filter_instance = BlacklistFilter(blacklist={"USDC/USDT"})
    
    features = FeatureSet(
        symbol="BTCUP/USDT",
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
    )
    
    # Initially passes
    result = filter_instance.evaluate(features)
    assert result.passed
    
    # Add to blacklist
    filter_instance.add_to_blacklist("BTCUP/USDT")
    result = filter_instance.evaluate(features)
    assert not result.passed
    
    # Remove from blacklist
    filter_instance.remove_from_blacklist("BTCUP/USDT")
    result = filter_instance.evaluate(features)
    assert result.passed
