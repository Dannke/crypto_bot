"""Decision pipeline: orchestrates the complete decision-making process.

The pipeline coordinates all components to transform raw market data into
final trading decisions. It serves as the main entry point for the
decision intelligence layer.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import replace
from typing import Any

from ..core.types import FeatureSet
from ..decision.decision_report import DecisionReport
from ..decision.explanation import ExplanationGenerator
from ..future.fusion import ClassicalOnlyFusion, FusionEngine, FusedDecision
from ..ml.predictor import Predictor
from ..pipeline.candidate_builder import CandidateBuilder
from ..pipeline.candidate_selector import CandidateSelector, SelectionConfig
from ..strategy.base import Strategy


class DecisionPipeline:
    """Main pipeline for generating trading decisions.

    The pipeline orchestrates:
    1. Candidate building (filters, scoring, strategy evaluation)
    2. Candidate selection (ranking, thresholds)
    3. Decision fusion (classical + ML)
    4. Decision reporting (explainable output)

    This is the primary interface for the decision intelligence layer.
    """

    def __init__(
        self,
        builder: CandidateBuilder | None = None,
        selector: CandidateSelector | None = None,
        fusion_engine: FusionEngine | None = None,
        ml_predictor: Predictor | None = None,
        explanation_generator: Any = None,
    ) -> None:
        self._builder = builder or CandidateBuilder()
        self._selector = selector or CandidateSelector()
        self._fusion_engine = fusion_engine or ClassicalOnlyFusion()
        self._ml_predictor = ml_predictor or Predictor()
        self._explanation_generator = explanation_generator or ExplanationGenerator()

    def process(
        self,
        features_by_symbol: dict[str, dict[str, FeatureSet]],
        strategy: Strategy,
        selection_config: SelectionConfig | None = None,
        *,
        per_timeframe: bool = False,
    ) -> dict[str, Any]:
        """Process feature sets and generate trading decisions.

        Args:
            features_by_symbol: Dictionary of symbol -> features by timeframe.
            strategy: Strategy instance for evaluation.
            selection_config: Optional selection configuration.
            per_timeframe: If True, evaluate each timeframe as an independent
                signal source (requires a strategy like ``SingleTfEngine``).

        Returns:
            Dictionary with processing results including selected candidates,
            rejected candidates, and statistics.
        """
        if per_timeframe:
            return self._process_per_timeframe(features_by_symbol, strategy, selection_config)

        # Update selector config if provided
        if selection_config:
            self._selector = CandidateSelector(selection_config)

        # Build candidates. `accepted_reports` are candidates that passed every
        # filter AND got a directional signal from the strategy — they still
        # need to clear the selector's score/confidence threshold below.
        # `rejected_reports` are everything that was dropped earlier (a failed
        # filter, or a HOLD/conflicting-timeframes verdict from the strategy).
        accepted_reports, rejected_reports = self._builder.build_batch(
            features_by_symbol,
            strategy,
        )

        # Select best candidates from those that cleared the builder stage.
        selected = self._selector.select(accepted_reports)

        # Anything that cleared filters + got a signal, but didn't make the
        # final cut (score/confidence threshold, or max_candidates_per_cycle),
        # is also a rejection — fold it into the same reporting bucket so the
        # full population is accounted for: processed == len(selected) + len(all_rejected).
        below_threshold = [r for r in accepted_reports if r not in selected]
        all_rejected = rejected_reports + below_threshold

        # Apply fusion (currently classical only)
        fused_decisions = []
        for report in selected:
            fused = self._apply_fusion(report)
            fused_decisions.append(fused)

        # Generate explanations
        explained_selected = []
        for report in selected:
            explanation = self._explanation_generator.generate(report)
            explained_selected.append(replace(report, explanation=explanation))
        selected = explained_selected

        # Compile results. `get_selection_stats` only knows about the builder
        # stage's "accepted" population, so recompute the top-level counters
        # here from the full picture instead of trusting it blindly.
        stats = self._selector.get_selection_stats(accepted_reports, selected)
        stats["rejected_count"] = len(all_rejected)
        stats["reject_reasons"] = dict(
            Counter(
                r.reject_reason.value if r.reject_reason else "unknown"
                for r in all_rejected
            )
        )

        return {
            "selected": selected,
            "rejected": all_rejected,
            "accepted": accepted_reports,
            "fused_decisions": fused_decisions,
            "stats": stats,
            "total_processed": len(features_by_symbol),
        }

    def _process_per_timeframe(
        self,
        features_by_symbol: dict[str, dict[str, FeatureSet]],
        strategy: Strategy,
        selection_config: SelectionConfig | None = None,
    ) -> dict[str, Any]:
        """Evaluate each timeframe independently.

        Iterates over every (symbol, timeframe) pair as a separate candidate,
        runs filters + strategy per pair, then selects the best overall.
        """
        if selection_config:
            self._selector = CandidateSelector(selection_config)

        accepted_reports, rejected_reports = self._builder.build_batch_per_tf(
            features_by_symbol,
            strategy,
        )

        selected = self._selector.select(accepted_reports)
        below_threshold = [r for r in accepted_reports if r not in selected]
        all_rejected = rejected_reports + below_threshold

        fused_decisions = []
        for report in selected:
            fused = self._apply_fusion(report)
            fused_decisions.append(fused)

        explained_selected = []
        for report in selected:
            explanation = self._explanation_generator.generate(report)
            explained_selected.append(replace(report, explanation=explanation))
        selected = explained_selected

        # Count pairs (symbol × timeframe) as processed
        total_pairs = sum(len(tfs) for tfs in features_by_symbol.values())

        stats = self._selector.get_selection_stats(accepted_reports, selected)
        stats["rejected_count"] = len(all_rejected)
        stats["reject_reasons"] = dict(
            Counter(
                r.reject_reason.value if r.reject_reason else "unknown"
                for r in all_rejected
            )
        )

        return {
            "selected": selected,
            "rejected": all_rejected,
            "accepted": accepted_reports,
            "fused_decisions": fused_decisions,
            "stats": stats,
            "total_processed": total_pairs,
        }

    def process_single(
        self,
        symbol: str,
        features_by_tf: dict[str, FeatureSet],
        strategy: Strategy,
    ) -> DecisionReport | None:
        """Process a single symbol and generate a decision.

        Args:
            symbol: Symbol to process.
            features_by_tf: Features by timeframe.
            strategy: Strategy instance for evaluation.

        Returns:
            DecisionReport if accepted, None if rejected.
        """
        accepted_report, rejected_reports = self._builder.build(
            symbol,
            features_by_tf,
            strategy,
        )

        if accepted_report:
            # Apply fusion
            fused = self._apply_fusion(accepted_report)
            explanation = self._explanation_generator.generate(accepted_report)
            return replace(accepted_report, explanation=explanation)

        return None

    def _apply_fusion(self, report: DecisionReport) -> FusedDecision:
        """Apply fusion engine to a decision report.

        Args:
            report: Decision report to fuse.

        Returns:
            FusedDecision with combined signal.
        """
        # Get ML prediction (stub for now)
        ml_prediction = None
        if self._ml_predictor.is_enabled:
            # Extract features for ML
            features = report.features
            ml_prediction = self._ml_predictor.predict(features)

        # Apply fusion
        fused = self._fusion_engine.fuse(
            classical_signal=report.signal,
            classical_side=report.side,
            classical_confidence=report.confidence,
            ml_prediction=ml_prediction,
            ml_weight=0.0,  # Currently classical only
        )

        # Update symbol in fused decision
        return FusedDecision(
            symbol=report.symbol,
            signal=fused.signal,
            side=fused.side,
            confidence=fused.confidence,
            method=fused.method,
            classical_signal=fused.classical_signal,
            classical_side=fused.classical_side,
            classical_confidence=fused.classical_confidence,
            ml_signal=fused.ml_signal,
            ml_side=fused.ml_side,
            ml_confidence=fused.ml_confidence,
            ml_weight=fused.ml_weight,
        )

    def enable_ml(self) -> None:
        """Enable ML predictions in the pipeline."""
        self._ml_predictor.enable()

    def disable_ml(self) -> None:
        """Disable ML predictions in the pipeline."""
        self._ml_predictor.disable()

    @property
    def builder(self) -> CandidateBuilder:
        """Get the candidate builder."""
        return self._builder

    @property
    def selector(self) -> CandidateSelector:
        """Get the candidate selector."""
        return self._selector

    @property
    def fusion_engine(self) -> FusionEngine:
        """Get the fusion engine."""
        return self._fusion_engine

    @property
    def ml_predictor(self) -> Predictor:
        """Get the ML predictor."""
        return self._ml_predictor