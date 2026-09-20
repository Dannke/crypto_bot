"""Composition factory for the Decision Intelligence Layer.

Wires validated ``Settings`` into filters, scoring, strategy, and the
``DecisionPipeline``. Keeps dependency construction out of the orchestrator
so the decision layer stays testable in isolation.
"""
from __future__ import annotations

from datetime import datetime

from ..config.schemas import Settings
from ..core import policy
from ..core.enums import StrategyType
from ..core.exceptions import ConfigError
from ..features.builder import builder_from_settings
from ..filters import (
    BlacklistFilter,
    CooldownFilter,
    Filter,
    LiquidityFilter,
    SpreadFilter,
    TrendFilter,
    VolatilityFilter,
    VolumeFilter,
)
from ..pipeline.candidate_builder import CandidateBuilder
from ..pipeline.candidate_selector import CandidateSelector, SelectionConfig
from ..pipeline.decision_pipeline import DecisionPipeline
from ..pipeline.portfolio_decision_pipeline import PortfolioDecisionPipeline
from ..portfolio import PortfolioRiskEngine, PortfolioRiskLimits
from ..scoring.score_engine import ScoreEngine
from ..scoring.weights import ScoringWeights
from ..strategy.base import CandidateStrategy, PortfolioStrategy, StrategyContext
from ..strategy.manager import StrategyManager
from ..strategy.portfolio_strategies import (
    MEAN_REVERSION_V0_STRATEGY_NAME,
    MOMENTUM_V0_STRATEGY_NAME,
    RANDOM_STRATEGY_NAME,
    REVERSE_MOMENTUM_STRATEGY_NAME,
    CrossSectionalMomentumStrategy,
    LongOnlyTrendPortfolioStrategy,
    MeanReversionStrategy,
    RandomBaselineStrategy,
    ReverseMomentumStrategy,
)
from ..strategy.registry import StrategyRegistry
from ..strategy.signal_engine import SignalEngine
from ..strategy.single_tf import SingleTfEngine

DEFAULT_STRATEGY_NAME = "confluence"
PER_TF_STRATEGY_NAME = "per_timeframe"
PORTFOLIO_STRATEGY_NAME = "long_only_trend"


def scoring_weights_from_settings(settings: Settings) -> ScoringWeights:
    """Build stage-3 scoring weights from validated settings."""
    w = settings.scoring.weights
    return ScoringWeights(
        trend=w.trend,
        momentum=w.momentum,
        volume=w.volume,
        volatility=w.volatility,
        liquidity=w.liquidity,
        spread=w.spread,
        risk=w.risk,
    ).normalized()


def strategy_context_from_settings(settings: Settings) -> StrategyContext:
    """Build a ``StrategyContext`` from validated settings."""
    w = settings.scoring.weights
    return StrategyContext(
        scoring_weights={
            "trend": w.trend,
            "momentum": w.momentum,
            "volume": w.volume,
            "spread": w.spread,
            "risk": w.risk,
            "liquidity": w.liquidity,
        },
        min_score=settings.scoring.min_score,
        min_confidence=settings.scoring.min_confidence,
        adx_min=settings.strategy.trend.adx_min,
        rsi_long=(
            settings.strategy.momentum.rsi_long_min,
            settings.strategy.momentum.rsi_long_max,
        ),
        rsi_short=(
            settings.strategy.momentum.rsi_short_min,
            settings.strategy.momentum.rsi_short_max,
        ),
        atr_min_pct=settings.strategy.volatility.atr_min_pct,
        atr_max_pct=settings.strategy.volatility.atr_max_pct,
        take_profit_risk_multiple=settings.risk.take_profit_risk_multiple,
        max_stop_distance_pct=settings.risk.max_stop_distance_pct,
    )


def build_filters(
    settings: Settings,
    *,
    last_trade_time: dict[str, datetime] | None = None,
) -> list[Filter]:
    """Create the default filter chain from configuration."""
    f = settings.filters
    s = settings.strategy
    filters: list[Filter] = [
        BlacklistFilter(set(f.blacklist)),
        CooldownFilter(
            cooldown_minutes=f.cooldown_after_trade_minutes,
            last_trade_time=last_trade_time,
        ),
    ]
    # Liquidity/spread test real exchange microstructure; on endpoints where
    # that data isn't meaningful (e.g. testnet, where 24h quote volume is
    # routinely zero) a config can explicitly opt out rather than forcing a
    # threshold guess that may reject everything or nothing.
    if f.enable_liquidity_filter:
        filters.append(LiquidityFilter(min_quote_volume_24h=f.min_quote_volume_usd))
    if f.enable_spread_filter:
        filters.append(SpreadFilter(max_spread_pct=f.max_spread_pct))
    filters.extend([
        TrendFilter(min_adx=s.trend.adx_min),
        VolatilityFilter(
            min_atr_pct=s.volatility.atr_min_pct,
            max_atr_pct=s.volatility.atr_max_pct,
        ),
    ])
    if f.enable_volume_filter:
        filters.append(
            VolumeFilter(
                min_volume_score=f.min_volume_score,
                min_absolute_volume=f.min_absolute_volume,
            )
        )
    return filters


