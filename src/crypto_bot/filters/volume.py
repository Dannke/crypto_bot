"""Volume filter: rejects symbols with insufficient trading volume.

Volume confirmation is important for validating price moves. This filter
ensures we only trade symbols with meaningful trading activity.
"""
from __future__ import annotations

from typing import Any

from ..core.types import FeatureSet
from .base import Filter, FilterOutcome, FilterResult


class VolumeFilter(Filter):
    """Filter based on volume score and absolute volume.

    Rejects symbols with low volume score (relative to moving average) or
    absolute volume below a minimum threshold.
    """

    def __init__(
        self,
        min_volume_score: float = 0.5,  # 0..1 normalized score
        min_absolute_volume: float = 1000.0,  # minimum base currency volume
    ) -> None:
        super().__init__("volume")
        self._min_volume_score = min_volume_score
        self._min_absolute_volume = min_absolute_volume

    def evaluate(self, features: FeatureSet, **kwargs: Any) -> FilterResult:
        # Check volume score (relative to MA)
        if features.volume_score < self._min_volume_score:
            return FilterResult(
                filter_name=self.name,
                outcome=FilterOutcome.REJECT,
                reason="low_volume_score",
                detail=f"volume_score={features.volume_score:.3f} < {self._min_volume_score}",
            )

        # Check absolute volume
        if features.volume < self._min_absolute_volume:
            return FilterResult(
                filter_name=self.name,
                outcome=FilterOutcome.REJECT,
                reason="low_absolute_volume",
                detail=f"volume={features.volume:.2f} < {self._min_absolute_volume}",
            )

        return FilterResult(
            filter_name=self.name,
            outcome=FilterOutcome.PASS,
        )
