"""Universe selector: determines which symbols to analyze.

Supports multiple selection strategies:
- Static list from configuration
- Auto-discovery by liquidity (top-N by volume)
- Dynamic updates based on market conditions
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class UniverseConfig:
    """Configuration for universe selection."""

    static_symbols: list[str]
    auto_discover_enabled: bool = False
    auto_discover_top_n: int = 50
    exclude_symbols: list[str] | None = None
    exclude_stablecoins: bool = True
    exclude_leveraged: bool = True
    quote_currency: str = "USDT"


class UniverseSelector:
    """Selects symbols for analysis based on configuration.

    Combines static watchlists with dynamic auto-discovery to create
    a comprehensive universe of tradable symbols.
    """

    def __init__(self, config: UniverseConfig) -> None:
        self._config = config
        self._exclude_set = set(config.exclude_symbols or [])

    def select_static(self) -> list[str]:
        """Select symbols from the static watchlist.

        Returns:
            List of symbols (e.g., ["BTC/USDT", "ETH/USDT"]).
        """
        selected = []
        for symbol in self._config.static_symbols:
            normalized = symbol.upper().strip()
            if self._is_allowed(normalized):
                selected.append(normalized)
        return selected

    def select_from_market_data(
        self,
        market_data: dict[str, dict[str, Any]],
    ) -> list[str]:
        """Select symbols from market data with auto-discovery.

        Args:
            market_data: Dictionary of symbol -> market data (volume, etc.).

        Returns:
            List of selected symbols.
        """
        if not self._config.auto_discover_enabled:
            return self.select_static()

        # Rank by quote volume
        ranked = []
        for symbol, data in market_data.items():
            if not self._is_allowed(symbol):
                continue

            # Extract quote volume (may be in different fields)
            volume = data.get("quoteVolume") or data.get("quote_volume") or 0.0
            if isinstance(volume, str):
                volume = float(volume)

            ranked.append((volume, symbol.upper()))

        # Sort by volume descending
        ranked.sort(key=lambda x: x[0], reverse=True)

        # Take top-N
        top_symbols = [symbol for _, symbol in ranked[: self._config.auto_discover_top_n]]

        # Merge with static symbols (static first, then discovered)
        static = self.select_static()
        seen = set(static)
        final = list(static)

        for symbol in top_symbols:
            if symbol not in seen:
                seen.add(symbol)
                final.append(symbol)

        return final

    def _is_allowed(self, symbol: str) -> bool:
        """Check if a symbol is allowed in the universe.

        Args:
            symbol: Symbol to check (e.g., "BTC/USDT").

        Returns:
            True if symbol is allowed, False otherwise.
        """
        symbol = symbol.upper().strip()

        # Check explicit exclusions
        if symbol in self._exclude_set:
            return False

        # Parse base/quote
        parts = symbol.split("/")
        if len(parts) != 2:
            return False

        base, quote = parts

        # Check quote currency
        if quote != self._config.quote_currency:
            return False

        # Check stablecoin exclusion
        if self._config.exclude_stablecoins and self._is_stablecoin(base):
            return False

        # Check leveraged token exclusion
        return not (self._config.exclude_leveraged and self._is_leveraged(base))

    def _is_stablecoin(self, base: str) -> bool:
        """Check if base is a stablecoin."""
        stablecoins = {
            "USDT", "USDC", "BUSD", "DAI", "TUSD", "FDUSD", "USDP",
            "PAX", "SUSD", "GUSD", "USDD", "USTC", "EURT", "USDS",
        }
        return base in stablecoins

    def _is_leveraged(self, base: str) -> bool:
        """Check if base is a leveraged token."""
        suffixes = ("UP", "DOWN", "BULL", "BEAR", "3L", "3S", "5L", "5S")
        return any(base.endswith(suffix) for suffix in suffixes)

    def add_exclusion(self, symbol: str) -> None:
        """Add a symbol to the exclusion list."""
        self._exclude_set.add(symbol.upper().strip())

    def remove_exclusion(self, symbol: str) -> None:
        """Remove a symbol from the exclusion list."""
        self._exclude_set.discard(symbol.upper().strip())
