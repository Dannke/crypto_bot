"""Lifecycle management for registered candidate and portfolio strategies."""
from __future__ import annotations

from typing import Any

from ..core.enums import StrategyType
from .base import CandidateStrategy, PortfolioStrategy, StrategyContext
from .registry import StrategyRegistry

StrategyInstance = CandidateStrategy | PortfolioStrategy


class StrategyManager:
    """Activate named strategy instances without conflating their interfaces."""

    def __init__(self, registry: StrategyRegistry | None = None) -> None:
        self._registry = registry or StrategyRegistry()
        self._active_strategies: dict[str, StrategyInstance] = {}
        self._default_strategy: str | None = None

    def set_default_strategy(
        self,
        name: str,
        *,
        strategy_type: StrategyType | None = None,
    ) -> None:
        """Set the default strategy, optionally asserting its registered type."""
        if not self._registry.is_registered(name, strategy_type=strategy_type):
            raise ValueError(f"Strategy '{name}' is not registered")
        self._default_strategy = name

    def get_default_strategy(self) -> StrategyInstance | None:
        """Return the activated default strategy, if any."""
        if self._default_strategy is None:
            return None
        return self._active_strategies.get(self._default_strategy)

    def activate_strategy(
        self,
        name: str,
        context: StrategyContext,
        *args: Any,
        strategy_type: StrategyType | None = None,
        **kwargs: Any,
    ) -> StrategyInstance:
        """Instantiate and activate a registered strategy."""
        strategy = self._registry.create(
            name, context, *args, strategy_type=strategy_type, **kwargs
        )
        if strategy is None:
            raise ValueError(f"Strategy '{name}' is not registered")
        self._active_strategies[name] = strategy
        return strategy

    def deactivate_strategy(self, name: str) -> None:
        """Deactivate an existing named strategy."""
        if name not in self._active_strategies:
            raise KeyError(f"Strategy '{name}' is not active")
        del self._active_strategies[name]

    def get_strategy(self, name: str) -> StrategyInstance | None:
        """Return an activated strategy by name, if any."""
        return self._active_strategies.get(name)

    def list_active_strategies(self) -> list[str]:
        """List activated strategy names."""
        return list(self._active_strategies.keys())

    def evaluate_with_default(self, symbol: str, features_by_tf: dict[str, Any]) -> Any:
        """Evaluate the default candidate strategy using the legacy helper API."""
        strategy = self.get_default_strategy()
        if strategy is None:
            raise RuntimeError("No default strategy set")
        if not isinstance(strategy, CandidateStrategy):
            raise RuntimeError("Default strategy is a portfolio strategy, not a candidate strategy")
        return strategy.evaluate(symbol, features_by_tf)

    @property
    def registry(self) -> StrategyRegistry:
        """Expose the registry for inspection and extension."""
        return self._registry
