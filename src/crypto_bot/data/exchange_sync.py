"""Synchronous ccxt market-data client (bypasses aiodns issues on Windows)."""
from __future__ import annotations

import ccxt
from typing import Any, Self, cast

from ..config.env import Config
from ..core import policy
from ..core.exceptions import DataFeedError, ExchangeError
from ..core.logging_setup import get_logger

_log = get_logger("data.exchange_sync")

_RETRYABLE = (
    ccxt.NetworkError,
    ccxt.RequestTimeout,
    ccxt.DDoSProtection,
    ccxt.RateLimitExceeded,
)
_FATAL = (
    ccxt.ExchangeNotAvailable,
    ccxt.AuthenticationError,
)


class MarketDataClientSync:
    """Synchronous public market-data access: OHLCV, ticker, order book, markets."""

    def __init__(self, config: Config) -> None:
        self._config = config
        exchange_name = config.env.exchange_name or config.settings.exchange.name
        sandbox = (
            config.env.exchange_sandbox
            if config.env.exchange_sandbox is not None
            else config.settings.exchange.sandbox
        )
        opts: dict[str, Any] = {
            "enableRateLimit": True,
            "rateLimit": config.settings.exchange.rate_limit_ms,
            "timeout": 30000,
            "options": {"defaultType": "spot"},
        }
        # No credentials for public data (avoids private endpoint calls on Bybit)
        try:
            self._ex = getattr(ccxt, exchange_name)(opts)
        except (AttributeError, TypeError) as exc:
            raise ExchangeError(
                f"ccxt has no exchange '{exchange_name}'. Check EXCHANGE_NAME."
            ) from exc
        self._ex.set_sandbox_mode(sandbox)
        self._closed = False
        self._seed_markets(config)

    def _seed_markets(self, config: Config) -> None:
        from ..data.feed import build_symbols
        symbols = build_symbols(config)
        if not symbols:
            return
        quote = config.settings.universe.quote.upper()
        markets: dict[str, dict[str, Any]] = {}
        markets_by_id: dict[str, list[dict[str, Any]]] = {}
        for sym in symbols:
            base = sym.split("/")[0]
            exch_id = base + quote
            entry: dict[str, Any] = {
                "id": exch_id,
                "symbol": sym,
                "base": base,
                "quote": quote,
                "active": True,
                "spot": True,
                "future": False,
                "swap": False,
                "option": False,
                "type": "spot",
                "linear": False,
                "inverse": False,
                "precision": {"price": 8, "amount": 8},
                "limits": {"amount": {"min": 1e-8, "max": 1e8}},
                "info": {},
            }
            markets[sym] = entry
            markets_by_id[exch_id] = [entry]
        self._ex.markets = markets
        self._ex.markets_by_id = markets_by_id
        _log.info("seeded %d synthetic market entries (no API call)", len(markets))

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._ex.close()
        except Exception as exc:
            _log.warning("error closing exchange connection: %s", exc)

    def _with_retry(self, op: str, coro_fn: Any, *args: Any, **kwargs: Any) -> Any:
        if self._closed:
            raise ExchangeError("client is closed")
        last_exc: Exception | None = None
        for attempt in range(1, policy.DEFAULT_RETRY_ATTEMPTS + 1):
            try:
                return coro_fn(*args, **kwargs)
            except _FATAL as exc:
                raise ExchangeError(f"{op}: unrecoverable exchange error: {exc}") from exc
            except _RETRYABLE as exc:
                last_exc = exc
                delay = min(
                    policy.RETRY_BACKOFF_MAX_SECONDS,
                    policy.RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)),
                )
                _log.warning(
                    "%s: transient error (attempt %d/%d): %s; retrying in %.2fs",
                    op, attempt, policy.DEFAULT_RETRY_ATTEMPTS, exc, delay,
                )
                import time
                time.sleep(delay)
            except Exception as exc:  # noqa: BLE001
                raise DataFeedError(f"{op}: unexpected error: {exc}") from exc
        raise DataFeedError(f"{op}: failed after {policy.DEFAULT_RETRY_ATTEMPTS} attempts: {last_exc}")

    def fetch_ohlcv(
        self, symbol: str, timeframe: str, limit: int = 200, since: int | None = None
    ) -> list[list[Any]]:
        limit = max(1, min(int(limit), policy.MAX_CANDLES_LOOKBACK))
        rows = self._with_retry(
            f"fetch_ohlcv({symbol}, {timeframe})",
            self._ex.fetch_ohlcv,
            symbol, timeframe, since, limit,
        )
        if not hasattr(self, '_limit_logged'):
            self._limit_logged = True
            headers = self._ex.last_response_headers or {}
            _log.info(
                "bybit limit headers — X-Bapi-Limit: %s  X-Bapi-Limit-Status: %s",
                headers.get("X-Bapi-Limit"),
                headers.get("X-Bapi-Limit-Status"),
            )
        return cast(list[list[Any]], rows or [])

    def fetch_ticker(self, symbol: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._ex.fetch_ticker(symbol))

    def fetch_order_book(self, symbol: str, limit: int = 50) -> dict[str, Any]:
        return cast(dict[str, Any], self._ex.fetch_order_book(symbol, limit))

    def fetch_tickers(self, symbols: list[str] | None = None) -> dict[str, Any]:
        try:
            return cast(dict[str, Any], self._ex.fetch_tickers(symbols))
        except Exception as exc:
            _log.warning("fetch_tickers failed (will use DB fallback): [%s] %s", type(exc).__name__, exc)
            return {}

    def load_markets(self) -> dict[str, Any]:
        return cast(dict[str, Any], self._ex.markets or {})

    def available_symbols(self) -> set[str]:
        markets = self.load_markets()
        if markets:
            return set(markets.keys())
        raise ExchangeError("markets loaded but empty")