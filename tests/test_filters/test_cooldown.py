"""Tests for CooldownFilter."""
from datetime import UTC, datetime, timedelta

from crypto_bot.core.types import FeatureSet
from crypto_bot.filters.cooldown import CooldownFilter


def test_cooldown_filter_pass_no_history():
    filter_instance = CooldownFilter(cooldown_minutes=60)
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


def test_cooldown_filter_pass_expired():
    filter_instance = CooldownFilter(cooldown_minutes=60)
    last_trade_time = {
        "BTC/USDT": datetime.now(tz=UTC) - timedelta(minutes=120),
    }
    filter_instance = CooldownFilter(cooldown_minutes=60, last_trade_time=last_trade_time)
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


def test_cooldown_filter_reject_active():
    filter_instance = CooldownFilter(cooldown_minutes=60)
    last_trade_time = {
        "BTC/USDT": datetime.now(tz=UTC) - timedelta(minutes=30),
    }
    filter_instance = CooldownFilter(cooldown_minutes=60, last_trade_time=last_trade_time)
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
    assert not result.passed
    assert result.reason == "in_cooldown"


def test_cooldown_update_last_trade_time():
    filter_instance = CooldownFilter(cooldown_minutes=60)
    now = datetime.now(tz=UTC)
    filter_instance.update_last_trade_time("BTC/USDT", now)
    
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
    assert not result.passed
