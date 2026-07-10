"""Features package: candles -> normalised feature vectors.

Kept free of I/O. ``FeatureBuilder`` consumes typed ``Candle`` lists and emits
``FeatureSet`` objects the strategy layer consumes.
"""
from .batch import aligned_correlation, build_features_batch, closes_by_timestamp
from .builder import FeatureBuilder, FeatureBuilderParams, builder_from_settings
from .context import SymbolMarketContext, liquidity_score, market_context_from_ticker

__all__ = [
    "FeatureBuilder",
    "FeatureBuilderParams",
    "builder_from_settings",
    "build_features_batch",
    "aligned_correlation",
    "closes_by_timestamp",
    "SymbolMarketContext",
    "liquidity_score",
    "market_context_from_ticker",
]