def build_candidate_builder(
    settings: Settings,
    *,
    last_trade_time: dict[str, datetime] | None = None,
) -> CandidateBuilder:
    """Wire filters and score engine into a ``CandidateBuilder``."""
    weights = scoring_weights_from_settings(settings)
    btc_sym = f"BTC/{settings.universe.quote}"
    score_engine = ScoreEngine(weights=weights, btc_reference_symbol=btc_sym)
    filters = build_filters(settings, last_trade_time=last_trade_time)
    return CandidateBuilder(filters=filters, score_engine=score_engine)


def build_candidate_selector(settings: Settings) -> CandidateSelector:
    """Wire selection thresholds from configuration."""
    return CandidateSelector(
        SelectionConfig(
            max_candidates=settings.scoring.max_candidates_per_cycle,
            min_score=settings.scoring.min_score,
            min_confidence=settings.scoring.min_confidence,
        )
    )


def build_strategy_registry() -> StrategyRegistry:
    """Register built-in strategies."""
    registry = StrategyRegistry()
    registry.register(DEFAULT_STRATEGY_NAME, SignalEngine, strategy_type=StrategyType.CANDIDATE)
    registry.register(PER_TF_STRATEGY_NAME, SingleTfEngine, strategy_type=StrategyType.CANDIDATE)
    registry.register(
        PORTFOLIO_STRATEGY_NAME,
        LongOnlyTrendPortfolioStrategy,
        strategy_type=StrategyType.PORTFOLIO,
    )
    registry.register(
        MOMENTUM_V0_STRATEGY_NAME,
        CrossSectionalMomentumStrategy,
        strategy_type=StrategyType.PORTFOLIO,
    )
    registry.register(
        RANDOM_STRATEGY_NAME,
        RandomBaselineStrategy,
        strategy_type=StrategyType.PORTFOLIO,
    )
    registry.register(
        REVERSE_MOMENTUM_STRATEGY_NAME,
        ReverseMomentumStrategy,
        strategy_type=StrategyType.PORTFOLIO,
    )
    registry.register(
        MEAN_REVERSION_V0_STRATEGY_NAME,
        MeanReversionStrategy,
        strategy_type=StrategyType.PORTFOLIO,
    )
    return registry


def build_strategy_manager(
    settings: Settings,
    *,
    strategy_name: str = DEFAULT_STRATEGY_NAME,
    strategy_type: StrategyType | None = None,
    registry: StrategyRegistry | None = None,
) -> StrategyManager:
    """Create a strategy manager with the configured default strategy active."""
    reg = registry or build_strategy_registry()
    resolved_type = strategy_type or settings.runtime.strategy_type
    manager = StrategyManager(registry=reg)
    manager.set_default_strategy(strategy_name, strategy_type=resolved_type)
    ctx = strategy_context_from_settings(settings)
    manager.activate_strategy(
        strategy_name,
        ctx,
        settings.timeframes.primary,
        strategy_type=resolved_type,
    )
    return manager


def build_decision_pipeline(
    settings: Settings,
    *,
    last_trade_time: dict[str, datetime] | None = None,
) -> DecisionPipeline:
    """Create a fully wired ``DecisionPipeline`` from settings."""
    builder = build_candidate_builder(settings, last_trade_time=last_trade_time)
    selector = build_candidate_selector(settings)
    return DecisionPipeline(builder=builder, selector=selector)


def build_portfolio_decision_pipeline(
    settings: Settings,
    regime_config=None,
) -> PortfolioDecisionPipeline:
    """Create the feature-only portfolio decision pipeline from settings."""
    from ..config.schemas import RegimeConfig
    from ..pipeline.portfolio_fusion import create_regime_gated_fusion

    rc = regime_config or RegimeConfig()
    # Extract default multipliers from regime config
    exposure_multiplier = {
        "trend_low_vol": rc.exposure_trend_low_vol,
        "trend_high_vol": rc.exposure_trend_high_vol,
        "range_low_vol": rc.exposure_range_low_vol,
        "range_high_vol": rc.exposure_range_high_vol,
    }
    # Create fusion engine with per-strategy overrides
    fusion = create_regime_gated_fusion(
        config=exposure_multiplier,
        strategy_overrides=rc.strategy_overrides,
    )

    return PortfolioDecisionPipeline(
        feature_builder=builder_from_settings(settings),
        fusion_engine=fusion,
        regime_config=rc,
    )


