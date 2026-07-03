"""Tests for universe selection."""
from __future__ import annotations

from crypto_bot.universe.selector import UniverseConfig, UniverseSelector


def test_static_selection_respects_exclusions():
    selector = UniverseSelector(
        UniverseConfig(
            static_symbols=["BTC/USDT", "ETH/USDT", "USDC/USDT"],
            exclude_symbols=["USDC/USDT"],
            quote_currency="USDT",
        )
    )
    selected = selector.select_static()
    assert selected == ["BTC/USDT", "ETH/USDT"]


def test_auto_discover_top_n_by_volume():
    selector = UniverseSelector(
        UniverseConfig(
            static_symbols=["BTC/USDT"],
            auto_discover_enabled=True,
            auto_discover_top_n=2,
            quote_currency="USDT",
        )
    )
    market_data = {
        "BTC/USDT": {"quoteVolume": 1_000_000_000},
        "ETH/USDT": {"quoteVolume": 500_000_000},
        "SOL/USDT": {"quoteVolume": 100_000_000},
    }
    selected = selector.select_from_market_data(market_data)
    assert selected[0] == "BTC/USDT"
    assert "ETH/USDT" in selected
    assert len(selected) <= 3
