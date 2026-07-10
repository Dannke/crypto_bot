"""Base ML interfaces: abstract contracts for ML models.

Defines the core interfaces that ML models must implement to integrate
with the decision pipeline. These are abstract contracts - concrete
implementations will be added in future stages.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum

from ..core.enums import Side, Signal


class ModelType(StrEnum):
    """Types of ML models."""
    CLASSIFIER = "classifier"  # Binary/multi-class classification
    REGRESSOR = "regressor"  # Regression for continuous values
    REINFORCEMENT = "reinforcement"  # RL agent


@dataclass(frozen=True, slots=True)
class MLPrediction:
    """Prediction result from an ML model.

    Currently a stub - will be expanded with actual ML outputs in future stages.
    """

    symbol: str
    signal: Signal
    side: Side | None
    confidence: float
    model_name: str
    model_version: str = "1.0"
    raw_output: dict[str, float] | None = None
    explanation: dict[str, str] | None = None

    @property
    def is_valid(self) -> bool:
        """Whether the prediction is valid (not a stub)."""
        return self.signal != Signal.HOLD and self.side is not None


class MLModel(ABC):
    """Abstract base class for ML models.

    All ML models must implement this interface to integrate with the
    decision pipeline. The interface is designed to be model-agnostic,
    supporting various ML frameworks (PyTorch, TensorFlow, scikit-learn, etc.).
    """

    @abstractmethod
    def predict(self, features: dict[str, float]) -> MLPrediction:
        """Generate a prediction from features.

        Args:
            features: Dictionary of feature names to values.

        Returns:
            MLPrediction with signal, side, and confidence.
        """
        raise NotImplementedError

    @abstractmethod
    def get_model_info(self) -> dict[str, str]:
        """Get information about the model.

        Returns:
            Dictionary with model metadata (name, version, type, etc.).
        """
        raise NotImplementedError

    @abstractmethod
    def is_ready(self) -> bool:
        """Check if the model is ready for predictions.

        Returns:
            True if the model is loaded and ready, False otherwise.
        """
        raise NotImplementedError
