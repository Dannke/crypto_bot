"""Tests for RankingEngine."""
from __future__ import annotations

from crypto_bot.core.types import FeatureSet
from crypto_bot.scoring.ranking import RankingEngine
from crypto_bot.scoring.score_engine import ScoreEngine, ScoreResult


def test_rank_orders_by_score_descending():
    engine = ScoreEngine()
    f1 = FeatureSet(
        symbol="A", timeframe="15m", trend_score=1, momentum_score=1,
        volatility_score=1, volume_score=1, adx=30, rsi=55, atr_pct=1,
        ema_fast=1, ema_mid=1, ema_slow=1, liquidity_score=0.8,
    )
    f2 = FeatureSet(
        symbol="B", timeframe="15m", trend_score=0.2, momentum_score=0.2,
        volatility_score=0.2, volume_score=0.2, adx=30, rsi=55, atr_pct=1,
        ema_fast=1, ema_mid=1, ema_slow=1, liquidity_score=0.8,
    )
    s1 = engine.compute(f1)
    s2 = engine.compute(f2)
    ranked = RankingEngine().rank([s2, s1], max_count=2)
    assert [r.symbol for r in ranked] == ["A", "B"]


def test_rank_with_features_preserves_pairs():
    engine = ScoreEngine()
    f = FeatureSet(
        symbol="BTC/USDT", timeframe="15m", trend_score=0.8, momentum_score=0.8,
        volatility_score=0.8, volume_score=0.8, adx=30, rsi=55, atr_pct=1,
        ema_fast=1, ema_mid=1, ema_slow=1, liquidity_score=0.8,
    )
    score = engine.compute(f)
    ranked = RankingEngine().rank_with_features([(score, f)], max_count=1)
    assert ranked[0][0].symbol == "BTC/USDT"
    assert ranked[0][1].symbol == "BTC/USDT"
