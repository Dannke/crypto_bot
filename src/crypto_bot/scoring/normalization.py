"""Normalization utilities for scoring.

Normalizes raw feature values to a consistent 0-1 scale for scoring.
This ensures different criteria (with different units and ranges) can be
combined meaningfully.
"""
from __future__ import annotations

import numpy as np


class Normalizer:
    """Utility class for normalizing values to [0, 1] range."""

    @staticmethod
    def min_max(value: float, min_val: float, max_val: float) -> float:
        """Normalize using min-max scaling.

        Args:
            value: The value to normalize.
            min_val: Minimum expected value.
            max_val: Maximum expected value.

        Returns:
            Normalized value in [0, 1], clipped to bounds.
        """
        if max_val == min_val:
            return 0.5
        normalized = (value - min_val) / (max_val - min_val)
        return max(0.0, min(1.0, normalized))

    @staticmethod
    def sigmoid(value: float, center: float = 0.0, scale: float = 1.0) -> float:
        """Normalize using sigmoid function.

        Useful for smooth transitions around a center point.

        Args:
            value: The value to normalize.
            center: The center point (where output = 0.5).
            scale: Controls the steepness of the transition.

        Returns:
            Normalized value in (0, 1).
        """
        x = (value - center) / scale
        return 1.0 / (1.0 + np.exp(-x))

    @staticmethod
    def clamp(value: float, min_val: float = 0.0, max_val: float = 1.0) -> float:
        """Clamp a value to a range."""
        return max(min_val, min(max_val, value))

    @staticmethod
    def safe_divide(numerator: float, denominator: float, default: float = 0.0) -> float:
        """Safe division that returns default on division by zero."""
        if denominator == 0 or np.isnan(denominator):
            return default
        return numerator / denominator
