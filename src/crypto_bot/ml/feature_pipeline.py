"""Feature pipeline: prepares features for ML models.

Transforms raw FeatureSet objects into the format expected by ML models.
Handles normalization, feature selection, and preprocessing.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..core.types import FeatureSet


@dataclass(frozen=True, slots=True)
class FeatureVector:
    """Normalized feature vector for ML models."""

    symbol: str
    features: dict[str, float]
    timestamp: int | None = None


class FeaturePipeline:
    """Pipeline for transforming FeatureSet into ML-ready feature vectors.

    This pipeline handles:
    - Feature selection (choosing which features to include)
    - Normalization (scaling features to consistent ranges)
    - Feature engineering (creating derived features)
    """

    def __init__(
        self,
        selected_features: list[str] | None = None,
    ) -> None:
        # Default feature set for ML models
        self._selected_features = selected_features or [
            "trend_score",
            "momentum_score",
            "volatility_score",
            "volume_score",
            "liquidity_score",
            "spread_pct",
            "adx",
            "rsi",
            "atr_pct",
            "correlation_btc",
            "correlation_eth",
        ]

    def transform(self, feature_set: FeatureSet) -> FeatureVector:
        """Transform a FeatureSet into an ML-ready feature vector.

        Args:
            feature_set: The feature set to transform.

        Returns:
            FeatureVector with selected and normalized features.
        """
        features = {}

        # Extract selected features from FeatureSet
        for feature_name in self._selected_features:
            if hasattr(feature_set, feature_name):
                value = getattr(feature_set, feature_name)
                features[feature_name] = float(value) if value is not None else 0.0
            elif feature_name in feature_set.extras:
                value = feature_set.extras[feature_name]
                features[feature_name] = float(value) if value is not None else 0.0

        return FeatureVector(
            symbol=feature_set.symbol,
            features=features,
            timestamp=None,  # Could be added from FeatureSet if needed
        )

    def transform_batch(
        self,
        feature_sets: list[FeatureSet],
    ) -> list[FeatureVector]:
        """Transform multiple FeatureSets into feature vectors.

        Args:
            feature_sets: List of FeatureSet objects.

        Returns:
            List of FeatureVector objects.
        """
        return [self.transform(fs) for fs in feature_sets]

    def add_feature(self, feature_name: str) -> None:
        """Add a feature to the selected feature set.

        Args:
            feature_name: Name of the feature to add.
        """
        if feature_name not in self._selected_features:
            self._selected_features.append(feature_name)

    def remove_feature(self, feature_name: str) -> None:
        """Remove a feature from the selected feature set.

        Args:
            feature_name: Name of the feature to remove.
        """
        if feature_name in self._selected_features:
            self._selected_features.remove(feature_name)

    @property
    def selected_features(self) -> list[str]:
        """Get the list of selected features."""
        return list(self._selected_features)
