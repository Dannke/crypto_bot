"""Spread filter: rejects symbols with wide bid-ask spreads.

Wide spreads indicate low liquidity or high market maker fees, which can
significantly impact profitability, especially for frequent trading.
"""
from __future__ import annotations

from .base import Filter, FilterOutcome, FilterResult
from ..core.types import FeatureSet


class SpreadFilter(Filter):
    """Filter based on bid-ask spread percentage.

    Rejects symbols where the spread is too wide relative to the mid price,
    which would lead to excessive slippage on entry and exit.
    """

    def __init__(
        self,
        max_spread_pct: float = 0.5,  # 0.5% max spread
    ) -> None:
        super().__init__("spread")
        self._max_spread_pct = max_spread_pct

    def evaluate(self, features: FeatureSet) -> FilterResult:
        if features.spread_pct > self._max_spread_pct:
            return FilterResult(
                filter_name=self.name,
                outcome=FilterOutcome.REJECT,
                reason="spread_too_wide",
                detail=f"spread_pct={features.spread_pct:.3f}% > {self._max_spread_pct}%",
            )

        return FilterResult(
            filter_name=self.name,
            outcome=FilterOutcome.PASS,
        )
