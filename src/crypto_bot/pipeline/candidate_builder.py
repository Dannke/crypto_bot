"""Candidate builder: transforms FeatureSets into scored candidates.

Applies filters, scoring, and strategy evaluation to transform raw
feature sets into scored trade candidates ready for selection.
"""
from __future__ import annotations

from typing import Any

from ..core.types import FeatureSet, SignalResult
from ..decision.decision_report import DecisionReport
from ..filters.base import Filter
from ..scoring.score_engine import ScoreEngine, ScoreResult
from ..strategy.base import Strategy


class CandidateBuilder:
    """Builds scored candidates from feature sets.

    The builder process:
    1. Apply filters to reject low-quality candidates
    2. Score remaining candidates using the score engine
    3. Evaluate with strategy to get signals
    4. Generate decision reports for all candidates
    """

    def __init__(
        self,
        filters: list[Filter] | None = None,
        score_engine: ScoreEngine | None = None,
    ) -> None:
        self._filters = filters or []
        self._score_engine = score_engine or ScoreEngine()

    def add_filter(self, filter_instance: Filter) -> None:
        """Add a filter to the builder.

        Args:
            filter_instance: Filter instance to add.
        """
        self._filters.append(filter_instance)

    def remove_filter(self, filter_name: str) -> None:
        """Remove a filter by name.

        Args:
            filter_name: Name of the filter to remove.
        """
        self._filters = [f for f in self._filters if f.name != filter_name]

    def build(
        self,
        symbol: str,
        features_by_tf: dict[str, FeatureSet],
        strategy: Strategy,
    ) -> tuple[DecisionReport | None, list[DecisionReport]]:
        """Build a candidate from feature sets.

        Args:
            symbol: Symbol being evaluated.
            features_by_tf: Features by timeframe.
            strategy: Strategy instance for evaluation.

        Returns:
            Tuple of (accepted_decision, rejected_reports).
            accepted_decision is None if the candidate was rejected.
        """
        # Use the primary (fastest) timeframe for filtering and scoring
        primary_tf = next(iter(features_by_tf)) if features_by_tf else None
        if primary_tf is None:
            return None, []

        features = features_by_tf[primary_tf]

        # Apply filters
        filter_results = []
        for filter_instance in self._filters:
            result = filter_instance.evaluate(features)
            filter_results.append(result)
            if not result.passed:
                # Rejected by filter
                report = DecisionReport.rejected_report(
                    symbol=symbol,
                    reject_reason="insufficient_data",  # Generic, will be overridden
                    filter_result=result,
                    features=features,
                )
                return None, [report]

        # Score the candidate
        score_result = self._score_engine.compute(features)

        # Evaluate with strategy
        signal_result = strategy.evaluate(symbol, features_by_tf)

        # Create decision report
        if signal_result.signal.value != "HOLD" and signal_result.side is not None:
            report = DecisionReport.from_score_result(
                score_result=score_result,
                signal=signal_result.signal,
                side=signal_result.side,
                confidence=signal_result.confidence,
                features=features,
                filter_results=filter_results,
                strategy_name=strategy.__class__.__name__,
            )
            return report, []
        else:
            # Strategy rejected
            report = DecisionReport.rejected_report(
                symbol=symbol,
                reject_reason="low_score",  # Will be refined
                features=features,
                explanation=signal_result.reason,
            )
            return None, [report]

    def build_batch(
        self,
        features_by_symbol: dict[str, dict[str, FeatureSet]],
        strategy: Strategy,
    ) -> tuple[list[DecisionReport], list[DecisionReport]]:
        """Build candidates for multiple symbols.

        Args:
            features_by_symbol: Dictionary of symbol -> features by timeframe.
            strategy: Strategy instance for evaluation.

        Returns:
            Tuple of (accepted_reports, rejected_reports).
        """
        accepted = []
        rejected = []

        for symbol, features_by_tf in features_by_symbol.items():
            accepted_report, rejected_reports = self.build(symbol, features_by_tf, strategy)
            if accepted_report:
                accepted.append(accepted_report)
            rejected.extend(rejected_reports)

        return accepted, rejected
