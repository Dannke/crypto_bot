"""Strategy interfaces and shared candidate-strategy configuration."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from ..core.types import FeatureSet, SignalResult
from ..portfolio.market_snapshot import MarketSnapshot
from ..portfolio.models import CrossSectionalFeatureSnapshot, PortfolioIntent, PortfolioState


@dataclass(frozen=True, slots=True)
class StrategyContext:
    """Resolved, read-only parameters passed to a candidate strategy."""

    scoring_weights: dict[str, float]
    min_score: float
    min_confidence: float
    adx_min: float
    rsi_long: tuple[float, float]
    rsi_short: tuple[float, float]
    atr_min_pct: float | dict[str, float]
    atr_max_pct: float | dict[str, float]
    take_profit_risk_multiple: float | dict[str, float]
    max_stop_distance_pct: float = 3.0
    raw: dict[str, Any] | None = None


class CandidateStrategy(ABC):
    """Evaluate one symbol's multi-timeframe features into a signal."""

    @abstractmethod
    def evaluate(self, symbol: str, features_by_tf: dict[str, FeatureSet]) -> SignalResult:
        """Produce a BUY, SELL, or HOLD result for one candidate."""


class PortfolioStrategy(ABC):
    """Evaluate the cross-section and state into a target portfolio intent."""

    @abstractmethod
    def evaluate(
        self,
        features: CrossSectionalFeatureSnapshot,
        state: PortfolioState,
    ) -> PortfolioIntent:
        """Produce an intent only; execution belongs to a downstream service."""

    def evaluate_market(
        self,
        snapshot: MarketSnapshot,
        state: PortfolioState,
    ) -> PortfolioIntent:
        """Evaluate a raw market snapshot (closed bars only) into an intent.

        Feature-based strategies build indicators from candles; market-based
        strategies (e.g. cross-sectional momentum) consume the snapshot
        directly.  Strategies that need ``FeatureSet`` input must not be fed
        a market snapshot, and vice versa.
        """
        raise NotImplementedError(
            f"{type(self).__name__} evaluates features, not a market snapshot; "
            "it does not implement evaluate_market()"
        )

    def build_intent(
        self,
        features: CrossSectionalFeatureSnapshot,
        state: PortfolioState,
    ) -> PortfolioIntent:
        """Portfolio-oriented alias for :meth:`evaluate`."""
        return self.evaluate(features, state)


class Strategy(CandidateStrategy):
    """Backward-compatible name for :class:`CandidateStrategy`.

    Existing signal engines continue to inherit this class unchanged, and are
    consequently registered as ``candidate`` strategies.
    """
