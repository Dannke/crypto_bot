"""Candidate selector: selects best candidates from scored candidates.

Ranks candidates by score and applies selection logic (top-N, thresholds)
to determine which candidates to trade.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..decision.decision_report import DecisionReport
from ..scoring.ranking import RankingEngine


@dataclass(frozen=True, slots=True)
class SelectionConfig:
    """Configuration for candidate selection."""

    max_candidates: int = 5  # Maximum number of candidates to select
    min_score: float = 65.0  # Minimum score threshold
    min_confidence: float = 0.6  # Minimum confidence threshold


class CandidateSelector:
    """Selects the best candidates from scored decision reports.

    Applies ranking and filtering to determine which candidates should
    be traded based on score, confidence, and other criteria.
    """

    def __init__(
        self,
        config: SelectionConfig | None = None,
        ranking_engine: RankingEngine | None = None,
    ) -> None:
        self._config = config or SelectionConfig()
        self._ranking_engine = ranking_engine or RankingEngine()

    def select(
        self,
        reports: list[DecisionReport],
    ) -> list[DecisionReport]:
        """Select the best candidates from decision reports.

        Args:
            reports: List of decision reports (accepted candidates only).

        Returns:
            List of selected decision reports, sorted by score.
        """
        # Filter by thresholds
        filtered = [
            r for r in reports
            if r.total_score >= self._config.min_score
            and r.confidence >= self._config.min_confidence
        ]

        # Rank by score
        ranked = sorted(
            filtered,
            key=lambda x: x.total_score,
            reverse=True,
        )

        # Limit to max candidates
        selected = ranked[: self._config.max_candidates]

        return selected

    def select_with_risk_limits(
        self,
        reports: list[DecisionReport],
        max_positions: int = 3,
        max_risk_per_trade: float = 2.0,
    ) -> list[DecisionReport]:
        """Select candidates with risk management constraints.

        Args:
            reports: List of decision reports.
            max_positions: Maximum number of concurrent positions.
            max_risk_per_trade: Maximum risk percentage per trade.

        Returns:
            List of selected decision reports respecting risk limits.
        """
        # First apply standard selection
        selected = self.select(reports)

        # Further limit by max positions
        selected = selected[:max_positions]

        # Filter by risk (if risk info available in features)
        # This is a placeholder - actual risk filtering would need
        # access to position sizing logic
        return selected

    def get_selection_stats(
        self,
        reports: list[DecisionReport],
        selected: list[DecisionReport],
    ) -> dict[str, Any]:
        """Get statistics about the selection process.

        Args:
            reports: All input reports.
            selected: Selected reports.

        Returns:
            Dictionary with selection statistics.
        """
        return {
            "total_candidates": len(reports),
            "selected_count": len(selected),
            "rejected_count": len(reports) - len(selected),
            "avg_score": sum(r.total_score for r in selected) / len(selected) if selected else 0.0,
            "avg_confidence": sum(r.confidence for r in selected) / len(selected) if selected else 0.0,
            "min_score_threshold": self._config.min_score,
            "min_confidence_threshold": self._config.min_confidence,
        }
