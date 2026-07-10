"""Ranking engine: sorts candidates by score and selects top-N.

The ranking engine takes scored candidates and returns them in descending
order of score, optionally limiting to the top N candidates.
"""
from __future__ import annotations

from ..core.types import FeatureSet
from .score_engine import ScoreResult


class RankingEngine:
    """Engine for ranking candidates by score.

    Candidates are sorted by total score (descending), then by confidence
    (descending) as a tiebreaker.
    """

    def __init__(self) -> None:
        pass

    def rank(
        self,
        scored_candidates: list[ScoreResult],
        max_count: int | None = None,
    ) -> list[ScoreResult]:
        """Rank candidates by score and return top-N.

        Args:
            scored_candidates: List of ScoreResult objects.
            max_count: Maximum number of candidates to return. If None, returns all.

        Returns:
            Ranked list of ScoreResult objects, sorted by score descending.
        """
        # Sort by total score descending
        ranked = sorted(
            scored_candidates,
            key=lambda x: x.total_score,
            reverse=True,
        )

        # Limit to max_count if specified
        if max_count is not None and max_count > 0:
            ranked = ranked[:max_count]

        return ranked

    def rank_with_features(
        self,
        scored_candidates: list[tuple[ScoreResult, FeatureSet]],
        max_count: int | None = None,
    ) -> list[tuple[ScoreResult, FeatureSet]]:
        """Rank candidates by score while preserving associated features.

        Args:
            scored_candidates: List of (ScoreResult, FeatureSet) tuples.
            max_count: Maximum number of candidates to return.

        Returns:
            Ranked list of (ScoreResult, FeatureSet) tuples.
        """
        # Sort by total score descending
        ranked = sorted(
            scored_candidates,
            key=lambda x: x[0].total_score,
            reverse=True,
        )

        # Limit to max_count if specified
        if max_count is not None and max_count > 0:
            ranked = ranked[:max_count]

        return ranked
