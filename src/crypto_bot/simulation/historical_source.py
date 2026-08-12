"""Historical data source for backtesting.

Reads OHLCV from the existing SQLite ``candles`` table once, then serves
in-memory slices filtered to closed bars only — no repeated database queries
inside the replay loop.

Supports multiple symbols and timeframes simultaneously.

Design:
  - ``load_all_async(symbol, timeframe)`` — one async SQL query per pair,
    caches all candles + precomputes close timestamps for O(log n) slicing.
  - ``slice(as_of_ms, symbol, timeframe)`` — binary search, returns only bars
    whose full period has elapsed by *as_of_ms* (no look-ahead).
"""
from __future__ import annotations

import bisect

from ..core.policy import timeframe_to_seconds
from ..core.types import Candle
from ..storage.db import CandleRepository


class HistoricalCandleSource:
    """Candle provider that reads from the local archive, not from the exchange.

    Stores candle data per (symbol, timeframe) pair. Usage::

        source = HistoricalCandleSource(repo)
        await source.load_all_async("BTC/USDT", "1h")
        await source.load_all_async("ETH/USDT", "1h")
        closed_btc = source.slice(1_700_000_000_000, "BTC/USDT", "1h")
        closed_eth = source.slice(1_700_000_000_000, "ETH/USDT", "1h")
    """

    def __init__(self, repo: CandleRepository | None = None) -> None:
        self._repo = repo
        # { (symbol, timeframe): (candles, close_times, period_ms) }
        self._cache: dict[tuple[str, str], tuple[list[Candle], list[int], int]] = {}

    async def load_all_async(self, symbol: str, timeframe: str) -> None:
        """Load all available candles for *symbol* / *timeframe* into memory.

        Uses ``CandleRepository.fetch_since`` with ``since_ts=0`` to bypass
        the ``MAX_CANDLES_LOOKBACK`` cap — assumes the caller has already
        populated the DB (e.g. via ``scripts/seed_history.py``).
        """
        assert self._repo is not None, "HistoricalCandleSource needs a repo to load from DB"
        candles = await self._repo.fetch_since(symbol, timeframe, since_ts=0)
        self._set_candles(symbol, timeframe, candles)

    def load_all(self, symbol: str, timeframe: str, candles: list[Candle]) -> None:
        """Load pre-fetched candles (useful for tests / synthetic data)."""
        self._set_candles(symbol, timeframe, candles)

    def _set_candles(self, symbol: str, timeframe: str, candles: list[Candle]) -> None:
        period_ms = timeframe_to_seconds(timeframe) * 1000
        close_times = [c.timestamp + period_ms for c in candles]
        self._cache[(symbol, timeframe)] = (candles, close_times, period_ms)

    def slice(self, as_of_ms: int, symbol: str, timeframe: str, limit: int = 400) -> list[Candle]:
        """Return up to ``limit`` bars fully closed by *as_of_ms*.

        This is an O(log n) operation on the in-memory cache, followed by a
        constant-sized slice of at most ``limit`` rows.  Every bar whose
        ``open + period <= as_of_ms`` is included; partially-formed bars are
        excluded by construction, so no look-ahead is possible.
        """
        key = (symbol, timeframe)
        entry = self._cache.get(key)
        if entry is None:
            return []
        candles, close_times, _ = entry
        if not candles:
            return []
        idx = bisect.bisect_right(close_times, as_of_ms)
        start = max(0, idx - limit)
        return candles[start:idx]

    def slice_between(
        self, start_ms: int, end_ms: int, symbol: str, timeframe: str,
    ) -> list[Candle]:
        """Return ALL bars whose close time is within [start_ms, end_ms].

        Unlike :meth:`slice` there is no ``limit`` cap — callers need the
        complete window (e.g. building the unified replay clock) and must
        not silently drop history.
        """
        key = (symbol, timeframe)
        entry = self._cache.get(key)
        if entry is None:
            return []
        candles, close_times, _ = entry
        if not candles:
            return []
        lo = bisect.bisect_left(close_times, start_ms)
        hi = bisect.bisect_right(close_times, end_ms)
        return candles[lo:hi]

    def is_loaded(self, symbol: str, timeframe: str) -> bool:
        """Check if a specific (symbol, timeframe) pair is loaded."""
        key = (symbol, timeframe)
        entry = self._cache.get(key)
        return entry is not None and len(entry[0]) > 0

    @property
    def loaded(self) -> bool:
        """True if any data is cached."""
        return any(len(c) > 0 for c, _, _ in self._cache.values())

    @property
    def cached_keys(self) -> list[tuple[str, str]]:
        return list(self._cache.keys())
