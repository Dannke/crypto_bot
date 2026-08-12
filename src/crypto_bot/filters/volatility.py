"""Volatility filter: rejects symbols with extreme volatility.

Too little volatility means no movement to profit from. Too much volatility
means excessive risk and potential stop-loss triggers. This filter keeps
volatility within a tradable range.
"""
from __future__ import annotations

from typing import Any

from ..core.types import FeatureSet
from .base import Filter, FilterOutcome, FilterResult


class VolatilityFilter(Filter):
    """Filter based on ATR percentage (volatility relative to price).

    Rejects symbols that are either too dead (low ATR%) or too explosive
    (high ATR%) for the configured risk parameters.
    """

    def __init__(
        self,
        min_atr_pct: float | dict[str, float] = 0.5,
        max_atr_pct: float | dict[str, float] = 8.0,
    ) -> None:
        super().__init__("volatility")
        self._min_atr_pct = min_atr_pct
        self._max_atr_pct = max_atr_pct

    def _resolve(self, val: float | dict[str, float], tf: str) -> float:
        return val[tf] if isinstance(val, dict) else val

    def evaluate(self, features: FeatureSet, **kwargs: Any) -> FilterResult:
        atr = features.atr_pct
        min_val = self._resolve(self._min_atr_pct, features.timeframe)
        max_val = self._resolve(self._max_atr_pct, features.timeframe)

        if atr < min_val:
            return FilterResult(
                filter_name=self.name,
                outcome=FilterOutcome.REJECT,
                reason="volatility_too_low",
                detail=f"atr_pct={atr:.3f}% < {min_val}%",
            )

        if atr > max_val:
            return FilterResult(
                filter_name=self.name,
                outcome=FilterOutcome.REJECT,
                reason="volatility_too_high",
                detail=f"atr_pct={atr:.3f}% > {max_val}%",
            )

        return FilterResult(
            filter_name=self.name,
            outcome=FilterOutcome.PASS,
        )
