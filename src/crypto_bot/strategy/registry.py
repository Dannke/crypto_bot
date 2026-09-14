"""Registry for candidate and portfolio strategy implementations."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..core.enums import StrategyType
from .base import CandidateStrategy, PortfolioStrategy
from .portfolio_strategies import MEAN_REVERSION_V0_STRATEGY_NAME

RegisteredStrategy = type[CandidateStrategy] | type[PortfolioStrategy]
StrategyInstance = CandidateStrategy | PortfolioStrategy

# Re-export for convenience
__all__ = [
    "StrategyRegistry",
    "StrategyRegistration",
    "RegisteredStrategy",
    "StrategyInstance",
    "MEAN_REVERSION_V0_STRATEGY_NAME",
]


@dataclass(frozen=True, slots=True)
class StrategyRegistration:
    """A strategy class plus the layer in which it may operate."""

    strategy_class: RegisteredStrategy
    strategy_type: StrategyType


class StrategyRegistry:
    """Register, inspect, and construct typed strategy implementations."""

    def __init__(self) -> None:
        self._strategies: dict[str, StrategyRegistration] = {}

    def register(
        self,
        name: str,
        strategy_class: RegisteredStrategy,
        *,
        strategy_type: StrategyType = StrategyType.CANDIDATE,
    ) -> None:
        """Register a class under a unique name and explicit strategy type."""
        if name in self._strategies:
            raise ValueError(f"Strategy '{name}' is already registered")
        if not isinstance(strategy_type, StrategyType):
            raise ValueError("strategy_type must be a StrategyType")
        expected_base = (
            CandidateStrategy
            if strategy_type == StrategyType.CANDIDATE
            else PortfolioStrategy
        )
        if not issubclass(strategy_class, expected_base):
            raise ValueError(
                f"Strategy '{name}' must implement {expected_base.__name__} "
                f"for strategy_type='{strategy_type.value}'"
            )
        self._strategies[name] = StrategyRegistration(strategy_class, strategy_type)

    def get(self, name: str) -> RegisteredStrategy | None:
        """Return a registered class, if present."""
        registration = self._strategies.get(name)
        return registration.strategy_class if registration else None

    def get_type(self, name: str) -> StrategyType | None:
        """Return the registered ``candidate`` or ``portfolio`` type."""
        registration = self._strategies.get(name)
        return registration.strategy_type if registration else None

    def create(
        self,
        name: str,
        *args: Any,
        strategy_type: StrategyType | None = None,
        **kwargs: Any,
    ) -> StrategyInstance | None:
        """Construct a registered strategy, optionally enforcing its type."""
        registration = self._strategies.get(name)
        if registration is None:
            return None
        if strategy_type is not None and registration.strategy_type != strategy_type:
            raise ValueError(
                f"Strategy '{name}' is registered as '{registration.strategy_type.value}', "
                f"not '{strategy_type.value}'"
            )
        return registration.strategy_class(*args, **kwargs)

    def is_registered(self, name: str, *, strategy_type: StrategyType | None = None) -> bool:
        """Return whether a name is registered, optionally for one strategy type."""
        registration = self._strategies.get(name)
        return registration is not None and (
            strategy_type is None or registration.strategy_type == strategy_type
        )
