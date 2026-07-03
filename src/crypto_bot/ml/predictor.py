"""Predictor: high-level interface for ML predictions.

Provides a unified interface for making predictions using ML models.
Currently returns stub predictions - will be connected to actual models
in future stages.
"""
from __future__ import annotations

from typing import Any

from ..core.enums import Signal
from .base import MLPrediction


class Predictor:
    """High-level predictor interface for ML models.

    This class provides a simple interface for making predictions,
    abstracting away the complexity of model loading, preprocessing,
    and postprocessing.
    """

    def __init__(self) -> None:
        self._enabled = False

    def predict(self, features: dict[str, float]) -> MLPrediction:
        """Generate a prediction from features.

        Currently returns a stub prediction. In future stages, this will
        use actual ML models to generate predictions.

        Args:
            features: Dictionary of feature names to values.

        Returns:
            MLPrediction with signal, side, and confidence.
        """
        # Stub implementation - returns HOLD with no confidence
        return MLPrediction(
            symbol=features.get("symbol", "UNKNOWN"),
            signal=Signal.HOLD,
            side=None,
            confidence=0.0,
            model_name="stub",
            model_version="1.0",
            raw_output=None,
            explanation=None,
        )

    def enable(self) -> None:
        """Enable ML predictions."""
        self._enabled = True

    def disable(self) -> None:
        """Disable ML predictions."""
        self._enabled = False

    @property
    def is_enabled(self) -> bool:
        """Check if ML predictions are enabled."""
        return self._enabled

    def predict_batch(
        self,
        features_list: list[dict[str, float]],
    ) -> list[MLPrediction]:
        """Generate predictions for multiple feature sets.

        Args:
            features_list: List of feature dictionaries.

        Returns:
            List of MLPrediction objects.
        """
        return [self.predict(features) for features in features_list]
