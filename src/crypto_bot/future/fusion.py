"""Fusion engine: combines classical and ML decision sources.

Provides the interface for fusing signals from classical strategies
with ML predictions. Currently uses only classical signals but the
architecture supports seamless ML integration in future stages.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum

from ..core.enums import Side, Signal
from ..ml.base import MLPrediction


class FusionMethod(StrEnum):
    """Methods for fusing classical and ML signals."""
    CLASSICAL_ONLY = "classical_only"  # Use only classical strategy
    ML_ONLY = "ml_only"  # Use only ML predictions
    WEIGHTED_AVERAGE = "weighted_average"  # Weighted combination
    MAJORITY_VOTE = "majority_vote"  # Vote-based combination
    ENSEMBLE = "ensemble"  # Advanced ensemble method


@dataclass(frozen=True, slots=True)
class FusedDecision:
    """Result of fusing classical and ML signals."""

    symbol: str
    signal: Signal
    side: Side | None
    confidence: float
    method: FusionMethod
    classical_signal: Signal
    classical_side: Side | None
    classical_confidence: float
    ml_signal: Signal | None = None
    ml_side: Side | None = None
    ml_confidence: float = 0.0
    ml_weight: float = 0.0  # Weight given to ML in fusion (0-1)

    @property
    def ml_used(self) -> bool:
        """Whether ML prediction was used in the decision."""
        return self.ml_signal is not None and self.ml_weight > 0


class FusionEngine(ABC):
    """Abstract base class for fusion engines.

    Fusion engines combine signals from multiple sources (classical strategy,
    ML models, etc.) into a single decision. Different fusion methods can
    be implemented as subclasses.
    """

    @abstractmethod
    def fuse(
        self,
        classical_signal: Signal,
        classical_side: Side | None,
        classical_confidence: float,
        ml_prediction: MLPrediction | None = None,
        ml_weight: float = 0.0,
    ) -> FusedDecision:
        """Fuse classical and ML signals into a single decision.

        Args:
            classical_signal: Signal from classical strategy.
            classical_side: Side from classical strategy.
            classical_confidence: Confidence from classical strategy.
            ml_prediction: Optional ML prediction.
            ml_weight: Weight given to ML (0-1, 0 = classical only).

        Returns:
            FusedDecision with the combined signal.
        """
        raise NotImplementedError


class ClassicalOnlyFusion(FusionEngine):
    """Fusion engine that uses only classical signals.

    This is the default implementation for stage 3, where ML is not yet
    integrated. It simply passes through the classical signal.
    """

    def fuse(
        self,
        classical_signal: Signal,
        classical_side: Side | None,
        classical_confidence: float,
        ml_prediction: MLPrediction | None = None,
        ml_weight: float = 0.0,
    ) -> FusedDecision:
        """Pass through the classical signal unchanged.

        Args:
            classical_signal: Signal from classical strategy.
            classical_side: Side from classical strategy.
            classical_confidence: Confidence from classical strategy.
            ml_prediction: Optional ML prediction (ignored).
            ml_weight: Weight given to ML (ignored).

        Returns:
            FusedDecision with the classical signal.
        """
        return FusedDecision(
            symbol="",  # Will be set by caller
            signal=classical_signal,
            side=classical_side,
            confidence=classical_confidence,
            method=FusionMethod.CLASSICAL_ONLY,
            classical_signal=classical_signal,
            classical_side=classical_side,
            classical_confidence=classical_confidence,
            ml_signal=None,
            ml_side=None,
            ml_confidence=0.0,
            ml_weight=0.0,
        )


class WeightedAverageFusion(FusionEngine):
    """Fusion engine that combines signals using weighted average.

    In future stages, this will combine classical and ML signals using
    configurable weights. Currently a stub for future implementation.
    """

    def __init__(self, ml_weight: float = 0.5) -> None:
        self._ml_weight = ml_weight

    def fuse(
        self,
        classical_signal: Signal,
        classical_side: Side | None,
        classical_confidence: float,
        ml_prediction: MLPrediction | None = None,
        ml_weight: float = 0.0,
    ) -> FusedDecision:
        """Combine classical and ML signals using weighted average.

        Args:
            classical_signal: Signal from classical strategy.
            classical_side: Side from classical strategy.
            classical_confidence: Confidence from classical strategy.
            ml_prediction: Optional ML prediction.
            ml_weight: Weight given to ML (overrides instance weight if provided).

        Returns:
            FusedDecision with the combined signal.
        """
        weight = ml_weight if ml_weight > 0 else self._ml_weight

        # If no ML prediction or ML disabled, use classical only
        if ml_prediction is None or weight == 0.0:
            return FusedDecision(
                symbol="",
                signal=classical_signal,
                side=classical_side,
                confidence=classical_confidence,
                method=FusionMethod.CLASSICAL_ONLY,
                classical_signal=classical_signal,
                classical_side=classical_side,
                classical_confidence=classical_confidence,
                ml_signal=None,
                ml_side=None,
                ml_confidence=0.0,
                ml_weight=0.0,
            )

        # Stub: for now, still use classical even with ML prediction
        # In future stages, this will implement actual weighted fusion
        return FusedDecision(
            symbol=ml_prediction.symbol,
            signal=classical_signal,  # Still use classical for now
            side=classical_side,
            confidence=classical_confidence,
            method=FusionMethod.WEIGHTED_AVERAGE,
            classical_signal=classical_signal,
            classical_side=classical_side,
            classical_confidence=classical_confidence,
            ml_signal=ml_prediction.signal,
            ml_side=ml_prediction.side,
            ml_confidence=ml_prediction.confidence,
            ml_weight=weight,
        )
