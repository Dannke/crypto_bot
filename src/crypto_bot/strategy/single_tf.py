"""Signal engine that evaluates each timeframe independently.

Instead of requiring cross-timeframe confluence (trend + confirm + trigger
all agreeing), this engine checks each timeframe on its own and produces
a separate BUY/SELL/HOLD signal per timeframe.  The caller (or the pipeline)
is responsible for aggregating or selecting among them.
"""
from __future__ import annotations

from ..core.enums import Side, Signal
from ..core.types import FeatureSet, SignalResult
from .base import Strategy, StrategyContext


def _direction_from_features(f: FeatureSet, ctx: StrategyContext) -> Side | None:
    """Map a single timeframe's features to a direction (or None = neutral)."""
    ema_sign = f.extras.get("ema_state", 0.0)
    if ema_sign == 0.0 or f.adx < ctx.adx_min:
        return None
    if ema_sign > 0:
        if ctx.rsi_long[0] <= f.rsi <= ctx.rsi_long[1]:
            return Side.LONG
    else:
        if ctx.rsi_short[0] <= f.rsi <= ctx.rsi_short[1]:
            return Side.SHORT
    return None


class SingleTfEngine(Strategy):
    """Evaluates each timeframe independently for directional signals.

    Unlike ``SignalEngine`` (which requires 3+ timeframes and cross-timeframe
    agreement), this engine treats each timeframe as its own signal source.
    A candidate built with this engine will carry the timeframe label in its
    ``FeatureSet.timeframe`` field.
    """

    def __init__(self, ctx: StrategyContext, timeframes: list[str]) -> None:
        self._ctx = ctx
        self._timeframes = timeframes

    def evaluate(self, symbol: str, features_by_tf: dict[str, FeatureSet]) -> SignalResult:
        tf = next(iter(features_by_tf))
        features = features_by_tf[tf]
        side = _direction_from_features(features, self._ctx)

        if side is not None:
            sig = Signal.BUY if side == Side.LONG else Signal.SELL
            return SignalResult(
                symbol=symbol,
                signal=sig,
                side=side,
                confidence=1.0,
                by_timeframe={tf: sig},
                reason=f"{tf}: {side.value} bias",
            )
        return SignalResult(
            symbol=symbol,
            signal=Signal.HOLD,
            side=None,
            confidence=0.0,
            by_timeframe={tf: Signal.HOLD},
            reason=f"{tf}: no directional bias",
        )
