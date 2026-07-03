"""Features package: candles -> normalised feature vectors.

Kept free of I/O. ``FeatureBuilder`` consumes typed ``Candle`` lists and emits
``FeatureSet`` objects the strategy layer consumes.
"""
from .builder import FeatureBuilder, FeatureBuilderParams, builder_from_settings
from .batch import build_features_batch, return_correlation
from .context import SymbolMarketContext, liquidity_score, market_context_from_ticker

__all__ = [
    "FeatureBuilder",
    "FeatureBuilderParams",
    "builder_from_settings",
    "build_features_batch",
    "return_correlation",
    "SymbolMarketContext",
    "liquidity_score",
    "market_context_from_ticker",
]
