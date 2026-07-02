"""Trend filter: rejects symbols without a clear directional bias.

This filter ensures we only trade symbols with a defined trend direction,
avoiding choppy/ranging markets where trend-following strategies fail.
"""
from __future__ import annotations

from .base import Filter, FilterOutcome, FilterResult
from ..core.types import FeatureSet


class TrendFilter(Filter):
    """Filter based on trend strength and direction.

    Rejects symbols with weak trend (low ADX) or undefined trend direction
    (mixed EMA stack). Only passes when there's a clear bullish or bearish
    trend with sufficient strength.
    """

    def __init__(
        self,
        min_adx: float = 20.0,  # minimum ADX for trending market
    ) -> None:
        super().__init__("trend")
        self._min_adx = min_adx

    def evaluate(self, features: FeatureSet) -> FilterResult:
        # Check ADX strength
        if features.adx < self._min_adx:
            return FilterResult(
                filter_name=self.name,
                outcome=FilterOutcome.REJECT,
                reason="weak_trend",
                detail=f"adx={features.adx:.2f} < {self._min_adx}",
            )

        # Check trend score (derived from EMA stack alignment)
        if features.trend_score < 0.3:
            return FilterResult(
                filter_name=self.name,
                outcome=FilterOutcome.REJECT,
                reason="no_clear_direction",
                detail=f"trend_score={features.trend_score:.3f} < 0.3",
            )

        return FilterResult(
            filter_name=self.name,
            outcome=FilterOutcome.PASS,
        )
