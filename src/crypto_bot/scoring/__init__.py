"""Scoring layer: multi-criteria evaluation and ranking of candidates.

The scoring system combines multiple sub-scores (trend, momentum, volume, etc.)
with configurable weights to produce a final 0-100 score for each candidate.
"""
from .normalization import Normalizer
from .ranking import RankingEngine
from .score_engine import ScoreEngine
from .weights import ScoringWeights

__all__ = [
    "Normalizer",
    "RankingEngine",
    "ScoreEngine",
    "ScoringWeights",
]
