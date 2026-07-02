"""Strategy manager: manages active strategies and execution.

Provides a high-level interface for managing multiple strategies,
switching between them, and executing strategy logic.
"""
from __future__ import annotations

from typing import Any

from .base import Strategy, StrategyContext
from .registry import StrategyRegistry


class StrategyManager:
    """Manager for trading strategies.

    Handles strategy selection, initialization, and execution.
    Supports running multiple strategies simultaneously and switching
    between them based on configuration.
    """

    def __init__(
        self,
        registry: StrategyRegistry | None = None,
    ) -> None:
        self._registry = registry or StrategyRegistry()
        self._active_strategies: dict[str, Strategy] = {}
        self._default_strategy: str | None = None

    def set_default_strategy(self, name: str) -> None:
        """Set the default strategy name.

        Args:
            name: Name of the strategy to use as default.

        Raises:
            ValueError: If the strategy is not registered.
        """
        if not self._registry.is_registered(name):
            raise ValueError(f"Strategy '{name}' is not registered")
        self._default_strategy = name

    def get_default_strategy(self) -> Strategy | None:
        """Get the default strategy instance.

        Returns:
            The default strategy instance, or None if not set.
        """
        if self._default_strategy is None:
            return None
        return self._active_strategies.get(self._default_strategy)

    def activate_strategy(
        self,
        name: str,
        context: StrategyContext,
        *args: Any,
        **kwargs: Any,
    ) -> Strategy:
        """Activate a strategy with the given context.

        Args:
            name: Name of the strategy to activate.
            context: Strategy context with parameters.
            *args: Additional arguments for strategy constructor.
            **kwargs: Additional keyword arguments for strategy constructor.

        Returns:
            The activated strategy instance.

        Raises:
            ValueError: If the strategy is not registered.
        """
        strategy = self._registry.create(name, context, *args, **kwargs)
        if strategy is None:
            raise ValueError(f"Strategy '{name}' is not registered")

        self._active_strategies[name] = strategy
        return strategy

    def deactivate_strategy(self, name: str) -> None:
        """Deactivate a strategy.

        Args:
            name: Name of the strategy to deactivate.

        Raises:
            KeyError: If the strategy is not active.
        """
        if name not in self._active_strategies:
            raise KeyError(f"Strategy '{name}' is not active")
        del self._active_strategies[name]

    def get_strategy(self, name: str) -> Strategy | None:
        """Get an active strategy by name.

        Args:
            name: Name of the strategy to retrieve.

        Returns:
            The strategy instance, or None if not active.
        """
        return self._active_strategies.get(name)

    def list_active_strategies(self) -> list[str]:
        """List names of all active strategies.

        Returns:
            List of active strategy names.
        """
        return list(self._active_strategies.keys())

    def evaluate_with_default(
        self,
        symbol: str,
        features_by_tf: dict[str, Any],
    ) -> Any:
        """Evaluate using the default strategy.

        Args:
            symbol: Symbol to evaluate.
            features_by_tf: Features by timeframe.

        Returns:
            Signal result from the strategy.

        Raises:
            RuntimeError: If no default strategy is set.
        """
        strategy = self.get_default_strategy()
        if strategy is None:
            raise RuntimeError("No default strategy set")
        return strategy.evaluate(symbol, features_by_tf)

    def score_with_default(
        self,
        features: Any,
        signal_result: Any,
    ) -> Any:
        """Score using the default strategy.

        Args:
            features: Feature set.
            signal_result: Signal result from evaluation.

        Returns:
            Scored candidate from the strategy.

        Raises:
            RuntimeError: If no default strategy is set.
        """
        strategy = self.get_default_strategy()
        if strategy is None:
            raise RuntimeError("No default strategy set")
        return strategy.score(features, signal_result)

    @property
    def registry(self) -> StrategyRegistry:
        """Get the strategy registry."""
        return self._registry