def build_portfolio_strategy(settings: Settings) -> PortfolioStrategy:
    """Construct the configured portfolio strategy with its parameters.

    ``settings.portfolio.strategy_name`` selects the strategy; the CSM
    block is translated for momentum/random/reverse strategies, while
    mean_reversion uses its own config block.
    """
    name = settings.portfolio.strategy_name
    ctx = strategy_context_from_settings(settings)
    if name == PORTFOLIO_STRATEGY_NAME:
        return LongOnlyTrendPortfolioStrategy(ctx, settings.timeframes.primary)
    if name == MOMENTUM_V0_STRATEGY_NAME:
        csm = settings.portfolio.csm
        tf_seconds = policy.timeframe_to_seconds(csm.timeframe)
        lookbacks_bars = tuple(
            policy.parse_duration_seconds(duration) // tf_seconds
            for duration in csm.lookbacks
        )
        return CrossSectionalMomentumStrategy(
            ctx,
            [csm.timeframe],
            lookbacks_bars=lookbacks_bars,
            top_fraction=1.0 - csm.long_percentile,
            short_fraction=csm.short_percentile,
        )
    if name == RANDOM_STRATEGY_NAME:
        csm = settings.portfolio.csm
        tf_seconds = policy.timeframe_to_seconds(csm.timeframe)
        lookbacks_bars = tuple(
            policy.parse_duration_seconds(duration) // tf_seconds
            for duration in csm.lookbacks
        )
        return RandomBaselineStrategy(
            ctx,
            [csm.timeframe],
            lookbacks_bars=lookbacks_bars,
            top_fraction=1.0 - csm.long_percentile,
            short_fraction=csm.short_percentile,
            seed=csm.seed,
        )
    if name == REVERSE_MOMENTUM_STRATEGY_NAME:
        csm = settings.portfolio.csm
        tf_seconds = policy.timeframe_to_seconds(csm.timeframe)
        lookbacks_bars = tuple(
            policy.parse_duration_seconds(duration) // tf_seconds
            for duration in csm.lookbacks
        )
        return ReverseMomentumStrategy(
            ctx,
            [csm.timeframe],
            lookbacks_bars=lookbacks_bars,
            top_fraction=1.0 - csm.long_percentile,
            short_fraction=csm.short_percentile,
        )
    if name == MEAN_REVERSION_V0_STRATEGY_NAME:
        mr = settings.portfolio.mean_reversion
        tf_seconds = policy.timeframe_to_seconds(mr.timeframe)
        signal_lookback_bars = policy.parse_duration_seconds(mr.signal_lookback) // tf_seconds
        return MeanReversionStrategy(
            ctx,
            [mr.timeframe],
            zscore_window_bars=mr.zscore_window_bars,
            signal_lookback_bars=signal_lookback_bars,
            entry_threshold=mr.entry_threshold,
            exit_threshold=mr.exit_threshold,
            max_holding_bars=mr.max_holding_bars,
            weighting=mr.weighting,
            rebalance_hours=mr.rebalance_hours,
            max_positions=mr.max_positions,
            entry_execution=mr.entry_execution,
            exit_execution=mr.exit_execution,
            min_expected_edge_bps=mr.min_expected_edge_bps,
            top_fraction=1.0 - 0.90,  # Use fixed percentile or could be configurable
            short_fraction=0.2,
        )
    raise ConfigError(f"unsupported portfolio strategy {name!r}")


def get_active_strategy(manager: StrategyManager) -> CandidateStrategy:
    """Return the default active candidate strategy or raise if missing."""
    strategy = manager.get_default_strategy()
    if strategy is None:
        raise RuntimeError("no active default strategy")
    if not isinstance(strategy, CandidateStrategy):
        raise TypeError("active strategy is a portfolio strategy, not a candidate strategy")
    return strategy


def get_active_portfolio_strategy(manager: StrategyManager) -> PortfolioStrategy:
    """Return the default active portfolio strategy or raise if missing."""
    strategy = manager.get_default_strategy()
    if strategy is None:
        raise RuntimeError("no active default portfolio strategy")
    if not isinstance(strategy, PortfolioStrategy):
        raise TypeError("active strategy is a candidate strategy, not a portfolio strategy")
    return strategy


def build_portfolio_risk_engine(settings: Settings) -> PortfolioRiskEngine:
    """Wire the portfolio risk limits from configuration."""
    risk = settings.portfolio.risk
    risk_global = settings.risk
    return PortfolioRiskEngine(
        PortfolioRiskLimits(
            max_positions=risk.max_positions,
            max_position_weight=risk.max_position_weight,
            max_gross_exposure=risk.max_gross_exposure,
            max_net_exposure=risk.max_net_exposure,
            max_leverage=risk.max_leverage,
            maintenance_margin_buffer_pct=risk.maintenance_margin_buffer_pct,
            max_correlation=risk_global.max_correlation,
            max_correlated_positions=risk_global.max_correlated_positions,
            enable_correlation_filter=risk_global.enable_correlation_filter,
            correlation_lookback_bars=risk_global.correlation_lookback_bars,
        )
    )
