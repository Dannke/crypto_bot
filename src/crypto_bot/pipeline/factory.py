"""Composition factory for the Decision Intelligence Layer.

Wires validated ``Settings`` into filters, scoring, strategy, and the
``DecisionPipeline``. Keeps dependency construction out of the orchestrator
so the decision layer stays testable in isolation.
"""
from __future__ import annotations

from datetime import datetime

from ..config.schemas import Settings
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
from ..scoring.score_engine import ScoreEngine
from ..scoring.weights import ScoringWeights
from ..strategy.base import Strategy, StrategyContext
from ..strategy.manager import StrategyManager
from ..strategy.registry import StrategyRegistry
from ..strategy.signal_engine import SignalEngine
from ..strategy.single_tf import SingleTfEngine

DEFAULT_STRATEGY_NAME = "confluence"
PER_TF_STRATEGY_NAME = "per_timeframe"


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
    registry.register(DEFAULT_STRATEGY_NAME, SignalEngine)
    registry.register(PER_TF_STRATEGY_NAME, SingleTfEngine)
    return registry


def build_strategy_manager(
    settings: Settings,
    *,
    strategy_name: str = DEFAULT_STRATEGY_NAME,
    registry: StrategyRegistry | None = None,
) -> StrategyManager:
    """Create a strategy manager with the configured default strategy active."""
    reg = registry or build_strategy_registry()
    manager = StrategyManager(registry=reg)
    manager.set_default_strategy(strategy_name)
    ctx = strategy_context_from_settings(settings)
    manager.activate_strategy(
        strategy_name,
        ctx,
        settings.timeframes.primary,
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


def get_active_strategy(manager: StrategyManager) -> Strategy:
    """Return the default active strategy or raise if missing."""
    strategy = manager.get_default_strategy()
    if strategy is None:
        raise RuntimeError("no active default strategy")
    return strategy