"""Base filter interface and result type.

All filters implement the same contract: evaluate a candidate and return
PASS or REJECT with a human-readable reason. This enables the decision
pipeline to explain exactly why a symbol was rejected.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ..core.types import FeatureSet


class FilterOutcome(StrEnum):
    """Result of a filter evaluation."""
    PASS = "pass"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class FilterResult:
    """Result of applying a single filter to a candidate."""

    filter_name: str
    outcome: FilterOutcome
    reason: str = ""
    detail: str = ""

    @property
    def passed(self) -> bool:
        return self.outcome == FilterOutcome.PASS


class Filter:
    """Base interface for all quality filters.

    Each filter is stateless and evaluates a single candidate independently.
    This makes filters easy to test and reason about.
    """

    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def evaluate(self, features: FeatureSet) -> FilterResult:
        """Evaluate a candidate and return PASS or REJECT with a reason.

        Args:
            features: The feature set for the symbol/timeframe being evaluated.

        Returns:
            FilterResult with outcome and optional reason/detail.
        """
        raise NotImplementedError
