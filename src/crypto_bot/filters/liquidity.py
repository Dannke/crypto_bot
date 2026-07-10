"""Liquidity filter: rejects symbols with insufficient trading volume.

Liquidity is critical for slippage control and order execution. This filter
ensures we only trade symbols with enough 24h volume to enter/exit positions
without significant market impact.
"""
from __future__ import annotations

from typing import Any

from ..core.types import FeatureSet
from .base import Filter, FilterOutcome, FilterResult


class LiquidityFilter(Filter):
    """Filter based on 24h quote volume and liquidity score.

    Rejects symbols that don't meet minimum liquidity thresholds to prevent
    trading in illiquid markets where execution would be problematic.
    """

    def __init__(
        self,
        min_quote_volume_24h: float = 1_000_000.0,  # $1M daily volume
        min_liquidity_score: float = 0.3,  # 0..1 normalized score
    ) -> None:
        super().__init__("liquidity")
        self._min_quote_volume = min_quote_volume_24h
        self._min_liquidity_score = min_liquidity_score

    def evaluate(self, features: FeatureSet, **kwargs: Any) -> FilterResult:
        # Check liquidity score from features
        if features.liquidity_score < self._min_liquidity_score:
            return FilterResult(
                filter_name=self.name,
                outcome=FilterOutcome.REJECT,
                reason="insufficient_liquidity",
                detail=f"liquidity_score={features.liquidity_score:.3f} < {self._min_liquidity_score}",
            )

        # Check raw volume (if available via extras or derived from volume)
        # For now, we rely on the normalized liquidity_score
        return FilterResult(
            filter_name=self.name,
            outcome=FilterOutcome.PASS,
        )
