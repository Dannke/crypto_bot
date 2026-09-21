"""Portfolio Fusion Engine (R4).

Combines multiple portfolio strategy intents into a single PortfolioIntent,
gated by market regime. This is the portfolio-layer analogue of the
candidate-layer FusionEngine.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Protocol

from ..portfolio.models import PortfolioIntent, RegimeSnapshot


class PortfolioFusionEngine(Protocol):
    """Protocol for fusing multiple strategy intents into one portfolio intent."""

    def fuse(
        self,
        intents: Sequence[PortfolioIntent],
        regime: RegimeSnapshot,
    ) -> PortfolioIntent:
        """Fuse multiple intents into a single portfolio intent.

        Args:
            intents: Sequence of PortfolioIntent from different strategies.
            regime: Current market regime snapshot.

        Returns:
            Fused PortfolioIntent.
        """
        ...


class RegimeGatedFusion:
    """v0 Fusion: single upstream intent + regime exposure multiplier.

    Supports per-strategy exposure multipliers via strategy_overrides.
    In v0 there is only one strategy, so this applies the regime-based
    gross exposure multiplier to the intent weights.
    Later versions will fuse multiple strategies.
    """

    def __init__(
        self,
        exposure_multiplier: dict[str, float] | None = None,
        strategy_overrides: dict[str, dict[str, float]] | None = None,
    ) -> None:
        """Initialize with regime exposure multipliers.

        Args:
            exposure_multiplier: Mapping regime -> gross exposure multiplier [0, 1].
                Defaults to a conservative mapping if not provided.
            strategy_overrides: Per-strategy overrides for exposure multipliers.
                Format: {strategy_name: {regime: multiplier}}
        """
        if exposure_multiplier is None:
            exposure_multiplier = {
                "trend_low_vol": 1.0,
                "trend_high_vol": 0.5,
                "range_low_vol": 0.25,
                "range_high_vol": 0.0,
            }
        # Validate multipliers
        for regime, mult in exposure_multiplier.items():
            if not 0.0 <= mult <= 1.0:
                raise ValueError(f"exposure_multiplier for {regime} must be in [0, 1], got {mult}")
        self._multipliers = exposure_multiplier

        # Validate strategy overrides
        self._strategy_overrides = strategy_overrides or {}
        for strat_name, overrides in self._strategy_overrides.items():
            for regime_name, mult in overrides.items():
                if not 0.0 <= mult <= 1.0:
                    raise ValueError(
                        f"strategy_overrides[{strat_name}][{regime_name}] must be in [0, 1], got {mult}"
                    )

    def _get_multiplier(self, strategy_name: str | None, regime: str) -> float:
        """Get the exposure multiplier for a strategy and regime.
        
        Strategies with explicit overrides use their per-regime multipliers.
        Strategies without overrides get 1.0 (no regime gating).
        """
        if strategy_name and strategy_name in self._strategy_overrides:
            override = self._strategy_overrides[strategy_name].get(regime)
            if override is not None:
                return override
        # No override for this strategy -> no regime gating (multiplier 1.0)
        return 1.0

    def fuse(
        self,
        intents: Sequence[PortfolioIntent],
        regime: RegimeSnapshot,
    ) -> PortfolioIntent:
        """Apply regime exposure multiplier to the (single) input intent.

        If multiple intents provided, merges them by summing weights per symbol/side.
        Each intent's strategy_name is used to look up per-strategy overrides.
        """
        if not intents:
            raise ValueError("intents must not be empty")

        # v0: only one intent expected, but merge if multiple provided
        from ..portfolio.models import PositionIntent
        merged_intents: dict[tuple[str, str | None], PositionIntent] = {}
        for intent in intents:
            for pi in intent.intents:
                key = (pi.symbol, pi.timeframe)
                if key in merged_intents:
                    existing = merged_intents[key]
                    # Sum weights if same side, else net them
                    if existing.side == pi.side:
                        new_weight = existing.target_weight + pi.target_weight
                    else:
                        new_weight = existing.target_weight - pi.target_weight
                    if new_weight > 0:
                        merged_intents[key] = replace(
                            existing, target_weight=new_weight
                        )
                    elif new_weight < 0:
                        merged_intents[key] = replace(
                            pi, target_weight=-new_weight
                        )
                    else:
                        del merged_intents[key]
                else:
                    merged_intents[key] = pi

        # Get regime multiplier for the first intent's strategy
        # (In multi-strategy v1+, we'd apply per-intent)
        strategy_name = intents[0].strategy_name if intents else None
        mult = self._get_multiplier(strategy_name, regime.regime)

        # Apply multiplier to all weights, filter out zero weights
        scaled_intents = tuple(
            replace(pi, target_weight=pi.target_weight * mult)
            for pi in merged_intents.values()
            if pi.target_weight * mult > 0
        )

        # Закрытия — авторская атрибуция стратегии, а не веса: их нельзя
        # терять при пересборке интента. Потерянный close не отменяет выход
        # (позиция всё равно уйдёт из целевой книги), но лишает его причины —
        # ниже по течению он молча становится "rebalance".
        # Ключ (symbol, timeframe) совпадает с ключом exit_reasons у
        # потребителя (backtester._rebalance_positions); при нескольких
        # интентах первая причина на ключ побеждает.
        merged_closes: dict[tuple[str, str], tuple[str, str, str]] = {}
        for intent in intents:
            for close in intent.closes:
                merged_closes.setdefault((close[0], close[1]), close)

        # Use the first intent's metadata
        base = intents[0]
        return PortfolioIntent(
            as_of_ms=regime.as_of_ms,
            intents=scaled_intents,
            closes=tuple(merged_closes.values()),
            universe=base.universe,
            strategy_name=base.strategy_name,
        )


def create_regime_gated_fusion(
    config: dict[str, float] | None = None,
    strategy_overrides: dict[str, dict[str, float]] | None = None,
) -> RegimeGatedFusion:
    """Factory to create RegimeGatedFusion from config dict."""
    return RegimeGatedFusion(exposure_multiplier=config, strategy_overrides=strategy_overrides)