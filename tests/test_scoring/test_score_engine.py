"""Tests for ScoreEngine."""
from __future__ import annotations

from crypto_bot.core.types import FeatureSet
from crypto_bot.scoring.score_engine import ScoreEngine
from crypto_bot.scoring.weights import ScoringWeights


def _features(**overrides) -> FeatureSet:
    base = dict(
        symbol="BTC/USDT",
        timeframe="15m",
        trend_score=0.9,
        momentum_score=0.8,
        volatility_score=0.7,
        volume_score=0.8,
        adx=30.0,
        rsi=55.0,
        atr_pct=1.5,
        ema_fast=100.0,
        ema_mid=99.0,
        ema_slow=98.0,
        liquidity_score=0.8,
        spread_pct=0.05,
        correlation_btc=0.5,
        market_regime="bull",
    )
    base.update(overrides)
    return FeatureSet(**base)


def test_score_engine_returns_bounded_total():
    engine = ScoreEngine()
    result = engine.compute(_features())
    assert 0.0 <= result.total_score <= 100.0
    assert result.symbol == "BTC/USDT"


def test_score_engine_respects_weights():
    heavy_trend = ScoringWeights(trend=1.0, momentum=0.0, volume=0.0, volatility=0.0, liquidity=0.0, spread=0.0, risk=0.0)
    heavy_momentum = ScoringWeights(trend=0.0, momentum=1.0, volume=0.0, volatility=0.0, liquidity=0.0, spread=0.0, risk=0.0)
    f = _features(trend_score=0.9, momentum_score=0.1)
    trend_score = ScoreEngine(weights=heavy_trend.normalized()).compute(f).total_score
    momentum_score = ScoreEngine(weights=heavy_momentum.normalized()).compute(f).total_score
    assert trend_score > momentum_score


def test_choppy_regime_lowers_risk_subscore():
    engine = ScoreEngine()
    calm = engine.compute(_features(market_regime="bull")).sub_scores.risk
    choppy = engine.compute(_features(market_regime="choppy")).sub_scores.risk
    assert calm > choppy
