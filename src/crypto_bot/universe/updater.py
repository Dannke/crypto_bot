"""Universe updater: dynamically updates the symbol universe.

Periodically refreshes the universe based on changing market conditions,
adding/removing symbols as liquidity and other factors change.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .selector import UniverseSelector


@dataclass(frozen=True, slots=True)
class UniverseUpdateResult:
    """Result of a universe update operation."""

    added: list[str]
    removed: list[str]
    unchanged: list[str]
    total_count: int
    timestamp: datetime


class UniverseUpdater:
    """Manages dynamic updates to the symbol universe.

    Periodically re-evaluates which symbols should be in the universe based
    on current market conditions (liquidity, volatility, etc.).
    """

    def __init__(
        self,
        selector: UniverseSelector,
        update_interval_minutes: int = 60,
    ) -> None:
        self._selector = selector
        self._update_interval = timedelta(minutes=update_interval_minutes)
        self._last_update: datetime | None = None
        self._current_universe: list[str] = []

    def should_update(self) -> bool:
        """Check if an update is due.

        Returns:
            True if the update interval has passed since last update.
        """
        if self._last_update is None:
            return True

        return datetime.now(tz=UTC) - self._last_update >= self._update_interval

    def update(
        self,
        market_data: dict[str, dict[str, Any]],
    ) -> UniverseUpdateResult:
        """Update the universe based on current market data.

        Args:
            market_data: Dictionary of symbol -> market data.

        Returns:
            UniverseUpdateResult with changes made.
        """
        old_universe = set(self._current_universe)
        new_universe = set(self._selector.select_from_market_data(market_data))

        added = sorted(new_universe - old_universe)
        removed = sorted(old_universe - new_universe)
        unchanged = sorted(old_universe & new_universe)

        self._current_universe = list(new_universe)
        self._last_update = datetime.now(tz=UTC)

        return UniverseUpdateResult(
            added=added,
            removed=removed,
            unchanged=unchanged,
            total_count=len(self._current_universe),
            timestamp=self._last_update,
        )

    def get_current_universe(self) -> list[str]:
        """Get the current universe.

        Returns:
            List of symbols currently in the universe.
        """
        return list(self._current_universe)

    def force_update(
        self,
        market_data: dict[str, dict[str, Any]],
    ) -> UniverseUpdateResult:
        """Force an update regardless of the update interval.

        Args:
            market_data: Dictionary of symbol -> market data.

        Returns:
            UniverseUpdateResult with changes made.
        """
        self._last_update = None  # Reset to allow update
        return self.update(market_data)

    def set_update_interval(self, minutes: int) -> None:
        """Update the interval between automatic updates.

        Args:
            minutes: New update interval in minutes.
        """
        self._update_interval = timedelta(minutes=minutes)
