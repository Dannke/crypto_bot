"""Signal engine: multi-timeframe confluence -> BUY / SELL / HOLD.

Logic (confluence model):
  * trend tf (slowest)  -> directional bias (EMA stack + ADX strength)
  * confirmation tf     -> momentum agrees with bias (RSI in the right zone)
  * trigger tf (fastest)-> entry trigger (pullback resuming / fresh impulse)

The three timeframes must AGREE in direction. Any conflict -> HOLD with a low
confidence, so the scorer never ranks a noisy setup highly.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..core.enums import Side, Signal
from ..core.types import FeatureSet, ScoredCandidate, SignalResult
from .base import Strategy, StrategyContext
from .scorer import Scorer


def _direction_from_features(f: FeatureSet, ctx: StrategyContext) -> Side | None:
    """Map a single timeframe's features to a direction (or None = neutral)."""
    # extras holds ema_state as +1 (bull) / -1 (bear) / 0 (mixed)
    ema_sign = f.extras.get("ema_state", 0.0)
    if ema_sign == 0.0 or f.adx < ctx.adx_min:
        return None
    if ema_sign > 0:
        # Uptrend: require RSI in the long zone (not overbought).
        if ctx.rsi_long[0] <= f.rsi <= ctx.rsi_long[1]:
            return Side.LONG
    else:
        if ctx.rsi_short[0] <= f.rsi <= ctx.rsi_short[1]:
            return Side.SHORT
    return None


@dataclass(slots=True)
class _TfOrder:
    trend: str
    confirm: str
    trigger: str


def _split_timeframes(timeframes: list[str]) -> _TfOrder:
    """Assume ascending granularity (validated): fastest=trigger, slowest=trend."""
    if len(timeframes) < 3:
        raise ValueError(
            f"signal engine expects >=3 timeframes for confluence, got {timeframes}"
        )
    return _TfOrder(trend=timeframes[-1], confirm=timeframes[len(timeframes) // 2], trigger=timeframes[0])


class SignalEngine(Strategy):
    """Multi-timeframe confluence strategy."""

    def __init__(self, ctx: StrategyContext, timeframes: list[str]) -> None:
        self._ctx = ctx
        self._order = _split_timeframes(timeframes)
        self._scorer = Scorer(ctx, ctx.max_stop_distance_pct)

    # Evaluate returns SignalResult directly (the base type is Any for flexibility).
    def evaluate(self, symbol: str, features_by_tf: dict[str, FeatureSet]) -> SignalResult:
        order = self._order
        # Every required timeframe must be present.
        missing = [tf for tf in (order.trend, order.confirm, order.trigger) if tf not in features_by_tf]
        if missing:
            return SignalResult(
                symbol=symbol, signal=Signal.HOLD, side=None, confidence=0.0,
                by_timeframe={}, reason=f"missing timeframes: {missing}",
            )

        f_trend = features_by_tf[order.trend]
        f_conf = features_by_tf[order.confirm]
        f_trig = features_by_tf[order.trigger]

        # Trend sets the bias; confirmation and trigger must agree.
        dir_trend = _direction_from_features(f_trend, self._ctx)
        dir_conf = _direction_from_features(f_conf, self._ctx)
        dir_trig = _direction_from_features(f_trig, self._ctx)

        by_tf = {
            order.trend: self._to_signal(dir_trend),
            order.confirm: self._to_signal(dir_conf),
            order.trigger: self._to_signal(dir_trig),
        }

        # Full agreement -> signal; partial agreement -> weak HOLD.
        non_null = [d for d in (dir_trend, dir_conf, dir_trig) if d is not None]
        if not non_null:
            return self._hold(symbol, by_tf, "no directional bias on any tf", 0.0)
        if dir_trend is not None and dir_trend == dir_conf == dir_trig:
            side = dir_trend
            confidence = self._confidence(dir_trend, dir_conf, dir_trig, side)
            sig = Signal.BUY if side == Side.LONG else Signal.SELL
            return SignalResult(
                symbol=symbol, signal=sig, side=side, confidence=confidence,
                by_timeframe=by_tf,
                reason=f"confluence {side.value}: trend+confirm+trigger aligned",
            )
        if len(set(non_null)) == 1:
            return self._hold(
                symbol,
                by_tf,
                "partial timeframe agreement",
                confidence=len(non_null) / 6.0,
            )
        # Mixed directions -> conflict.
        return self._hold(
            symbol, by_tf, "conflicting timeframes", confidence=len(non_null) / 6.0
        )

    @staticmethod
    def _to_signal(side: Side | None) -> Signal:
        if side == Side.LONG:
            return Signal.BUY
        if side == Side.SHORT:
            return Signal.SELL
        return Signal.HOLD

    def _confidence(self, trend: Side | None, confirm: Side | None, trigger: Side | None, side: Side) -> float:
        # Base on how many TFs agree; weight trend highest.
        score = 0.0
        weights = (0.2, 0.3, 0.5)  # (trigger, confirm, trend)
        for direction, w in zip((trigger, confirm, trend), weights, strict=True):
            if direction == side:
                score += w
        return round(min(1.0, max(0.0, score)), 4)

    def _hold(self, symbol: str, by_tf: dict[str, Signal], reason: str, confidence: float) -> SignalResult:
        return SignalResult(
            symbol=symbol, signal=Signal.HOLD, side=None,
            confidence=round(min(1.0, max(0.0, confidence)), 4),
            by_timeframe=by_tf, reason=reason,
        )

    def score(self, features: FeatureSet, signal_result: Any) -> ScoredCandidate | None:
        return self._scorer.score_candidate(features, signal_result)
