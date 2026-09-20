"""Portfolio risk: limits, volatility sizing, and per-position rejections.

The engine consumes a :class:`PortfolioIntent` (produced by a portfolio
strategy) and returns an :class:`PortfolioRiskReport` with an adjusted
intent plus a per-position reason for every rejection.  Reasons use
:class:`PortfolioRejectReason` so downstream ``DecisionReport`` generation
can record why a target was not tradable.

Constraint order is deterministic: max positions, per-position weight,
gross exposure, then net exposure.  Volatility sizing (inverse-vol
scaling renormalized to the approved gross) runs afterwards and is
re-checked against the same limits.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from statistics import median

from ..core.enums import PortfolioRejectReason, Side
from .models import (
    CrossSectionalFeatureSnapshot,
    PortfolioIntent,
    PortfolioState,
    PositionIntent,
)

_FALLBACK_VOLATILITY_PCT = 0.0


def _finite(value: float, field_name: str, *, non_negative: bool = False, positive: bool = False) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(float(value)):
        raise ValueError(f"{field_name} must be a finite number")
    if non_negative and value < 0:
        raise ValueError(f"{field_name} must be non-negative")
    if positive and value <= 0:
        raise ValueError(f"{field_name} must be greater than zero")


def _count(value: int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class PortfolioRiskLimits:
    """Risk limits for one portfolio evaluation."""

    max_positions: int
    max_position_weight: float
    max_gross_exposure: float
    max_net_exposure: float
    max_leverage: float = 1.0
    maintenance_margin_buffer_pct: float = 0.0

    # R8: Correlation and clustering risk controls
    max_correlation: float = 0.7
    max_correlated_positions: int = 2
    enable_correlation_filter: bool = True
    correlation_lookback_bars: int = 168

    def __post_init__(self) -> None:
        _count(self.max_positions, "max_positions")
        _finite(self.max_position_weight, "max_position_weight", non_negative=True)
        _finite(self.max_gross_exposure, "max_gross_exposure", non_negative=True)
        _finite(self.max_net_exposure, "max_net_exposure", non_negative=True)
        _finite(self.max_leverage, "max_leverage", positive=True)
        _finite(self.maintenance_margin_buffer_pct, "maintenance_margin_buffer_pct", non_negative=True)
        if self.max_leverage < 1.0:
            raise ValueError("max_leverage must be >= 1.0")
        if not 0.0 <= self.max_correlation <= 1.0:
            raise ValueError("max_correlation must be in [0, 1]")
        if self.max_correlated_positions < 1:
            raise ValueError("max_correlated_positions must be >= 1")
        if self.correlation_lookback_bars < 24:
            raise ValueError("correlation_lookback_bars must be >= 24")


@dataclass(frozen=True, slots=True)
class VolatilitySizingParams:
    """Controls optional inverse-volatility weight adjustment.

    Weights are scaled by ``reference_volatility / volatility`` and
    renormalized to the approved gross exposure; scale/halve nothing when
    no volatility information is available.
    """

    source: str = "atr_pct"
    reference_volatility_pct: float | None = None
    fallback_volatility_pct: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("source must be a non-empty string")
        for field_name in ("reference_volatility_pct", "fallback_volatility_pct"):
            value = getattr(self, field_name)
            if value is not None:
                _finite(value, field_name, positive=True)


@dataclass(frozen=True, slots=True)
class PositionRiskResult:
    """Verdict for one requested position."""

    symbol: str
    side: Side
    requested_weight: float
    granted_weight: float
    accepted: bool
    timeframe: str | None = None
    reason: PortfolioRejectReason | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.side, Side):
            raise ValueError("side must be a Side")
        _finite(self.requested_weight, "requested_weight", positive=True)
        _finite(self.granted_weight, "granted_weight", non_negative=True)
        if self.reason is not None and not isinstance(self.reason, PortfolioRejectReason):
            raise ValueError("reason must be a PortfolioRejectReason")


@dataclass(frozen=True, slots=True)
class PortfolioRiskReport:
    """Outcome of a portfolio risk evaluation."""

    accepted: bool
    adjusted_intent: PortfolioIntent
    position_results: tuple[PositionRiskResult, ...]
    gross_exposure: float
    net_exposure: float
    rejected_reasons: tuple[PortfolioRejectReason, ...]

    def __post_init__(self) -> None:
        _finite(self.gross_exposure, "gross_exposure", non_negative=True)
        _finite(self.net_exposure, "net_exposure", non_negative=True)
        if not isinstance(self.adjusted_intent, PortfolioIntent):
            raise ValueError("adjusted_intent must be a PortfolioIntent")
        if not isinstance(self.position_results, tuple):
            object.__setattr__(self, "position_results", tuple(self.position_results))
        if not isinstance(self.rejected_reasons, tuple):
            object.__setattr__(self, "rejected_reasons", tuple(self.rejected_reasons))


class PortfolioRiskEngine:
    """Applies portfolio limits and optional volatility sizing to an intent."""

    def __init__(self, limits: PortfolioRiskLimits) -> None:
        if not isinstance(limits, PortfolioRiskLimits):
            raise ValueError("limits must be a PortfolioRiskLimits")
        self._limits = limits

    @property
    def limits(self) -> PortfolioRiskLimits:
        return self._limits

    def evaluate(
        self,
        intent: PortfolioIntent,
        state: PortfolioState,
        features: CrossSectionalFeatureSnapshot | None = None,
        *,
        sizing: VolatilitySizingParams | None = None,
    ) -> PortfolioRiskReport:
        """Check and adjust one portfolio intent against the configured limits."""
        if not isinstance(intent, PortfolioIntent):
            raise ValueError("intent must be a PortfolioIntent")
        if not isinstance(state, PortfolioState):
            raise ValueError("state must be a PortfolioState")
        if intent.as_of_ms != state.as_of_ms:
            raise ValueError("intent.as_of_ms must match state.as_of_ms")
        if features is not None:
            if not isinstance(features, CrossSectionalFeatureSnapshot):
                raise ValueError("features must be a CrossSectionalFeatureSnapshot")
            if features.as_of_ms != intent.as_of_ms:
                raise ValueError("features.as_of_ms must match intent.as_of_ms")

        candidates = list(intent.intents)
        results: list[PositionRiskResult] = []
        reasons: list[PortfolioRejectReason] = []

        candidates = self._apply_max_positions(candidates, results, reasons)
        candidates = self._apply_max_position_weight(candidates, results, reasons)
        candidates = self._apply_max_gross_exposure(candidates, results, reasons)
        candidates = self._apply_max_net_exposure(candidates, results, reasons)

        # Margin / leverage check (R0.3) — after gross/net, before vol-sizing
        candidates = self._apply_max_leverage(candidates, state, results, reasons)

        # R8: Correlation filter — after leverage, before vol-sizing
        if self._limits.enable_correlation_filter:
            candidates = self._apply_correlation_filter(candidates, features, results, reasons)

        if sizing is not None:
            candidates = self._apply_volatility_sizing(candidates, features, sizing)
            candidates = self._apply_max_position_weight(candidates, results, reasons)
            candidates = self._apply_max_net_exposure(candidates, results, reasons)
            if not candidates:
                self._reject_unrejected(intent.intents, results, reasons)

        gross = sum(abs(c.target_weight) for c in candidates)
        net = sum(
            c.target_weight if c.side == Side.LONG else -c.target_weight
            for c in candidates
        )

        for candidate in candidates:
            results.append(
                PositionRiskResult(
                    symbol=candidate.symbol,
                    side=candidate.side,
                    requested_weight=candidate.target_weight,
                    granted_weight=candidate.target_weight,
                    accepted=True,
                    timeframe=candidate.timeframe,
                )
            )

        adjusted_intent = PortfolioIntent(
            as_of_ms=intent.as_of_ms,
            intents=tuple(candidates),
            universe=intent.universe,
            strategy_name=intent.strategy_name,
            closes=intent.closes,
        )

        order = {
            (candidate.symbol, candidate.timeframe): index
            for index, candidate in enumerate(intent.intents)
        }
        position_results = tuple(
            sorted(
                results,
                key=lambda r: order.get((r.symbol, r.timeframe), len(order)),
            )
        )

        return PortfolioRiskReport(
            accepted=not reasons,
            adjusted_intent=adjusted_intent,
            position_results=position_results,
            gross_exposure=abs(gross),
            net_exposure=abs(net),
            rejected_reasons=tuple(dict.fromkeys(reasons)),
        )

    def _apply_max_positions(
        self,
        candidates: list[PositionIntent],
        results: list[PositionRiskResult],
        reasons: list[PortfolioRejectReason],
    ) -> list[PositionIntent]:
        if len(candidates) <= self._limits.max_positions:
            return candidates
        kept = candidates[: self._limits.max_positions]
        for dropped in candidates[self._limits.max_positions :]:
            self._reject(dropped, PortfolioRejectReason.REJECT_MAX_POSITIONS, results, reasons)
        return kept

    def _apply_max_position_weight(
        self,
        candidates: list[PositionIntent],
        results: list[PositionRiskResult],
        reasons: list[PortfolioRejectReason],
    ) -> list[PositionIntent]:
        kept: list[PositionIntent] = []
        for candidate in candidates:
            if candidate.target_weight > self._limits.max_position_weight:
                self._reject(
                    candidate,
                    PortfolioRejectReason.REJECT_MAX_POSITION_WEIGHT,
                    results,
                    reasons,
                )
            else:
                kept.append(candidate)
        return kept

    def _apply_max_gross_exposure(
        self,
        candidates: list[PositionIntent],
        results: list[PositionRiskResult],
        reasons: list[PortfolioRejectReason],
    ) -> list[PositionIntent]:
        gross = sum(abs(c.target_weight) for c in candidates)
        while candidates and gross > self._limits.max_gross_exposure:
            dropped = min(candidates, key=lambda c: (abs(c.target_weight), candidates.index(c)))
            candidates.remove(dropped)
            gross -= abs(dropped.target_weight)
            self._reject(
                dropped,
                PortfolioRejectReason.REJECT_MAX_GROSS_EXPOSURE,
                results,
                reasons,
            )
        return candidates

    def _apply_max_net_exposure(
        self,
        candidates: list[PositionIntent],
        results: list[PositionRiskResult],
        reasons: list[PortfolioRejectReason],
    ) -> list[PositionIntent]:
        while candidates:
            net = sum(
                c.target_weight if c.side == Side.LONG else -c.target_weight
                for c in candidates
            )
            if abs(net) <= self._limits.max_net_exposure:
                break
            overloaded_side = Side.LONG if net > 0 else Side.SHORT
            on_side = [c for c in candidates if c.side == overloaded_side]
            if not on_side:
                break
            dropped = min(on_side, key=lambda c: (abs(c.target_weight), candidates.index(c)))
            candidates.remove(dropped)
            self._reject(
                dropped,
                PortfolioRejectReason.REJECT_MAX_NET_EXPOSURE,
                results,
                reasons,
            )
        return candidates

    def _apply_max_leverage(
        self,
        candidates: list[PositionIntent],
        state: PortfolioState,
        results: list[PositionRiskResult],
        reasons: list[PortfolioRejectReason],
    ) -> list[PositionIntent]:
        """Apply margin/leverage limit to the portfolio.

        required_margin_fraction = gross_weight / max_leverage
        If required_margin_fraction * (1 + maintenance_margin_buffer_pct) > 1.0,
        proportionally down-scale gross exposure and re-check downstream limits.

        Long and short positions are treated symmetrically for margin purposes
        (native perpetual short is margin-equivalent to long).
        """
        if self._limits.max_leverage >= 1e9:  # effectively unlimited
            return candidates

        gross = sum(abs(c.target_weight) for c in candidates)
        if gross <= 0:
            return candidates

        available_equity = state.equity
        if available_equity <= 0:
            # No equity to support any positions
            for c in candidates:
                self._reject(
                    c,
                    PortfolioRejectReason.REJECT_MAX_LEVERAGE,
                    results,
                    reasons,
                )
            return []

        # required_margin is a FRACTION of equity (not dollar amount)
        # e.g., gross=3.0, leverage=2.0 => required_margin_fraction = 1.5 (150% of equity)
        required_margin_fraction = gross / self._limits.max_leverage
        buffer = self._limits.maintenance_margin_buffer_pct
        required_with_buffer = required_margin_fraction * (1.0 + buffer)

        # Check if margin fraction exceeds 100% of equity
        if required_with_buffer <= 1.0:
            return candidates

        # Scale down gross exposure to fit within margin + buffer
        # target gross = max_leverage / (1 + buffer)
        target_gross = self._limits.max_leverage / (1.0 + buffer)
        scale = target_gross / gross

        # Scale all weights proportionally
        scaled: list[PositionIntent] = []
        for c in candidates:
            new_weight = c.target_weight * scale
            if new_weight > 0:
                scaled.append(_with_weight(c, new_weight))

        # After scaling, re-check downstream limits (gross, net, position weight)
        # by recursively calling the check methods
        scaled = self._apply_max_gross_exposure(scaled, results, reasons)
        scaled = self._apply_max_net_exposure(scaled, results, reasons)
        scaled = self._apply_max_position_weight(scaled, results, reasons)

        # Track rejected positions due to margin
        accepted_symbols = {(c.symbol, c.timeframe) for c in scaled}
        for c in candidates:
            if (c.symbol, c.timeframe) not in accepted_symbols:
                # Already rejected by downstream checks, but record the margin reason
                # if it wasn't already rejected for another reason
                existing = next(
                    (r for r in results if r.symbol == c.symbol and r.timeframe == c.timeframe and not r.accepted),
                    None,
                )
                if existing is None:
                    self._reject(
                        c,
                        PortfolioRejectReason.REJECT_MAX_LEVERAGE,
                        results,
                        reasons,
                    )

        return scaled

    def _apply_correlation_filter(
        self,
        candidates: list[PositionIntent],
        features: CrossSectionalFeatureSnapshot | None,
        results: list[PositionRiskResult],
        reasons: list[PortfolioRejectReason],
    ) -> list[PositionIntent]:
        """Apply leg-aware correlation filter to limit correlated positions.

        Distinguishes between:
        - "Within-leg" correlation: positions on the SAME side (long-long or short-short)
        - "Cross-leg" correlation: positions on OPPOSITE sides (long-short)

        Long positions are typically on oversold assets, shorts on overbought.
        These are naturally diversifying (often negatively correlated), so we allow
        more cross-leg positions. Within-leg positions are more likely to be
        positively correlated and should be limited.

        The filter tracks counts per side and applies max_correlated_positions
        as a per-side limit.
        """
        if not self._limits.enable_correlation_filter or not candidates:
            return candidates

        if features is None:
            # Cannot compute correlation without features
            return candidates

        accepted: list[PositionIntent] = []
        rejected_symbols: set[str] = set()

        # Track accepted count per side for leg-aware limiting
        accepted_long_count = 0
        accepted_short_count = 0

        for candidate in candidates:
            symbol = candidate.symbol
            if symbol in rejected_symbols:
                self._reject(
                    candidate,
                    PortfolioRejectReason.REJECT_CORRELATION,
                    results,
                    reasons,
                )
                continue

            # Check within-leg count
            if candidate.side == Side.LONG:
                within_leg_count = accepted_long_count
            else:
                within_leg_count = accepted_short_count

            # If we already have max_correlated_positions on the SAME side, reject
            # This limits within-leg correlation (long-long or short-short)
            if within_leg_count >= self._limits.max_correlated_positions:
                rejected_symbols.add(symbol)
                self._reject(
                    candidate,
                    PortfolioRejectReason.REJECT_CORRELATION,
                    results,
                    reasons,
                )
                continue

            # Accept the position
            accepted.append(candidate)
            if candidate.side == Side.LONG:
                accepted_long_count += 1
            else:
                accepted_short_count += 1

        return accepted

    def _apply_volatility_sizing(
        self,
        candidates: list[PositionIntent],
        features: CrossSectionalFeatureSnapshot | None,
        sizing: VolatilitySizingParams,
    ) -> list[PositionIntent]:
        if not candidates or features is None:
            return candidates
        if not any(hasattr(f, sizing.source) for f in features.features_by_symbol.values()):
            return candidates

        volatilities: dict[str, float | None] = {}
        known: list[float] = []
        for candidate in candidates:
            feature = features.features_by_symbol.get(candidate.symbol)
            value = getattr(feature, sizing.source, _FALLBACK_VOLATILITY_PCT) if feature is not None else None
            if (
                feature is not None
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
                and isfinite(float(value))
                and float(value) > 0
            ):
                volatilities[candidate.symbol] = float(value)
                known.append(float(value))
            else:
                volatilities[candidate.symbol] = None

        fallback = sizing.fallback_volatility_pct or (
            median(known) if known else None
        )
        if fallback is None:
            return candidates
        reference = sizing.reference_volatility_pct or fallback
        if reference <= 0:
            return candidates

        scaled: list[PositionIntent] = []
        for candidate in candidates:
            volatility = volatilities[candidate.symbol] or fallback
            factor = reference / volatility
            scaled.append(_with_weight(candidate, candidate.target_weight * factor))

        gross = sum(abs(c.target_weight) for c in candidates)
        scaled_gross = sum(abs(c.target_weight) for c in scaled)
        if scaled_gross <= 0:
            return candidates
        scale = gross / scaled_gross
        scaled = [_with_weight(c, c.target_weight * scale) for c in scaled]
        return [
            _with_weight(c, min(c.target_weight, self._limits.max_position_weight))
            for c in scaled
        ]

    def _reject(
        self,
        candidate: PositionIntent,
        reason: PortfolioRejectReason,
        results: list[PositionRiskResult],
        reasons: list[PortfolioRejectReason],
    ) -> None:
        results.append(
            PositionRiskResult(
                symbol=candidate.symbol,
                side=candidate.side,
                requested_weight=candidate.target_weight,
                granted_weight=0.0,
                accepted=False,
                timeframe=candidate.timeframe,
                reason=reason,
            )
        )
        reasons.append(reason)

    def _reject_unrejected(
        self,
        intents: tuple[PositionIntent, ...],
        results: list[PositionRiskResult],
        reasons: list[PortfolioRejectReason],
    ) -> None:
        rejected_keys = {(r.symbol, r.timeframe) for r in results if not r.accepted}
        for intent in intents:
            if (intent.symbol, intent.timeframe) not in rejected_keys:
                self._reject(intent, PortfolioRejectReason.REJECT_MAX_NET_EXPOSURE, results, reasons)


def _with_weight(candidate: PositionIntent, weight: float) -> PositionIntent:
    return PositionIntent(
        symbol=candidate.symbol,
        side=candidate.side,
        target_weight=weight,
        timeframe=candidate.timeframe,
        reference_price=candidate.reference_price,
    )