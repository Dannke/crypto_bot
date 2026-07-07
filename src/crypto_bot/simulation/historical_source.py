"""Historical data source for backtesting.

Reads OHLCV from the existing SQLite ``candles`` table once, then serves
in-memory slices filtered to closed bars only — no repeated database queries
inside the replay loop.

Design:
  - ``load_all(symbol, timeframe)`` — one async SQL query via ``CandleRepository``,
    caches all candles + precomputes close timestamps for O(log n) slicing.
  - ``slice(as_of_ms)`` — binary search, returns only bars whose full period
    has elapsed by *as_of_ms* (no look-ahead).
"""
from __future__ import annotations

import bisect

from ..core.policy import timeframe_to_seconds
from ..core.types import Candle
from ..storage.db import CandleRepository


class HistoricalCandleSource:
    """Candle provider that reads from the local archive, not from the exchange.

    Usage::

        source = HistoricalCandleSource(repo)
        await source.load_all_async("BTC/USDT", "1h")
        closed = source.slice(as_of_ms=1_700_000_000_000)   # fast, no I/O
    """

    def __init__(self, repo: CandleRepository | None = None) -> None:
        self._repo = repo
        self._symbol: str = ""
        self._timeframe: str = ""
        self._candles: list[Candle] = []
        self._close_times: list[int] = []
        self._period_ms: int = 0

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
        self._symbol = symbol
        self._timeframe = timeframe
        self._candles = candles
        self._period_ms = timeframe_to_seconds(timeframe) * 1000
        self._close_times = [c.timestamp + self._period_ms for c in candles]

    def slice(self, as_of_ms: int) -> list[Candle]:
        """Return all bars fully closed by *as_of_ms*, maintaining ascending order.

        This is an O(log n) operation on the in-memory cache.  Every bar whose
        ``open + period <= as_of_ms`` is included; partially-formed bars are
        excluded by construction, so no look-ahead is possible.
        """
        if not self._candles:
            return []
        idx = bisect.bisect_right(self._close_times, as_of_ms)
        return self._candles[:idx]

    @property
    def loaded(self) -> bool:
        return len(self._candles) > 0

    @property
    def symbol(self) -> str:
        return self._symbol

    @property
    def timeframe(self) -> str:
        return self._timeframe
