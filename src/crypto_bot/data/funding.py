"""Funding rate data layer for Bybit perpetual futures.

Provides:
- FundingEvent: immutable record of a funding settlement
- BybitFundingClient: async client for fetching funding history from Bybit API
- FundingRepository: typed CRUD for funding_rates table
- HistoricalFundingSource: in-memory cache with O(log n) slicing (no look-ahead)
- InstrumentFundingIntervalCache: per-symbol funding interval from instruments-info
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..config.env import Config
from ..core.exceptions import DataFeedError, ExchangeError
from ..core.logging_setup import get_logger
from ..storage.db import Database

_log = get_logger("data.funding")


@dataclass(frozen=True, slots=True)
class FundingEvent:
    """Immutable funding settlement record."""
    symbol: str
    funding_time_ms: int      # actual settlement timestamp from exchange
    funding_rate: float       # signed decimal, e.g. 0.0001 = 0.01%
    mark_price: float | None  # mark price at settlement if available


class BybitFundingClient:
    """Thin client over Bybit v5 funding history endpoint.

    GET /v5/market/funding/history (category=linear)
    Paginates by startTime/endTime windows (max 200 records per call).
    """

    ENDPOINT = "/v5/market/funding/history"
    MAX_RECORDS_PER_CALL = 200
    DEFAULT_WINDOW_DAYS = 30

    def __init__(self, config: Config) -> None:
        self._config = config
        exchange_name = config.env.exchange_name or config.settings.exchange.name
        sandbox = (
            config.env.exchange_sandbox
            if config.env.exchange_sandbox is not None
            else config.settings.exchange.sandbox
        )
        import ccxt.async_support as ccxt_async

        opts: dict[str, Any] = {
            "enableRateLimit": True,
            "rateLimit": config.settings.exchange.rate_limit_ms,
            "timeout": 5000,
            "options": {"defaultType": "swap"},  # linear perpetuals
        }
        try:
            self._ex = getattr(ccxt_async, exchange_name)(opts)
        except (AttributeError, TypeError) as exc:
            raise ExchangeError(
                f"ccxt has no exchange '{exchange_name}'. Check EXCHANGE_NAME."
            ) from exc
        self._ex.set_sandbox_mode(sandbox)
        self._closed = False

    async def __aenter__(self) -> BybitFundingClient:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._ex.close()
        except Exception as exc:  # noqa: BLE001
            _log.warning("error closing funding client: %s", exc)

    async def fetch_funding_history(
        self,
        symbol: str,
        start_ms: int,
        end_ms: int | None = None,
    ) -> list[FundingEvent]:
        """Fetch all funding events for symbol in [start_ms, end_ms].

        Handles pagination by advancing startTime after each batch.
        Returns events sorted by funding_time_ms ascending.
        """
        if self._closed:
            raise ExchangeError("client is closed")

        all_events: list[FundingEvent] = []
        cursor_start = start_ms
        end_time = end_ms or int(datetime.now(tz=UTC).timestamp() * 1000)

        # Use aiohttp directly for the public API endpoint (no auth needed)
        import aiohttp

        sandbox = (
            self._config.env.exchange_sandbox
            if self._config.env.exchange_sandbox is not None
            else self._config.settings.exchange.sandbox
        )
        base_url = "https://api-testnet.bybit.com" if sandbox else "https://api.bybit.com"
        url = f"{base_url}{self.ENDPOINT}"

        async with aiohttp.ClientSession() as session:
            while cursor_start < end_time:
                window_end = min(cursor_start + self.DEFAULT_WINDOW_DAYS * 86_400_000, end_time)
                # Bybit API expects symbol without slash (e.g., "BTCUSDT")
                api_symbol = symbol.replace("/", "")
                params = {
                    "category": "linear",
                    "symbol": api_symbol,
                    "startTime": cursor_start,
                    "endTime": window_end,
                    "limit": self.MAX_RECORDS_PER_CALL,
                }
                try:
                    async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        resp.raise_for_status()
                        data = await resp.json()
                except Exception as exc:
                    raise DataFeedError(f"fetch_funding_history({symbol}): {exc}") from exc

                result = data.get("result", {})
                items = result.get("list", [])
                if not items:
                    cursor_start = window_end
                    continue

                for item in items:
                    funding_time = int(item["fundingRateTimestamp"])
                    rate = float(item["fundingRate"])
                    mark = item.get("markPrice")
                    mark_price = float(mark) if mark is not None else None
                    all_events.append(
                        FundingEvent(
                            symbol=symbol,
                            funding_time_ms=funding_time,
                            funding_rate=rate,
                            mark_price=mark_price,
                        )
                    )

                # Advance cursor to after the last event we got
                last_time = max(e.funding_time_ms for e in all_events[-len(items):])
                cursor_start = last_time + 1

                # If we got fewer than max records, we're done with this window
                if len(items) < self.MAX_RECORDS_PER_CALL:
                    cursor_start = window_end

        # Deduplicate by funding_time_ms (API may return overlaps at boundaries)
        seen = set()
        unique: list[FundingEvent] = []
        for ev in all_events:
            if ev.funding_time_ms not in seen:
                seen.add(ev.funding_time_ms)
                unique.append(ev)

        unique.sort(key=lambda e: e.funding_time_ms)
        return unique


class FundingRepository:
    """Typed CRUD for funding_rates table (append-only facts)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def upsert_many(self, events: list[FundingEvent]) -> int:
        """Bulk upsert funding events. Idempotent on (symbol, funding_time_ms)."""
        if not events:
            return 0
        rows = [
            (e.symbol, e.funding_time_ms, e.funding_rate, e.mark_price)
            for e in events
        ]
        with self._db.transaction() as conn:
            conn.executemany(
                """INSERT INTO funding_rates (symbol, funding_time_ms, funding_rate, mark_price)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(symbol, funding_time_ms) DO UPDATE SET
                       funding_rate=excluded.funding_rate,
                       mark_price=excluded.mark_price""",
                rows,
            )
        return len(rows)

    def get_events(
        self, symbol: str, from_ms: int, to_ms: int
    ) -> list[FundingEvent]:
        """Fetch events for symbol in [from_ms, to_ms] (inclusive)."""
        rows = self._db.conn.execute(
            """SELECT symbol, funding_time_ms, funding_rate, mark_price
                 FROM funding_rates
                WHERE symbol=? AND funding_time_ms BETWEEN ? AND ?
                ORDER BY funding_time_ms""",
            (symbol, from_ms, to_ms),
        ).fetchall()
        return [
            FundingEvent(
                symbol=r["symbol"],
                funding_time_ms=r["funding_time_ms"],
                funding_rate=r["funding_rate"],
                mark_price=r["mark_price"] if r["mark_price"] is not None else None,
            )
            for r in rows
        ]

    def latest_event(self, symbol: str) -> FundingEvent | None:
        """Most recent funding event for symbol."""
        row = self._db.conn.execute(
            """SELECT symbol, funding_time_ms, funding_rate, mark_price
                 FROM funding_rates
                WHERE symbol=?
                ORDER BY funding_time_ms DESC LIMIT 1""",
            (symbol,),
        ).fetchone()
        if not row:
            return None
        return FundingEvent(
            symbol=row["symbol"],
            funding_time_ms=row["funding_time_ms"],
            funding_rate=row["funding_rate"],
            mark_price=row["mark_price"] if row["mark_price"] is not None else None,
        )


