"""Data layer: market-data access (ccxt) and the multi-timeframe feed.

Execution (order placement) is intentionally inert in this stage.
"""
from .exchange import ExecutionClient, MarketDataClient
from .feed import Feed, SymbolFeed, build_symbols, discover_symbols, resolve_symbols, to_dataframe

__all__ = [
    "ExecutionClient",
    "Feed",
    "MarketDataClient",
    "SymbolFeed",
    "build_symbols",
    "discover_symbols",
    "resolve_symbols",
    "to_dataframe",
]
