"""Data layer: market-data access (ccxt), instruments, and the multi-timeframe feed.

Execution (order placement) is intentionally inert in this stage.
"""
from .exchange import ExecutionClient, MarketDataClient
from .feed import Feed, SymbolFeed, build_symbols, discover_symbols, resolve_symbols, to_dataframe
from .funding import (
    BybitFundingClient,
    FundingEvent,
    FundingRepository,
    HistoricalFundingSource,
    InstrumentFundingIntervalCache,
)
from .instruments import (
    BybitInstrumentsClient,
    InstrumentCache,
    InstrumentSpec,
    build_instrument_cache,
)

__all__ = [
    "ExecutionClient",
    "Feed",
    "MarketDataClient",
    "SymbolFeed",
    "build_symbols",
    "discover_symbols",
    "resolve_symbols",
    "to_dataframe",
    "BybitFundingClient",
    "FundingEvent",
    "FundingRepository",
    "HistoricalFundingSource",
    "InstrumentFundingIntervalCache",
    "BybitInstrumentsClient",
    "InstrumentCache",
    "InstrumentSpec",
    "build_instrument_cache",
]
