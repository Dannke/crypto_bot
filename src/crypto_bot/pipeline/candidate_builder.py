"""Candidate builder: transforms FeatureSets into scored candidates.

Applies filters, scoring, and strategy evaluation to transform raw
feature sets into scored trade candidates ready for selection.
"""
from __future__ import annotations

from datetime import UTC, datetime

from ..core.enums import RejectReason
from ..core.types import FeatureSet
from ..decision.decision_report import DecisionReport
from ..filters.base import Filter, FilterResult
from ..scoring.score_engine import ScoreEngine
from ..strategy.base import Strategy

_FILTER_REJECT: dict[str, RejectReason] = {
    "insufficient_liquidity": RejectReason.INSUFFICIENT_LIQUIDITY,
    "spread_too_wide": RejectReason.SPREAD_TOO_WIDE,
    "weak_trend": RejectReason.NO_DIRECTION,
    "no_clear_direction": RejectReason.NO_DIRECTION,
    "volatility_too_low": RejectReason.VOLATILITY_OUT_OF_RANGE,
    "volatility_too_high": RejectReason.VOLATILITY_OUT_OF_RANGE,
    "low_volume_score": RejectReason.INSUFFICIENT_LIQUIDITY,
    "low_absolute_volume": RejectReason.INSUFFICIENT_LIQUIDITY,
    "blacklisted": RejectReason.BLACKLISTED,
    "in_cooldown": RejectReason.IN_COOLDOWN,
}


def _reject_from_filter(result: FilterResult) -> RejectReason:
    return _FILTER_REJECT.get(result.reason, RejectReason.INSUFFICIENT_DATA)


def _reject_from_strategy(reason: str) -> RejectReason:
    text = reason.lower()
    if "conflict" in text:
        return RejectReason.CONFLICTING_TIMEFRAMES
    if "partial" in text:
        return RejectReason.LOW_CONFIDENCE
    if "no directional" in text or "missing timeframes" in text:
        return RejectReason.NO_DIRECTION
    return RejectReason.LOW_SCORE


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
        *,
        as_of_ms: int | None = None,
    ) -> tuple[DecisionReport | None, list[DecisionReport]]:
        """Build a candidate from feature sets.

        Args:
            symbol: Symbol being evaluated.
            features_by_tf: Features by timeframe.
            strategy: Strategy instance for evaluation.
            as_of_ms: Optional bar timestamp (epoch ms) for time-dependent
                filters (cooldown, etc.).  ``None`` = live mode (wall-clock).

        Returns:
            Tuple of (accepted_decision, rejected_reports).
            accepted_decision is None if the candidate was rejected.
        """
        # Use the primary (fastest) timeframe for filtering and scoring
        primary_tf = next(iter(features_by_tf)) if features_by_tf else None
        if primary_tf is None:
            return None, []

        features = features_by_tf[primary_tf]
        reference_ts = (
            datetime.fromtimestamp(as_of_ms / 1000, tz=UTC)
            if as_of_ms is not None
            else None
        )

        # Apply filters — collect all reject reasons, don't stop at first
        filter_results = []
        rejected = []
        kwargs = {} if reference_ts is None else {"reference_ts": reference_ts}
        for filter_instance in self._filters:
            result = filter_instance.evaluate(features, **kwargs)
            filter_results.append(result)
            if not result.passed:
                rejected.append(
                    DecisionReport.rejected_report(
                        symbol=symbol,
                        reject_reason=_reject_from_filter(result),
                        filter_result=result,
                        features=features,
                    )
                )

        if rejected:
            return None, rejected

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
                reject_reason=_reject_from_strategy(signal_result.reason),
                features=features,
                explanation=signal_result.reason,
            )
            return None, [report]

    def build_batch(
        self,
        features_by_symbol: dict[str, dict[str, FeatureSet]],
        strategy: Strategy,
        *,
        as_of_ms: int | None = None,
    ) -> tuple[list[DecisionReport], list[DecisionReport]]:
        """Build candidates for multiple symbols.

        Args:
            features_by_symbol: Dictionary of symbol -> features by timeframe.
            strategy: Strategy instance for evaluation.
            as_of_ms: Optional bar timestamp, forwarded to ``build()``.

        Returns:
            Tuple of (accepted_reports, rejected_reports).
        """
        accepted = []
        rejected = []

        for symbol, features_by_tf in features_by_symbol.items():
            accepted_report, rejected_reports = self.build(
                symbol, features_by_tf, strategy, as_of_ms=as_of_ms,
            )
            if accepted_report:
                accepted.append(accepted_report)
            rejected.extend(rejected_reports)

        return accepted, rejected

    def build_batch_per_tf(
        self,
        features_by_symbol: dict[str, dict[str, FeatureSet]],
        strategy: Strategy,
        *,
        as_of_ms: int | None = None,
    ) -> tuple[list[DecisionReport], list[DecisionReport]]:
        """Build one candidate per (symbol, timeframe) pair.

        Unlike ``build_batch`` (which evaluates one symbol across all its
        timeframes at once), this method treats each timeframe as an
        independent signal source.  Filters and the strategy are evaluated
        separately on each timeframe's features.

        Args:
            features_by_symbol: Dictionary of symbol -> features by timeframe.
            strategy: Strategy instance (e.g. ``SingleTfEngine``).
            as_of_ms: Optional bar timestamp, forwarded to ``build()``.

        Returns:
            Tuple of (accepted_reports, rejected_reports).
        """
        accepted = []
        rejected = []
        for symbol, features_by_tf in features_by_symbol.items():
            for tf, features in features_by_tf.items():
                single_tf = {tf: features}
                accepted_report, rejected_reports = self.build(
                    symbol, single_tf, strategy, as_of_ms=as_of_ms,
                )
                if accepted_report:
                    accepted.append(accepted_report)
                rejected.extend(rejected_reports)
        return accepted, rejected