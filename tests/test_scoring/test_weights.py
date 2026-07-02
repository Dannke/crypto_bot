"""Tests for ScoringWeights."""
from crypto_bot.scoring.weights import ScoringWeights


def test_weights_total():
    weights = ScoringWeights()
    total = weights.total()
    assert total > 0


def test_weights_normalized():
    weights = ScoringWeights(trend=0.5, momentum=0.3, volume=0.2)
    normalized = weights.normalized()
    assert abs(normalized.total() - 1.0) < 0.0001


def test_weights_normalized_zero_total():
    weights = ScoringWeights(trend=0.0, momentum=0.0, volume=0.0)
    normalized = weights.normalized()
    assert normalized.total() > 0  # Should return defaults
