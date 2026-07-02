"""Volatility filter: rejects symbols with extreme volatility.

Too little volatility means no movement to profit from. Too much volatility
means excessive risk and potential stop-loss triggers. This filter keeps
volatility within a tradable range.
"""
from __future__ import annotations

from .base import Filter, FilterOutcome, FilterResult
from ..core.types import FeatureSet


class VolatilityFilter(Filter):
    """Filter based on ATR percentage (volatility relative to price).

    Rejects symbols that are either too dead (low ATR%) or too explosive
    (high ATR%) for the configured risk parameters.
    """

    def __init__(
        self,
        min_atr_pct: float = 0.5,  # minimum 0.5% daily movement
        max_atr_pct: float = 8.0,  # maximum 8% daily movement
    ) -> None:
        super().__init__("volatility")
        self._min_atr_pct = min_atr_pct
        self._max_atr_pct = max_atr_pct

    def evaluate(self, features: FeatureSet) -> FilterResult:
        atr = features.atr_pct

        if atr < self._min_atr_pct:
            return FilterResult(
                filter_name=self.name,
                outcome=FilterOutcome.REJECT,
                reason="volatility_too_low",
                detail=f"atr_pct={atr:.3f}% < {self._min_atr_pct}%",
            )

        if atr > self._max_atr_pct:
            return FilterResult(
                filter_name=self.name,
                outcome=FilterOutcome.REJECT,
                reason="volatility_too_high",
                detail=f"atr_pct={atr:.3f}% > {self._max_atr_pct}%",
            )

        return FilterResult(
            filter_name=self.name,
            outcome=FilterOutcome.PASS,
        )
