"""Multi-timeframe candle feed.

Responsibilities:
  * Turn the configured universe (base assets + quote) into exchange symbols.
  * Fetch candle history for every (symbol, timeframe) pair concurrently.
  * Normalise raw ccxt rows into typed ``Candle`` objects.
  * Be robust to partial failures: one bad symbol must not abort the whole scan.

The feed does NOT compute indicators — it only delivers clean ``Candle``
sequences. Indicator/feature code lives under ``indicators/`` and ``features/``
and stays free of any I/O dependency.
"""
from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import pandas as pd

from ..config.env import Config
from ..core import policy
from ..core.exceptions import DataFeedError
from ..core.logging_setup import get_logger
from ..core.types import Candle
from .exchange import MarketDataClient

_log = get_logger("data.feed")


@dataclass(slots=True)
class SymbolFeed:
    """All candles for one symbol across every requested timeframe."""

    symbol: str
    by_timeframe: dict[str, list[Candle]]


def build_symbols(config: Config) -> list[str]:
    """Expand the universe into ``BASE/QUOTE`` ccxt symbols."""
    u = config.settings.universe
    quote = u.quote.strip().upper()
    excluded = {x.strip().upper() for x in u.exclude if x.strip()}
    symbols: list[str] = []
    seen: set[str] = set()
    for base in u.symbols:
        b = base.strip().upper()
        if not b or b == quote or b in excluded:
            continue
        if u.exclude_stablecoins and policy.is_stablecoin(b):
            continue
        if u.exclude_leveraged_tokens and policy.is_leveraged_token(b):
            continue
        sym = f"{b}/{quote}"
        if sym not in seen:
            seen.add(sym)
            symbols.append(sym)
    return symbols


def _quote_volume(ticker: dict[str, Any]) -> float:
    value = ticker.get("quoteVolume")
    if value is None:
        base_volume = ticker.get("baseVolume")
        last = ticker.get("last")
        value = (
            float(base_volume) * float(last)
            if base_volume is not None and last is not None
            else 0.0
        )
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _base_for_symbol(symbol: str, quote: str) -> str | None:
    suffix = f"/{quote}"
    if not symbol.endswith(suffix) or ":" in symbol:
        return None
    base = symbol[: -len(suffix)].strip().upper()
    return base or None


async def discover_symbols(client: MarketDataClient, config: Config) -> list[str]:
    """Discover top-N liquid symbols by 24h quote volume."""
    u = config.settings.universe
    if not u.auto_discover.enabled:
        return []

    quote = u.quote.strip().upper()
    excluded = {x.strip().upper() for x in u.exclude if x.strip()}
    tickers = await client.fetch_tickers(None)
    ranked: list[tuple[float, str]] = []
    for symbol, ticker in tickers.items():
        base = _base_for_symbol(symbol, quote)
        if base is None or base in excluded:
            continue
        if u.exclude_stablecoins and policy.is_stablecoin(base):
            continue
        if u.exclude_leveraged_tokens and policy.is_leveraged_token(base):
            continue
        ranked.append((_quote_volume(ticker), symbol))

    ranked.sort(key=lambda item: item[0], reverse=True)
    return [symbol for _, symbol in ranked[: u.auto_discover.top_n]]


async def resolve_symbols(client: MarketDataClient, config: Config) -> list[str]:
    """Merge explicit watchlist symbols with auto-discovered high-liquidity pairs."""
    explicit = build_symbols(config)
    discovered = await discover_symbols(client, config)
    seen: set[str] = set()
    resolved: list[str] = []
    for symbol in [*explicit, *discovered]:
        if symbol not in seen:
            seen.add(symbol)
            resolved.append(symbol)
    return resolved


def _row_to_candle(row: list[Any]) -> Candle:
    # ccxt OHLCV row: [timestamp_ms, o, h, l, c, v]
    return Candle(
        timestamp=int(row[0]),
        open=float(row[1]),
        high=float(row[2]),
        low=float(row[3]),
        close=float(row[4]),
        volume=float(row[5]),
    )


def _sort_dedup(candles: list[Candle]) -> list[Candle]:
    """Sort ascending by time and drop duplicate timestamps (keep last)."""
    if not candles:
        return []
    seen: dict[int, Candle] = {}
    for c in candles:
        seen[c.timestamp] = c
    return [seen[ts] for ts in sorted(seen)]


class Feed:
    """Fetches candle history across the configured timeframes."""

    def __init__(self, client: MarketDataClient, config: Config) -> None:
        self._client = client
        self._config = config
        self._timeframes = config.settings.timeframes.primary
        self._limit = config.settings.timeframes.candles_per_tf

    async def fetch_symbol(self, symbol: str) -> SymbolFeed:
        """Fetch all timeframes for one symbol, concurrently."""
        async def _one(tf: str) -> tuple[str, list[Candle]]:
            raw = await self._client.fetch_ohlcv(symbol, tf, limit=self._limit)
            candles = _sort_dedup([_row_to_candle(r) for r in raw])
            return tf, candles

        results = await asyncio.gather(*[_one(tf) for tf in self._timeframes])
        return SymbolFeed(symbol=symbol, by_timeframe=dict(results))

    async def fetch_many(self, symbols: Iterable[str]) -> list[SymbolFeed]:
        """Fetch many symbols concurrently; isolate per-symbol failures.

        A failing symbol is logged and skipped rather than aborting the scan —
        the bot's whole point is to scan a wide universe, so one outage must
        not blank the rest.
        """
        async def _safe(sym: str) -> SymbolFeed | None:
            try:
                return await self.fetch_symbol(sym)
            except DataFeedError as exc:
                _log.warning("feed: skip symbol %s: %s", sym, exc)
                return None

        gathered = await asyncio.gather(*[_safe(s) for s in symbols])
        return [f for f in gathered if f is not None]


def to_dataframe(candles: list[Candle]) -> pd.DataFrame:
    """Materialise a Candle list as a sorted, typed DataFrame.

    Indicator functions consume this format. Sorting + dedup upstream means the
    DataFrame is already monotonic, but we sort again defensively.
    """
    if not candles:
        return pd.DataFrame(
            columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
    df = pd.DataFrame(
        [
            {
                "timestamp": c.timestamp,
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
                "volume": c.volume,
            }
            for c in candles
        ]
    )
    df = df.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)
    return df


# Re-export for callers that import from the feed package.
__all__ = [
    "Feed",
    "SymbolFeed",
    "build_symbols",
    "discover_symbols",
    "resolve_symbols",
    "to_dataframe",
]
