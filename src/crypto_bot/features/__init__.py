"""Features package: candles -> normalised feature vectors.

Kept free of I/O. ``FeatureBuilder`` consumes typed ``Candle`` lists and emits
``FeatureSet`` objects the strategy layer consumes.
"""
from .builder import FeatureBuilder, FeatureBuilderParams, builder_from_settings

__all__ = ["FeatureBuilder", "FeatureBuilderParams", "builder_from_settings"]
