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
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import pandas as pd

from ..config.env import Config
from ..core import policy
from ..core.exceptions import DataFeedError
from ..core.logging_setup import get_logger
from ..core.types import Candle
from ..storage.db import CandleRepository, Database
from .exchange import MarketDataClient

_log = get_logger("data.feed")

# How long to wait before retrying API for a (symbol, tf) pair that failed.
# Prevents tight retry loops without permanently disabling the pair.
FAILED_PAIR_COOLDOWN_SECONDS: float = 120.0


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
    if not tickers:
        _log.info("auto_discover: no tickers from exchange, skipping discovery")
        return []

    # Diagnostic breakdown: on some exchange endpoints (notably testnet),
    # most listed markets are derivatives (e.g. "BTC/USDT:USDT") rather than
    # spot pairs, and _base_for_symbol() intentionally excludes anything with
    # a ":" in it — a spot-only universe has no business auto-discovering
    # perpetual futures. If that's what's happening, auto_discover silently
    # returns an empty list, which looks identical to "no liquid symbols
    # exist" from the caller's side. Log the split so it's diagnosable.
    total = len(tickers)
    spot_like = sum(1 for sym in tickers if _base_for_symbol(sym, quote) is not None)
    if total and spot_like == 0:
        _log.warning(
            "auto_discover: exchange returned %d tickers but none matched "
            "'<BASE>/%s' spot format (no ':' allowed) — likely a derivatives-"
            "only endpoint for this quote currency. Auto-discover will add 0 symbols.",
            total, quote,
        )
    elif total:
        _log.info(
            "auto_discover: %d/%d tickers match spot format '<BASE>/%s'",
            spot_like, total, quote,
        )

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


