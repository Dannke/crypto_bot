"""Blacklist filter: rejects symbols that are explicitly blacklisted.

Provides a mechanism to exclude specific symbols from trading, either
permanently (e.g., delisted coins) or temporarily (e.g., under investigation).
"""
from __future__ import annotations

from .base import Filter, FilterOutcome, FilterResult
from ..core.types import FeatureSet


class BlacklistFilter(Filter):
    """Filter based on a static blacklist of symbols.

    Rejects any symbol that appears in the configured blacklist.
    This is useful for excluding problematic assets or symbols under
    regulatory scrutiny.
    """

    def __init__(
        self,
        blacklist: set[str] | None = None,
    ) -> None:
        super().__init__("blacklist")
        self._blacklist = blacklist or set()

    def evaluate(self, features: FeatureSet) -> FilterResult:
        symbol = features.symbol.upper()

        if symbol in self._blacklist:
            return FilterResult(
                filter_name=self.name,
                outcome=FilterOutcome.REJECT,
                reason="blacklisted",
                detail=f"symbol {symbol} is in blacklist",
            )

        return FilterResult(
            filter_name=self.name,
            outcome=FilterOutcome.PASS,
        )

    def add_to_blacklist(self, symbol: str) -> None:
        """Add a symbol to the blacklist at runtime."""
        self._blacklist.add(symbol.upper())

    def remove_from_blacklist(self, symbol: str) -> None:
        """Remove a symbol from the blacklist at runtime."""
        self._blacklist.discard(symbol.upper())
