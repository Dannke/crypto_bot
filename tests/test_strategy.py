"""Strategy scaffold tests."""
from __future__ import annotations

from crypto_bot.core.enums import Side, Signal
from crypto_bot.core.types import FeatureSet, SignalResult
from crypto_bot.strategy import Scorer, SignalEngine, StrategyContext, select_top_candidates


def _ctx(min_score: float = 65.0, min_confidence: float = 0.6) -> StrategyContext:
    return StrategyContext(
        scoring_weights={
            "trend": 0.25,
            "momentum": 0.15,
            "volume": 0.15,
            "spread": 0.05,
            "risk": 0.20,
            "liquidity": 0.10,
        },
        min_score=min_score,
        min_confidence=min_confidence,
        adx_min=20.0,
        rsi_long=(50.0, 70.0),
        rsi_short=(30.0, 50.0),
        atr_min_pct=0.5,
        atr_max_pct=8.0,
        take_profit_risk_multiple=2.0,
        max_stop_distance_pct=3.0,
    )


def _feature(tf: str, ema_sign: float, rsi: float = 55.0, adx: float = 30.0) -> FeatureSet:
    return FeatureSet(
        symbol="BTC/USDT",
        timeframe=tf,
        trend_score=1.0,
        momentum_score=0.8,
        volatility_score=0.7,
        volume_score=0.9,
        adx=adx,
        rsi=rsi,
        atr_pct=1.0,
        ema_fast=100.0,
        ema_mid=99.0,
        ema_slow=98.0,
        extras={"ema_state": ema_sign, "last_close": 100.0},
    )


def test_signal_engine_requires_full_timeframe_agreement():
    engine = SignalEngine(_ctx(), ["15m", "1h", "4h"])
    result = engine.evaluate(
        "BTC/USDT",
        {
            "15m": _feature("15m", 1.0),
            "1h": _feature("1h", 1.0),
            "4h": _feature("4h", 0.0),
        },
    )
    assert result.signal == Signal.HOLD
    assert result.reason == "partial timeframe agreement"


def test_signal_engine_full_long_confluence():
    engine = SignalEngine(_ctx(), ["15m", "1h", "4h"])
    result = engine.evaluate(
        "BTC/USDT",
        {
            "15m": _feature("15m", 1.0),
            "1h": _feature("1h", 1.0),
            "4h": _feature("4h", 1.0),
        },
    )
    assert result.signal == Signal.BUY
    assert result.side == Side.LONG
    assert result.confidence == 1.0


def test_scorer_filters_low_confidence_and_low_score():
    feature = _feature("15m", 1.0)
    scorer = Scorer(_ctx(min_score=95.0, min_confidence=0.9), max_stop_distance_pct=3.0)
    low_conf = SignalResult(
        symbol="BTC/USDT",
        signal=Signal.BUY,
        side=Side.LONG,
        confidence=0.5,
        by_timeframe={},
        reason="test",
    )
    assert scorer.score_candidate(feature, low_conf) is None

    high_conf = SignalResult(
        symbol="BTC/USDT",
        signal=Signal.BUY,
        side=Side.LONG,
        confidence=1.0,
        by_timeframe={},
        reason="test",
    )
    assert scorer.score_candidate(feature, high_conf) is None


def test_select_top_candidates_orders_by_score_then_confidence():
    feature = _feature("15m", 1.0)
    scorer = Scorer(_ctx(min_score=0.0, min_confidence=0.0), max_stop_distance_pct=3.0)
    signal = SignalResult(
        symbol="BTC/USDT",
        signal=Signal.BUY,
        side=Side.LONG,
        confidence=1.0,
        by_timeframe={},
        reason="test",
    )
    one = scorer.score_candidate(feature, signal)
    two = scorer.score_candidate(feature, signal)
    assert one is not None and two is not None
    two.symbol = "ETH/USDT"
    two.score = one.score + 1

    assert [c.symbol for c in select_top_candidates([one, two], max_count=1)] == ["ETH/USDT"]