async def resolve_symbols(client: MarketDataClient, config: Config, exchange_available: bool = True) -> list[str]:
    """Merge explicit watchlist symbols with auto-discovered high-liquidity pairs.

    The explicit watchlist is user-curated YAML and is NOT guaranteed to match
    what the exchange actually lists on the active endpoint — this matters a
    lot on testnet, whose market set is a small subset of mainnet's. Symbols
    absent from the exchange are dropped here (with a warning) rather than
    left to blow up ``fetch_tickers``/``fetch_ohlcv`` later with a hard,
    non-retryable ``ccxt.BadSymbol`` that would otherwise abort the whole
    scan cycle instead of just skipping the one bad symbol.

    Auto-discovered symbols never need this filter: they are derived directly
    from tickers the exchange itself returned, so they are available by
    construction.

    Graceful degradation: if the exchange API is unreachable (geo-blocked,
    DNS failure, etc.) the explicit watchlist is returned as-is so the
    orchestrator can still reach ``feed.fetch_many`` which falls back to
    its DB cache for candles.
    """
    explicit = build_symbols(config)

    if exchange_available:
        try:
            available = await client.available_symbols()
        except Exception as exc:
            _log.warning(
                "resolve_symbols: cannot check available symbols (%s); "
                "using explicit watchlist as-is (DB cache fallback)",
                exc,
            )
            available = None
    else:
        available = None

    if available is not None:
        missing = [s for s in explicit if s not in available]
        if missing:
            _log.warning(
                "universe: %d symbol(s) not listed on this exchange endpoint, skipping: %s",
                len(missing), missing,
            )
        explicit = [s for s in explicit if s in available]
    else:
        _log.info(
            "resolve_symbols: exchange unavailable, keeping %d explicit symbols",
            len(explicit),
        )

    discovered = [] if available is None else await discover_symbols(client, config)

    seen: set[str] = set()
    resolved: list[str] = []
    for symbol in [*explicit, *discovered]:
        if symbol not in seen:
            seen.add(symbol)
            resolved.append(symbol)

    _log.info(
        "universe resolved: %d explicit + %d discovered = %d total (after de-dup)",
        len(explicit), len(discovered), len(resolved),
    )
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
    """Fetches candle history across the configured timeframes with DB caching.

    When the exchange is unreachable (geo-blocked, DNS failure, etc.) the feed
    silently falls back to whatever candles are already cached in the DB.
    Callers can set ``exchange_available`` to ``False`` to skip all API calls
    and use the DB cache only, avoiding slow HTTP timeouts.
    """

    def __init__(self, client: MarketDataClient, config: Config, db: Database | None = None) -> None:
        self._client = client
        self._config = config
        self._timeframes = config.settings.timeframes.primary
        self._limit = config.settings.timeframes.candles_per_tf
        self._db = db
        self._candle_repo = CandleRepository(db) if db else None
        self._exchange_available = True
        # Per-(symbol, tf) failure cooldown — maps (symbol, tf) to the
        # timestamp when it failed.  Pairs are retried after a cooldown
        # period so one bad request doesn't permanently disable a pair.
        self._failed_api_pairs: dict[tuple[str, str], float] = {}
        # In-memory cache: (symbol, tf) -> list[Candle] — avoids re-reading
        # the full candle window from DB on every cycle.
        self._cache: dict[tuple[str, str], list[Candle]] = {}

    @property
    def exchange_available(self) -> bool:
        return self._exchange_available

    @exchange_available.setter
    def exchange_available(self, value: bool) -> None:
        self._exchange_available = value

    async def fetch_symbol(self, symbol: str) -> SymbolFeed:
        """Fetch all timeframes for one symbol, concurrently with DB caching."""
        async def _one(tf: str) -> tuple[str, list[Candle]]:
            cache_key = (symbol, tf)
            cached = self._cache.get(cache_key)
            latest_ts: int | None = None
            now_ms = int(time.time() * 1000)
            period_ms = policy.timeframe_to_seconds(tf) * 1000
            
            if cached is not None:
                # Use in-memory cache from the previous cycle
                latest_ts = cached[-1].timestamp if cached else None
            elif self._candle_repo:
                # First cycle — load from DB
                latest_ts = await self._candle_repo.latest_ts_async(symbol, tf)
                if latest_ts is not None:
                    cached = await self._candle_repo.fetch_async(symbol, tf, limit=self._limit)
                    _log.info("feed: db %s %s = %d candles", symbol, tf, len(cached))
            
            # Try fetching new candles from API.
            # If this (symbol, tf) pair failed recently, skip API and go
            # straight to DB cache until the cooldown expires.
            fetched_new: list[Candle] = []
            failed_at = self._failed_api_pairs.get((symbol, tf))
            should_try_api = failed_at is None or (time.time() - failed_at) > FAILED_PAIR_COOLDOWN_SECONDS
            if should_try_api:
                try:
                    if latest_ts is not None:
                        # Chunked catch-up: fetch in batches until we've caught up to present.
                        # If the exchange returns a full batch (== self._limit), there may be
                        # more candles — keep fetching until we get a partial batch.
                        fetch_since: int = latest_ts
                        chunks = 0
                        while True:
                            raw = await self._client.fetch_ohlcv(
                                symbol, tf, limit=self._limit, since=fetch_since,
                            )
                            batch = _sort_dedup([_row_to_candle(r) for r in raw])
                            batch = [c for c in batch if c.timestamp > latest_ts]
                            batch = [c for c in batch if c.timestamp + period_ms <= now_ms]
                            if not batch:
                                break
                            fetched_new.extend(batch)
                            chunks += 1
                            # Partial batch means we've reached the exchange's latest data
                            if len(batch) < self._limit:
                                break
                            fetch_since = batch[-1].timestamp + 1
                        _log.info(
                            "feed: api+ %s %s: +%d from ts=%d (%d chunks)",
                            symbol, tf, len(fetched_new), latest_ts, chunks,
                        )
                    else:
                        raw = await self._client.fetch_ohlcv(symbol, tf, limit=self._limit)
                        fetched_new = _sort_dedup([_row_to_candle(r) for r in raw])
                        fetched_new = [c for c in fetched_new if c.timestamp + period_ms <= now_ms]
                    # Success — clear any previous failure timestamp
                    self._failed_api_pairs.pop((symbol, tf), None)
                except Exception as exc:
                    _log.warning("feed: api fail %s %s: %s", symbol, tf, exc)
                    self._failed_api_pairs[(symbol, tf)] = time.time()
            
            all_candles = (cached or []) + fetched_new
            all_candles = _sort_dedup(all_candles)
            
            if len(all_candles) > self._limit:
                all_candles = all_candles[-self._limit:]
            
            # Update in-memory cache
            self._cache[cache_key] = all_candles
            
            if not all_candles:
                _log.warning("feed: %s %s = 0 candles — skipping", symbol, tf)
                return tf, []
            
            if fetched_new and self._candle_repo:
                await self._candle_repo.upsert_many_async(symbol, tf, fetched_new)
            
            new_suffix = f" [+{len(fetched_new)}]" if fetched_new else ""
            _log.info(
                "feed: %s %s = %d candles%s  %d .. %d",
                symbol, tf, len(all_candles), new_suffix,
                all_candles[0].timestamp, all_candles[-1].timestamp,
            )
            
            return tf, all_candles

        results = await asyncio.gather(*[_one(tf) for tf in self._timeframes])
        non_empty = {tf: candles for tf, candles in results if candles}
        return SymbolFeed(symbol=symbol, by_timeframe=non_empty)

    async def fetch_many(self, symbols: Iterable[str]) -> list[SymbolFeed]:
        """Fetch many symbols concurrently; isolate per-symbol failures.

        A failing symbol is logged and skipped rather than aborting the scan —
        the bot's whole point is to scan a wide universe, so one outage must
        not blank the rest.
        """
        symbols = list(symbols)  # materialise once

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