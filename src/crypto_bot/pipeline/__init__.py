"""Pipeline layer: orchestrates the decision-making process.

The pipeline coordinates all components (filters, scoring, strategy, ML, fusion)
to transform raw market data into final trading decisions.
"""
from .candidate_builder import CandidateBuilder
from .candidate_selector import CandidateSelector
from .decision_pipeline import DecisionPipeline
from .factory import (
    DEFAULT_STRATEGY_NAME,
    build_candidate_builder,
    build_candidate_selector,
    build_decision_pipeline,
    build_filters,
    build_strategy_manager,
    build_strategy_registry,
    get_active_strategy,
    scoring_weights_from_settings,
    strategy_context_from_settings,
)

__all__ = [
    "CandidateBuilder",
    "CandidateSelector",
    "DecisionPipeline",
    "DEFAULT_STRATEGY_NAME",
    "build_candidate_builder",
    "build_candidate_selector",
    "build_decision_pipeline",
    "build_filters",
    "build_strategy_manager",
    "build_strategy_registry",
    "get_active_strategy",
    "scoring_weights_from_settings",
    "strategy_context_from_settings",
]
