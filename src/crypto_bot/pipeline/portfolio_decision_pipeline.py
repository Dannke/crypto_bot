"""Portfolio decision orchestration without risk or execution concerns.

The pipeline deliberately stops at ``PortfolioIntent``:

``UniverseSnapshot -> market snapshot -> features -> PortfolioStrategy -> PortfolioIntent -> Fusion -> PortfolioIntent``.

The fusion step applies regime-aware exposure scaling (R4).
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

from ..core.types import Candle
from ..features.batch import build_features_batch
from ..features.builder import FeatureBuilder
from ..features.context import SymbolMarketContext
from ..pipeline.portfolio_fusion import PortfolioFusionEngine, RegimeGatedFusion
from ..portfolio.market_snapshot import MarketSnapshot
from ..portfolio.models import (
    CrossSectionalFeatureSnapshot,
    PortfolioIntent,
    PortfolioState,
    UniverseSnapshot,
)
from ..portfolio.regime import RegimeConfig, classify_regime
from ..strategy.base import PortfolioStrategy


class PortfolioDecisionPipeline:
    """Build portfolio features and delegate the decision to a portfolio strategy.

    No risk constraints, allocation adjustment, order construction, or
    execution are applied here.  The returned intent is exactly the intent
    emitted by the supplied strategy.
    """

    def __init__(
        self,
        feature_builder: FeatureBuilder,
        fusion_engine: PortfolioFusionEngine | None = None,
        regime_config: RegimeConfig | None = None,
    ) -> None:
        self._feature_builder = feature_builder
        self._fusion = fusion_engine or RegimeGatedFusion()
        self._regime_config = regime_config

    def process(
        self,
        universe: UniverseSnapshot,
        symbol_candles: Mapping[str, Mapping[str, list[Candle]]],
        market_by_symbol: Mapping[str, SymbolMarketContext],
        state: PortfolioState,
        strategy: PortfolioStrategy,
        *,
        trigger_tf: str | None = None,
    ) -> PortfolioIntent:
        """Return the portfolio intent emitted for the current universe.

        Candles for symbols outside ``universe`` are intentionally excluded
        before feature construction.  Missing market context falls back to
        ``SymbolMarketContext`` through the existing batch feature builder.

        The returned intent passes through the fusion engine for regime-aware
        exposure scaling.
        """
        if not isinstance(universe, UniverseSnapshot):
            raise ValueError("universe must be a UniverseSnapshot")
        if not isinstance(state, PortfolioState):
            raise ValueError("state must be a PortfolioState")
        if not isinstance(strategy, PortfolioStrategy):
            raise ValueError("strategy must be a PortfolioStrategy")

        scoped_candles = {
            symbol: {timeframe: list(candles) for timeframe, candles in by_timeframe.items()}
            for symbol, by_timeframe in symbol_candles.items()
            if symbol in universe.symbols
        }
        scoped_market = {
            symbol: context
            for symbol, context in market_by_symbol.items()
            if symbol in universe.symbols
        }
        if not all(isinstance(context, SymbolMarketContext) for context in scoped_market.values()):
            raise ValueError("market_by_symbol values must be SymbolMarketContext instances")

        features_by_symbol = build_features_batch(
            scoped_candles,
            self._feature_builder,
            scoped_market,
            quote=universe.quote_currency,
            trigger_tf=trigger_tf,
        )
        if not features_by_symbol:
            raise ValueError("no portfolio features could be built for the universe")

        # A portfolio strategy receives one feature vector per symbol.  The
        # primary/trigger timeframe is the current cross-sectional observation.
        primary_tf = trigger_tf or next(iter(next(iter(features_by_symbol.values()))))
        cross_section = CrossSectionalFeatureSnapshot(
            as_of_ms=universe.as_of_ms,
            universe=universe,
            features_by_symbol={
                symbol: by_timeframe[primary_tf]
                for symbol, by_timeframe in features_by_symbol.items()
                if primary_tf in by_timeframe
            },
        )
        intent = strategy.evaluate(cross_section, state)
        if not isinstance(intent, PortfolioIntent):
            raise TypeError("PortfolioStrategy.evaluate must return a PortfolioIntent")

        # Compute regime for fusion - extract candles for primary timeframe
        primary_candles = {
            symbol: tf_candles[primary_tf]
            for symbol, tf_candles in scoped_candles.items()
            if primary_tf in tf_candles
        }
        regime_config = self._regime_config or RegimeConfig(
            enabled=True,
            reference="universe_basket",
            trend_period=14,
            trend_threshold=25.0,
            vol_lookback_bars=168,
            vol_percentile_high=0.75,
        )
        regime = classify_regime(
            primary_candles,
            universe.as_of_ms,
            regime_config,
            quote=universe.quote_currency,
            timeframe=primary_tf,
        )

        # Apply fusion (regime gating)
        fused_intent = self._fusion.fuse([intent], regime)
        if not isinstance(fused_intent, PortfolioIntent):
            raise TypeError("PortfolioFusionEngine.fuse must return a PortfolioIntent")
        return fused_intent

    def process_market(
        self,
        snapshot: MarketSnapshot,
        state: PortfolioState,
        strategy: PortfolioStrategy,
        *,
        historical_candles: Mapping[str, Mapping[str, Sequence[Candle]]] | None = None,
    ) -> PortfolioIntent:
        """Run a market-based portfolio strategy over a closed-bar snapshot.

        The snapshot comes from the task-8 API (same timestamp, same
        timeframe, closed bars only); the strategy must implement
        ``evaluate_market`` (e.g. ``CrossSectionalMomentumStrategy``).

        The returned intent passes through the fusion engine for regime-aware
        exposure scaling.

        Args:
            snapshot: Current market snapshot with closed bars.
            state: Current portfolio state.
            strategy: Portfolio strategy to evaluate.
            historical_candles: Optional historical candles for regime classification.
                If provided, must be Mapping[symbol -> Mapping[timeframe -> list of candles]].
                Used to compute regime indicators that require lookback data.
        """
        if not isinstance(snapshot, MarketSnapshot):
            raise ValueError("snapshot must be a MarketSnapshot")
        if not isinstance(state, PortfolioState):
            raise ValueError("state must be a PortfolioState")
        if not isinstance(strategy, PortfolioStrategy):
            raise ValueError("strategy must be a PortfolioStrategy")
        intent = strategy.evaluate_market(snapshot, state)
        if not isinstance(intent, PortfolioIntent):
            raise TypeError("PortfolioStrategy.evaluate_market must return a PortfolioIntent")

        # Compute regime using the classifier with pipeline's regime config
        regime_config = self._regime_config or RegimeConfig(
            enabled=True,
            reference="universe_basket",
            trend_period=14,
            trend_threshold=25.0,
            vol_lookback_bars=168,
            vol_percentile_high=0.75,
        )

        # Use historical candles if provided, otherwise fall back to snapshot
        if historical_candles is not None:
            # Convert to the format expected by classify_regime
            candles_by_symbol = {
                symbol: list(tf_candles.get(snapshot.timeframe, []))
                for symbol, tf_candles in historical_candles.items()
            }
        else:
            # Extract candles from snapshot for regime classification
            candles_by_symbol = {
                symbol: list(candles)
                for symbol, candles in snapshot.candles_by_symbol.items()
            }
        regime = classify_regime(
            candles_by_symbol,
            snapshot.as_of_ms,
            regime_config,
            quote="USDT",  # MarketSnapshot uses USDT quote
            timeframe=snapshot.timeframe,
        )

        # Apply fusion (regime gating)
        fused_intent = self._fusion.fuse([intent], regime)
        if not isinstance(fused_intent, PortfolioIntent):
            raise TypeError("PortfolioFusionEngine.fuse must return a PortfolioIntent")
        return fused_intent

    @property
    def feature_builder(self) -> FeatureBuilder:
        """Expose the injected builder for composition and inspection."""
        return self._feature_builder
