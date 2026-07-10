"""Scoring weights configuration.

Defines the relative importance of each scoring criterion.
Weights can be configured via YAML and are normalized to sum to 1.0.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ScoringWeights:
    """Weights for each scoring criterion.

    All weights should be non-negative. The scoring engine normalizes
    them to sum to 1.0, so absolute values don't matter - only ratios.
    """

    trend: float = 0.30
    momentum: float = 0.20
    volume: float = 0.15
    volatility: float = 0.10
    liquidity: float = 0.10
    spread: float = 0.05
    risk: float = 0.10

    def total(self) -> float:
        """Return the sum of all weights."""
        return (
            self.trend
            + self.momentum
            + self.volume
            + self.volatility
            + self.liquidity
            + self.spread
            + self.risk
        )

    def normalized(self) -> ScoringWeights:
        """Return a new ScoringWeights with values normalized to sum to 1.0."""
        total = self.total()
        if total == 0:
            return ScoringWeights()  # fallback to defaults

        return ScoringWeights(
            trend=self.trend / total,
            momentum=self.momentum / total,
            volume=self.volume / total,
            volatility=self.volatility / total,
            liquidity=self.liquidity / total,
            spread=self.spread / total,
            risk=self.risk / total,
        )
