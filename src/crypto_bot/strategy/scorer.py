"""Composite scorer.

Turns a FeatureSet + SignalResult into a 0..100 score and a fully priced
ScoredCandidate (entry/stop/take + risk%). The scoring weights come from the
StrategyContext; their sum is normalised so re-tuning weights never breaks the
0..100 scale.

Entry/stop/take math:
  * entry = latest close (passed via features.extras or fallback to ema_fast)
  * stop  = entry -/+ stop_distance, where distance = min(max_stop_distance_pct, atr*1.5)
  * take  = entry +/- (stop_distance * take_profit_risk_multiple)

The stop is placed at the tighter of the configured cap and 1.5*ATR. This keeps
risk per trade bounded while still respecting recent volatility.
"""
from __future__ import annotations

from typing import Any

from ..core.enums import Side
from ..core.types import FeatureSet, ScoredCandidate, SignalResult
from .base import StrategyContext


class Scorer:
    def __init__(self, ctx: StrategyContext, max_stop_distance_pct: float) -> None:
        self._ctx = ctx
        self._max_stop_pct = max_stop_distance_pct

    def _raw_score(self, f: FeatureSet) -> float:
        w = self._ctx.scoring_weights
        spread_w = w.get("spread", w.get("setup", 0.05))
        risk_w = w.get("risk", w.get("reward_risk", 0.15))
        total_w = w["trend"] + w["momentum"] + w["volume"] + spread_w + risk_w + w["liquidity"]
        s = (
            w["trend"] * f.trend_score
            + w["momentum"] * f.momentum_score
            + w["volume"] * f.volume_score
            + spread_w * min(1.0, 1.0 - f.spread_pct)
            + risk_w * (1.0 if f.market_regime != "choppy" else 0.3)
            + w["liquidity"] * f.liquidity_score
        )
        return s / total_w if total_w > 0 else 0.0

    def score_candidate(self, features: FeatureSet, signal: SignalResult) -> ScoredCandidate | None:
        if signal.signal.value == "HOLD" or signal.side is None:
            return None
        if signal.confidence < self._ctx.min_confidence:
            return None
        raw = self._raw_score(features)
        score = round(raw * 100.0, 2)
        if score < self._ctx.min_score:
            return None

        entry, stop, take, risk_pct = self._levels(features, signal.side)

        return ScoredCandidate(
            symbol=features.symbol,
            signal=signal.signal,
            side=signal.side,
            score=score,
            confidence=signal.confidence,
            entry=entry,
            stop=stop,
            take=take,
            risk_pct=risk_pct,
            features={
                "trend": features.trend_score,
                "momentum": features.momentum_score,
                "volume": features.volume_score,
                "volatility": features.volatility_score,
                "adx": features.adx,
                "rsi": features.rsi,
                "atr_pct": features.atr_pct,
            },
        )

    def _levels(self, f: FeatureSet, side: Side) -> tuple[float, float, float, float]:
        # Entry: prefer latest close if provided via extras; else approximate by ema_fast.
        entry = f.extras.get("last_close", f.ema_fast)
        if entry != entry:  # NaN guard
            entry = f.ema_mid if f.ema_mid == f.ema_mid else 0.0

        # Stop distance: tighter of (max cap, 1.5 * ATR%).
        atr_distance_pct = min(self._max_stop_pct, 1.5 * f.atr_pct)
        if atr_distance_pct <= 0 or atr_distance_pct != atr_distance_pct:
            atr_distance_pct = self._max_stop_pct

        dist = entry * (atr_distance_pct / 100.0)
        if side == Side.LONG:
            stop = entry - dist
            take = entry + dist * self._ctx.take_profit_risk_multiple
        else:
            stop = entry + dist
            take = entry - dist * self._ctx.take_profit_risk_multiple
        return entry, round(stop, 8), round(take, 8), round(atr_distance_pct, 4)

    def score(self, features: FeatureSet, signal_result: Any) -> ScoredCandidate | None:
        # Strategy ABC conformance.
        return self.score_candidate(features, signal_result)


def select_top_candidates(
    candidates: list[ScoredCandidate],
    max_count: int,
) -> list[ScoredCandidate]:
    """Return the highest-score candidates, stable for ties."""
    if max_count <= 0:
        return []
    return sorted(
        candidates,
        key=lambda c: (c.score, c.confidence),
        reverse=True,
    )[:max_count]