class HistoricalFundingSource:
    """In-memory funding history cache with O(log n) slicing (no look-ahead).

    Mirrors HistoricalCandleSource design: load once, slice many times.
    Events are visible only if funding_time_ms <= as_of_ms.
    """

    def __init__(self, repo: FundingRepository | None = None) -> None:
        self._repo = repo
        # { symbol: (events, funding_times) }
        self._cache: dict[str, tuple[list[FundingEvent], list[int]]] = {}

    def load_from_repo(
        self, symbol: str, from_ms: int = 0, to_ms: int | None = None
    ) -> None:
        """Load funding events from repository into memory."""
        assert self._repo is not None, "HistoricalFundingSource needs a repo"
        to = to_ms or int(datetime.now(tz=UTC).timestamp() * 1000)
        events = self._repo.get_events(symbol, from_ms, to)
        self._set_events(symbol, events)

    def load_events(self, symbol: str, events: list[FundingEvent]) -> None:
        """Load pre-fetched events (tests / synthetic data)."""
        self._set_events(symbol, events)

    def _set_events(self, symbol: str, events: list[FundingEvent]) -> None:
        funding_times = [e.funding_time_ms for e in events]
        self._cache[symbol] = (events, funding_times)

    def events_up_to(self, as_of_ms: int, symbol: str) -> list[FundingEvent]:
        """Return all funding events with funding_time_ms <= as_of_ms.

        No look-ahead: future settlement timestamps are excluded by bisect.
        """
        entry = self._cache.get(symbol)
        if entry is None:
            return []
        events, funding_times = entry
        if not events:
            return []
        idx = bisect.bisect_right(funding_times, as_of_ms)
        return events[:idx]

    def events_between(
        self, start_ms: int, end_ms: int, symbol: str
    ) -> list[FundingEvent]:
        """Return events with funding_time_ms in [start_ms, end_ms]."""
        entry = self._cache.get(symbol)
        if entry is None:
            return []
        events, funding_times = entry
        if not events:
            return []
        lo = bisect.bisect_left(funding_times, start_ms)
        hi = bisect.bisect_right(funding_times, end_ms)
        return events[lo:hi]

    def is_loaded(self, symbol: str) -> bool:
        entry = self._cache.get(symbol)
        return entry is not None and len(entry[0]) > 0

    @property
    def loaded_symbols(self) -> list[str]:
        return [s for s, (ev, _) in self._cache.items() if ev]


