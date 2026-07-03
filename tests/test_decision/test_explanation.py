"""Tests for decision reporting and explanations."""
from __future__ import annotations

from crypto_bot.core.enums import RejectReason, Side, Signal
from crypto_bot.core.types import FeatureSet
from crypto_bot.decision.decision_report import DecisionReport
from crypto_bot.decision.explanation import ExplanationGenerator
from crypto_bot.filters.base import FilterOutcome, FilterResult
from crypto_bot.scoring.score_engine import ScoreEngine


def _feature() -> FeatureSet:
    return FeatureSet(
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
        extras={"last_close": 100.0},
    )


def test_explanation_for_accepted_decision():
    score = ScoreEngine().compute(_feature())
    report = DecisionReport.from_score_result(
        score_result=score,
        signal=Signal.BUY,
        side=Side.LONG,
        confidence=0.9,
        features=_feature(),
        strategy_name="SignalEngine",
    )
    text = ExplanationGenerator().generate(report)
    assert "BUY" in text
    assert "Total Score" in text
    assert "trend" in text


def test_explanation_for_rejected_decision():
    fr = FilterResult("spread", FilterOutcome.REJECT, reason="spread_too_wide", detail="too wide")
    report = DecisionReport.rejected_report(
        symbol="ETH/USDT",
        reject_reason=RejectReason.SPREAD_TOO_WIDE,
        filter_result=fr,
    )
    text = ExplanationGenerator().generate(report)
    assert "spread" in text.lower()
    assert "REJECT" in ExplanationGenerator().generate_summary(report)
