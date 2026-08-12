"""Decision report: comprehensive record of a trading decision.

Captures all information needed to explain why a decision was made,
including scores, filter results, and the final signal.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from ..core.enums import RejectReason, Side, Signal
from ..core.types import FeatureSet
from ..filters.base import FilterResult
from ..scoring.score_engine import ScoreResult


@dataclass(frozen=True, slots=True)
class DecisionReport:
    """Complete report of a trading decision.

    This report provides full transparency into the decision-making process,
    enabling auditability and explainability of every trade.
    """

    symbol: str
    signal: Signal
    side: Side | None
    confidence: float
    timestamp: datetime = field(default_factory=lambda: datetime.now(tz=UTC))

    # Scoring information
    total_score: float = 0.0
    sub_scores: dict[str, float] = field(default_factory=dict)
    weights: dict[str, float] = field(default_factory=dict)

    # Filter results
    filter_results: list[FilterResult] = field(default_factory=list)
    rejected_by: str | None = None
    reject_reason: RejectReason | None = None

    # Feature snapshot
    features: dict[str, float | str] = field(default_factory=dict)

    # Additional context
    strategy_name: str = ""
    source: str = "classical"  # "classical", "ml", "fusion"
    explanation: str = ""

    @property
    def accepted(self) -> bool:
        """Whether the candidate was accepted for trading."""
        return self.signal != Signal.HOLD and self.side is not None

    @property
    def rejected(self) -> bool:
        """Whether the candidate was rejected."""
        return not self.accepted

    def to_dict(self) -> dict:
        """Convert report to dictionary for logging/serialization."""
        return {
            "symbol": self.symbol,
            "signal": self.signal.value,
            "side": self.side.value if self.side else None,
            "confidence": self.confidence,
            "timestamp": self.timestamp.isoformat(),
            "total_score": self.total_score,
            "sub_scores": self.sub_scores,
            "weights": self.weights,
            "rejected_by": self.rejected_by,
            "reject_reason": self.reject_reason.value if self.reject_reason else None,
            "strategy_name": self.strategy_name,
            "source": self.source,
            "explanation": self.explanation,
            "filter_results": [
                {
                    "filter": r.filter_name,
                    "outcome": r.outcome.value,
                    "reason": r.reason,
                    "detail": r.detail,
                }
                for r in self.filter_results
            ],
        }

    @classmethod
    def _ts_from_features(cls, features: FeatureSet | None) -> datetime:
        if features and features.candle_timestamp_ms > 0:
            return datetime.fromtimestamp(features.candle_timestamp_ms / 1000, tz=UTC)
        return datetime.now(tz=UTC)

    @classmethod
    def from_score_result(
        cls,
        score_result: ScoreResult,
        signal: Signal,
        side: Side | None,
        confidence: float,
        features: FeatureSet,
        filter_results: list[FilterResult] | None = None,
        strategy_name: str = "",
        explanation: str = "",
    ) -> DecisionReport:
        """Create a DecisionReport from a ScoreResult and additional context."""
        return cls(
            timestamp=cls._ts_from_features(features),
            symbol=score_result.symbol,
            signal=signal,
            side=side,
            confidence=confidence,
            total_score=score_result.total_score,
            sub_scores={
                "trend": score_result.sub_scores.trend,
                "momentum": score_result.sub_scores.momentum,
                "volume": score_result.sub_scores.volume,
                "volatility": score_result.sub_scores.volatility,
                "liquidity": score_result.sub_scores.liquidity,
                "spread": score_result.sub_scores.spread,
                "risk": score_result.sub_scores.risk,
            },
            weights={
                "trend": score_result.weights.trend,
                "momentum": score_result.weights.momentum,
                "volume": score_result.weights.volume,
                "volatility": score_result.weights.volatility,
                "liquidity": score_result.weights.liquidity,
                "spread": score_result.weights.spread,
                "risk": score_result.weights.risk,
            },
            filter_results=filter_results or [],
            features={
                "adx": features.adx,
                "rsi": features.rsi,
                "atr_pct": features.atr_pct,
                "trend_score": features.trend_score,
                "momentum_score": features.momentum_score,
                "volatility_score": features.volatility_score,
                "volume_score": features.volume_score,
                "liquidity_score": features.liquidity_score,
                "spread_pct": features.spread_pct,
                "last_close": features.extras.get("last_close", features.close),
                "close": features.close,
                "ema_fast": features.ema_fast,
                "timeframe": features.timeframe,
            },
            strategy_name=strategy_name,
            explanation=explanation,
        )

    @classmethod
    def rejected_report(
        cls,
        symbol: str,
        reject_reason: RejectReason,
        filter_result: FilterResult | None = None,
        features: FeatureSet | None = None,
        explanation: str = "",
    ) -> DecisionReport:
        """Create a DecisionReport for a rejected candidate."""
        return cls(
            timestamp=cls._ts_from_features(features),
            symbol=symbol,
            signal=Signal.HOLD,
            side=None,
            confidence=0.0,
            rejected_by=filter_result.filter_name if filter_result else None,
            reject_reason=reject_reason,
            filter_results=[filter_result] if filter_result else [],
            features={
                "adx": features.adx if features else 0.0,
                "rsi": features.rsi if features else 0.0,
                "atr_pct": features.atr_pct if features else 0.0,
                "timeframe": features.timeframe if features else "",
            } if features else {},
            explanation=explanation,
        )