class InstrumentFundingIntervalCache:
    """Per-symbol funding interval cache from instruments-info.

    Bybit sets funding interval per symbol (usually 8h, sometimes 1h during
    rate caps). This cache reads the interval once and uses it as a sanity
    check when loading history (detect gaps/anomalies). Does NOT generate
    timestamps — funding history must come from actual events.
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._cache: dict[str, int] = {}  # symbol -> interval_ms
        self._fetched = False

    async def ensure_fetched(self) -> None:
        """Fetch intervals for all linear perpetuals from instruments-info."""
        if self._fetched:
            return
        import ccxt.async_support as ccxt_async

        exchange_name = self._config.env.exchange_name or self._config.settings.exchange.name
        sandbox = (
            self._config.env.exchange_sandbox
            if self._config.env.exchange_sandbox is not None
            else self._config.settings.exchange.sandbox
        )
        opts = {
            "enableRateLimit": True,
            "rateLimit": self._config.settings.exchange.rate_limit_ms,
            "timeout": 5000,
            "options": {"defaultType": "swap"},
        }
        ex = getattr(ccxt_async, exchange_name)(opts)
        ex.set_sandbox_mode(sandbox)
        try:
            resp = await ex.fetch("GET", "/v5/market/instruments-info", {"category": "linear"})
            for item in resp.get("result", {}).get("list", []):
                if item.get("status") != "Trading":
                    continue
                sym = item["symbol"]
                # fundingInterval is in minutes as string, e.g. "480" (8h) or "60" (1h)
                interval_min = item.get("fundingInterval")
                if interval_min:
                    self._cache[sym] = int(interval_min) * 60_000
        except Exception as exc:
            _log.warning("failed to fetch funding intervals: %s", exc)
        finally:
            await ex.close()
        self._fetched = True

    def get_interval_ms(self, symbol: str) -> int | None:
        """Return funding interval in milliseconds, or None if unknown."""
        # Bybit uses symbols like "BTCUSDT" without slash
        return self._cache.get(symbol.replace("/", ""))

    def validate_history(self, symbol: str, events: list[FundingEvent]) -> list[str]:
        """Sanity-check event timestamps against known interval.

        Returns list of warnings (empty if OK). Does not raise — gaps may be
        legitimate (delisting, exchange maintenance).
        """
        interval = self.get_interval_ms(symbol)
        if not interval or len(events) < 2:
            return []

        warnings = []
        for i in range(1, len(events)):
            expected = events[i - 1].funding_time_ms + interval
            actual = events[i].funding_time_ms
            diff = abs(actual - expected)
            if diff > interval * 0.1:  # >10% drift
                warnings.append(
                    f"{symbol}: funding gap at {actual} (expected ~{expected}, interval={interval/3_600_000:.0f}h)"
                )
        return warnings