"""Cooldown filter: rejects symbols that were recently traded.

Prevents overtrading by enforcing a minimum time interval between trades
on the same symbol. This reduces transaction costs and avoids choppy
re-entries on the same position.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from .base import Filter, FilterOutcome, FilterResult
from ..core.types import FeatureSet


class CooldownFilter(Filter):
    """Filter based on recent trading activity.

    Rejects symbols that have been traded within the cooldown period.
    Requires access to decision history to check recent activity.
    """

    def __init__(
        self,
        cooldown_minutes: int = 60,  # minimum 60 minutes between trades
        last_trade_time: dict[str, datetime] | None = None,
    ) -> None:
        super().__init__("cooldown")
        self._cooldown_minutes = cooldown_minutes
        self._last_trade_time = last_trade_time or {}

    def evaluate(self, features: FeatureSet) -> FilterResult:
        symbol = features.symbol
        last_time = self._last_trade_time.get(symbol)

        if last_time is None:
            # No recent trade history for this symbol
            return FilterResult(
                filter_name=self.name,
                outcome=FilterOutcome.PASS,
            )

        # Check if we're still in cooldown
        now = datetime.now(tz=UTC)
        elapsed = now - last_time

        if elapsed < timedelta(minutes=self._cooldown_minutes):
            remaining_minutes = self._cooldown_minutes - elapsed.total_seconds() / 60
            return FilterResult(
                filter_name=self.name,
                outcome=FilterOutcome.REJECT,
                reason="in_cooldown",
                detail=f"{remaining_minutes:.1f} minutes remaining",
            )

        return FilterResult(
            filter_name=self.name,
            outcome=FilterOutcome.PASS,
        )

    def update_last_trade_time(self, symbol: str, timestamp: datetime) -> None:
        """Update the last trade time for a symbol after execution."""
        self._last_trade_time[symbol] = timestamp
