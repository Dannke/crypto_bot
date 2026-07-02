"""Strategy registry: manages registration and retrieval of strategies.

Provides a central registry where strategies can be registered by name
and retrieved for use. This enables strategy switching via configuration
without code changes.
"""
from __future__ import annotations

from typing import Any

from .base import Strategy


class StrategyRegistry:
    """Registry for trading strategies.

    Strategies are registered with a unique name and can be retrieved
    by that name. This enables dynamic strategy selection via configuration.
    """

    def __init__(self) -> None:
        self._strategies: dict[str, type[Strategy]] = {}

    def register(self, name: str, strategy_class: type[Strategy]) -> None:
        """Register a strategy class with a name.

        Args:
            name: Unique name for the strategy.
            strategy_class: The strategy class to register.

        Raises:
            ValueError: If a strategy with this name is already registered.
        """
        if name in self._strategies:
            raise ValueError(f"Strategy '{name}' is already registered")
        self._strategies[name] = strategy_class

    def get(self, name: str) -> type[Strategy] | None:
        """Get a strategy class by name.

        Args:
            name: Name of the strategy to retrieve.

        Returns:
            The strategy class, or None if not found.
        """
        return self._strategies.get(name)

    def create(self, name: str, *args: Any, **kwargs: Any) -> Strategy | None:
        """Create an instance of a strategy by name.

        Args:
            name: Name of the strategy to instantiate.
            *args: Positional arguments to pass to the strategy constructor.
            **kwargs: Keyword arguments to pass to the strategy constructor.

        Returns:
            A new strategy instance, or None if the strategy is not found.
        """
        strategy_class = self.get(name)
        if strategy_class is None:
            return None
        return strategy_class(*args, **kwargs)

    def list_strategies(self) -> list[str]:
        """List all registered strategy names.

        Returns:
            List of strategy names.
        """
        return list(self._strategies.keys())

    def is_registered(self, name: str) -> bool:
        """Check if a strategy is registered.

        Args:
            name: Name of the strategy to check.

        Returns:
            True if the strategy is registered, False otherwise.
        """
        return name in self._strategies

    def unregister(self, name: str) -> None:
        """Unregister a strategy by name.

        Args:
            name: Name of the strategy to unregister.

        Raises:
            KeyError: If the strategy is not registered.
        """
        if name not in self._strategies:
            raise KeyError(f"Strategy '{name}' is not registered")
        del self._strategies[name]


# Global registry instance
_global_registry = StrategyRegistry()


def get_global_registry() -> StrategyRegistry:
    """Get the global strategy registry instance."""
    return _global_registry
