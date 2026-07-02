"""Explanation generator: creates human-readable explanations for decisions.

Transforms technical decision data into clear, natural language explanations
that traders can understand.
"""
from __future__ import annotations

from ..core.enums import Signal, Side
from .decision_report import DecisionReport


class ExplanationGenerator:
    """Generates human-readable explanations for trading decisions."""

    def __init__(self) -> None:
        pass

    def generate(self, report: DecisionReport) -> str:
        """Generate a natural language explanation for a decision.

        Args:
            report: The decision report to explain.

        Returns:
            Human-readable explanation string.
        """
        if report.rejected:
            return self._generate_rejection_explanation(report)
        return self._generate_acceptance_explanation(report)

    def _generate_acceptance_explanation(self, report: DecisionReport) -> str:
        """Generate explanation for an accepted trade."""
        parts = []

        # Signal and direction
        direction = report.side.value if report.side else "unknown"
        parts.append(f"Signal: {report.signal.value} {direction.upper()}")

        # Score breakdown
        parts.append(f"Total Score: {report.total_score:.1f}/100")
        parts.append("Score breakdown:")
        for criterion, score in report.sub_scores.items():
            weight = report.weights.get(criterion, 0.0)
            parts.append(f"  - {criterion}: {score:.2f} (weight: {weight:.2f})")

        # Key features
        if report.features:
            parts.append("Key market conditions:")
            if "adx" in report.features:
                parts.append(f"  - ADX (trend strength): {report.features['adx']:.1f}")
            if "rsi" in report.features:
                parts.append(f"  - RSI (momentum): {report.features['rsi']:.1f}")
            if "atr_pct" in report.features:
                parts.append(f"  - ATR% (volatility): {report.features['atr_pct']:.2f}%")

        # Strategy and confidence
        if report.strategy_name:
            parts.append(f"Strategy: {report.strategy_name}")
        parts.append(f"Confidence: {report.confidence:.1%}")

        # Source
        parts.append(f"Decision source: {report.source}")

        return "\n".join(parts)

    def _generate_rejection_explanation(self, report: DecisionReport) -> str:
        """Generate explanation for a rejected candidate."""
        parts = []

        # Rejection reason
        if report.rejected_by:
            parts.append(f"Rejected by filter: {report.rejected_by}")
        if report.reject_reason:
            parts.append(f"Reason: {report.reject_reason.value}")

        # Filter details
        if report.filter_results:
            parts.append("Filter results:")
            for fr in report.filter_results:
                status = "✓ PASS" if fr.passed else "✗ REJECT"
                parts.append(f"  - {fr.filter_name}: {status}")
                if fr.detail:
                    parts.append(f"    {fr.detail}")

        # Score (if computed before rejection)
        if report.total_score > 0:
            parts.append(f"Score before rejection: {report.total_score:.1f}/100")

        return "\n".join(parts)

    def generate_summary(self, report: DecisionReport) -> str:
        """Generate a one-line summary of the decision."""
        if report.rejected:
            reason = report.reject_reason.value if report.reject_reason else "unknown"
            return f"REJECT {report.symbol}: {reason}"
        else:
            direction = report.side.value if report.side else "unknown"
            return f"{report.signal.value} {report.symbol} {direction.upper()} (score: {report.total_score:.1f}, confidence: {report.confidence:.1%})"
