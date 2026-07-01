"""ccxt wrapper, split into market-data and (inert) execution clients.

Why split:
  * Market data is needed even in signal-only mode; it must work without keys.
  * Order placement is the dangerous part; it is isolated behind an explicit
    ``ExecutionClient`` that refuses to act until the live gate is passed.

The live gate is enforced BOTH in the client (defence in depth) and in
validators at startup. The client check protects against a future call path
that bypasses validation (e.g. a script importing it directly).

Retry / rate-limit is implemented manually (no ``tenacity``) to keep the
dependency surface small and behaviour deterministic in tests.
"""
from __future__ import annotations

import asyncio
from typing import Any, cast

import ccxt.async_support as ccxt_async

from ..config.env import Config
from ..core import policy
from ..core.enums import Mode, OrderType, Side
from ..core.exceptions import (
    DataFeedError,
    ExchangeError,
    LiveTradingForbiddenError,
)
from ..core.logging_setup import get_logger

_log = get_logger("data.exchange")

# ccxt exceptions we treat as transient (retry) vs fatal (give up).
_RETRYABLE = (
    ccxt_async.NetworkError,
    ccxt_async.RequestTimeout,
    ccxt_async.DDoSProtection,
    ccxt_async.RateLimitExceeded,
)
_FATAL = (
    ccxt_async.ExchangeNotAvailable,
    ccxt_async.AuthenticationError,
)


class _BaseClient:
    """Common ccxt lifecycle + retry plumbing."""

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
        }
        # Inject credentials only when provided; ccxt treats empty string as
        # a credential, which can confuse some exchanges' public endpoints.
        if config.env.exchange_api_key:
            opts["apiKey"] = config.env.exchange_api_key
        if config.env.exchange_api_secret:
            opts["secret"] = config.env.exchange_api_secret

        try:
            self._ex = getattr(ccxt_async, exchange_name)(opts)
        except (AttributeError, TypeError) as exc:
            raise ExchangeError(
                f"ccxt has no exchange '{exchange_name}'. Check EXCHANGE_NAME."
            ) from exc
        self._ex.set_sandbox_mode(sandbox)
        self._closed = False

    async def __aenter__(self) -> _BaseClient:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._ex.close()
        except Exception as exc:  # noqa: BLE001 - close must not mask the real error
            _log.warning("error closing exchange connection: %s", exc)

    async def _with_retry(self, op: str, coro_fn: Any, *args: Any, **kwargs: Any) -> Any:
        """Run an async ccxt call with bounded retries + backoff."""
        if self._closed:
            raise ExchangeError("client is closed")
        last_exc: Exception | None = None
        for attempt in range(1, policy.DEFAULT_RETRY_ATTEMPTS + 1):
            try:
                return await coro_fn(*args, **kwargs)
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
                await asyncio.sleep(delay)
            except Exception as exc:  # noqa: BLE001 - surface as DataFeedError
                raise DataFeedError(f"{op}: unexpected error: {exc}") from exc
        raise DataFeedError(f"{op}: failed after {policy.DEFAULT_RETRY_ATTEMPTS} attempts: {last_exc}")


class MarketDataClient(_BaseClient):
    """Public market-data access: OHLCV, ticker, order book, markets."""

    async def load_markets(self) -> dict[str, Any]:
        return cast(dict[str, Any], await self._with_retry("load_markets", self._ex.load_markets))

    async def fetch_ohlcv(
        self, symbol: str, timeframe: str, limit: int = 200, since: int | None = None
    ) -> list[list[Any]]:
        limit = max(1, min(int(limit), policy.MAX_CANDLES_LOOKBACK))
        rows = await self._with_retry(
            "fetch_ohlcv", self._ex.fetch_ohlcv, symbol, timeframe, since, limit
        )
        return cast(list[list[Any]], rows or [])

    async def fetch_ticker(self, symbol: str) -> dict[str, Any]:
        return cast(dict[str, Any], await self._with_retry("fetch_ticker", self._ex.fetch_ticker, symbol))

    async def fetch_order_book(self, symbol: str, limit: int = 50) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await self._with_retry("fetch_order_book", self._ex.fetch_order_book, symbol, limit),
        )

    async def fetch_tickers(self, symbols: list[str] | None = None) -> dict[str, Any]:
        return cast(dict[str, Any], await self._with_retry("fetch_tickers", self._ex.fetch_tickers, symbols))


class ExecutionClient(_BaseClient):
    """Order placement. INERT until the live gate is passed.

    Defence-in-depth: even if a caller constructs this directly, methods raise
    ``LiveTradingForbiddenError`` unless every gate condition holds.
    """

    def _check_live(self) -> None:
        cfg = self._config
        if cfg.mode != Mode.LIVE:
            raise LiveTradingForbiddenError(
                f"order placement requires mode=LIVE (got {cfg.mode.value})."
            )
        if not policy.LIVE_TRADING_RELEASED:
            raise LiveTradingForbiddenError(
                "LIVE_TRADING_RELEASED is False; order placement disabled."
            )
        if not cfg.env.enable_live_trading:
            raise LiveTradingForbiddenError("ENABLE_LIVE_TRADING is false.")
        sandbox = (
            cfg.env.exchange_sandbox
            if cfg.env.exchange_sandbox is not None
            else cfg.settings.exchange.sandbox
        )
        if sandbox:
            raise LiveTradingForbiddenError(
                "sandbox is on; live requires production endpoints."
            )
        if not cfg.env.exchange_api_key or not cfg.env.exchange_api_secret:
            raise LiveTradingForbiddenError("API credentials are missing.")

    # The methods below are intentionally NOT implemented with real ccxt calls
    # in this stage. They exist as a stable interface and a hard block. They
    # will be filled in stage 6 once paper trading + tests are green.
    async def create_order(
        self,
        symbol: str,
        side: Side,
        amount: float,
        order_type: OrderType = OrderType.MARKET,
        price: float | None = None,
    ) -> dict[str, Any]:
        self._check_live()
        raise LiveTradingForbiddenError(
            "create_order is not implemented until live trading is released (stage 6)."
        )

    async def cancel_order(self, order_id: str, symbol: str) -> dict[str, Any]:
        self._check_live()
        raise LiveTradingForbiddenError(
            "cancel_order is not implemented until live trading is released (stage 6)."
        )

    async def fetch_balance(self) -> dict[str, Any]:
        self._check_live()
        raise LiveTradingForbiddenError(
            "fetch_balance is not implemented until live trading is released (stage 6)."
        )
