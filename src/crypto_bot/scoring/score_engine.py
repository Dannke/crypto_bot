"""Score engine: combines multiple criteria into a final 0-100 score.

The score engine evaluates candidates across multiple dimensions (trend, momentum,
volume, volatility, liquidity, spread, risk) and combines them using configurable
weights to produce a final score.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..core.types import FeatureSet
from .normalization import Normalizer
from .weights import ScoringWeights


@dataclass(frozen=True, slots=True)
class SubScores:
    """Individual sub-scores for each criterion."""

    trend: float
    momentum: float
    volume: float
    volatility: float
    liquidity: float
    spread: float
    risk: float


@dataclass(frozen=True, slots=True)
class ScoreResult:
    """Result of scoring a candidate."""

    symbol: str
    total_score: float  # 0-100
    sub_scores: SubScores
    weights: ScoringWeights


class ScoreEngine:
    """Engine for computing composite scores from feature sets.

    The scoring process:
    1. Extract raw feature values
    2. Normalize each to [0, 1]
    3. Apply weights
    4. Combine into final 0-100 score
    """

    def __init__(
        self,
        weights: ScoringWeights | None = None,
        normalizer: Normalizer | None = None,
        btc_reference_symbol: str = "BTC/USDT",
    ) -> None:
        self._weights = (weights or ScoringWeights()).normalized()
        self._normalizer = normalizer or Normalizer()
        self._btc_reference_symbol = btc_reference_symbol

    @property
    def weights(self) -> ScoringWeights:
        return self._weights

    def compute(self, features: FeatureSet) -> ScoreResult:
        """Compute a composite score for a candidate.

        Args:
            features: The feature set for the symbol being scored.

        Returns:
            ScoreResult with total score and individual sub-scores.
        """
        # Compute individual sub-scores (0-1 each)
        sub_scores = SubScores(
            trend=self._score_trend(features),
            momentum=self._score_momentum(features),
            volume=self._score_volume(features),
            volatility=self._score_volatility(features),
            liquidity=self._score_liquidity(features),
            spread=self._score_spread(features),
            risk=self._score_risk(features),
        )

        # Weighted sum
        weighted_sum = (
            self._weights.trend * sub_scores.trend
            + self._weights.momentum * sub_scores.momentum
            + self._weights.volume * sub_scores.volume
            + self._weights.volatility * sub_scores.volatility
            + self._weights.liquidity * sub_scores.liquidity
            + self._weights.spread * sub_scores.spread
            + self._weights.risk * sub_scores.risk
        )

        # Scale to 0-100
        total_score = weighted_sum * 100.0

        return ScoreResult(
            symbol=features.symbol,
            total_score=round(total_score, 2),
            sub_scores=sub_scores,
            weights=self._weights,
        )

    def _score_trend(self, features: FeatureSet) -> float:
        """Score based on trend strength and direction."""
        # Use the pre-computed trend_score from FeatureSet
        # This already incorporates ADX and EMA alignment
        return self._normalizer.clamp(features.trend_score)

    def _score_momentum(self, features: FeatureSet) -> float:
        """Score based on momentum (RSI deviation from midpoint)."""
        # Use the pre-computed momentum_score from FeatureSet
        return self._normalizer.clamp(features.momentum_score)

    def _score_volume(self, features: FeatureSet) -> float:
        """Score based on volume confirmation."""
        # Use the pre-computed volume_score from FeatureSet
        return self._normalizer.clamp(features.volume_score)

    def _score_volatility(self, features: FeatureSet) -> float:
        """Score based on volatility being in the optimal range."""
        # Use the pre-computed volatility_score from FeatureSet
        # This already incorporates ATR% band logic
        return self._normalizer.clamp(features.volatility_score)

    def _score_liquidity(self, features: FeatureSet) -> float:
        """Score based on liquidity (quote volume and depth)."""
        # Use the liquidity_score from extended FeatureSet
        return self._normalizer.clamp(features.liquidity_score)

    def _score_spread(self, features: FeatureSet) -> float:
        """Score based on spread (tighter is better)."""
        # Lower spread is better - invert the score
        # Assume 0% spread = 1.0, 1% spread = 0.0
        spread_score = 1.0 - self._normalizer.min_max(features.spread_pct, 0.0, 1.0)
        return self._normalizer.clamp(spread_score)

    def _score_risk(self, features: FeatureSet) -> float:
        """Score based on risk factors (correlation, regime, etc.)."""
        # Combine multiple risk factors
        # Lower correlation with BTC/ETH can be good for diversification
        # Neutral regime is safer than extreme regimes

        regime_score = 1.0
        if features.market_regime == "choppy":
            regime_score = 0.3
        elif features.market_regime in ("bull", "bear"):
            regime_score = 0.7

        # Correlation: moderate correlation (0.3-0.7) is ideal
        # For the reference symbol (BTC/USDT), self-correlation is 1.0 by
        # construction — skip the penalty since it's a tautology, not a signal.
        if features.symbol == self._btc_reference_symbol:
            corr_score = 1.0
        else:
            corr_btc = abs(features.correlation_btc)
            corr_score = 1.0 - abs(corr_btc - 0.5) * 2.0  # Peak at 0.5

        risk_score = (regime_score + corr_score) / 2.0
        return self._normalizer.clamp(risk_score)
