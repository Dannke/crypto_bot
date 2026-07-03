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
from typing import Any, Self, cast

import ccxt.async_support as ccxt_async

from ..config.env import Config
from ..core import policy
from ..core.enums import Mode, OrderType, Side
from ..core.exceptions import (
    DataFeedError,
    ExchangeError,
    LiveTradingForbiddenError,
)
import asyncio
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

    def __init__(self, config: Config, *, set_credentials: bool = True) -> None:
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
            # Short timeout so a geo-blocked / unreachable exchange fails fast
            # and the feed falls back to DB cache without blocking for minutes.
            "timeout": 3000,
            # Bybit's unified API serves multiple market "categories" (spot,
            # linear perpetuals, inverse, option) from the same endpoints.
            # Without this, ccxt's default category for calls like
            # fetch_tickers(None) is "linear" (derivatives) rather than
            # "spot" — so an unscoped fetch_tickers() silently returns
            # hundreds of perpetual-futures tickers and zero spot pairs,
            # even though this bot only ever builds "<BASE>/<QUOTE>" spot
            # symbols. This bit us as auto-discover finding 0 symbols.
            "options": {"defaultType": "spot"},
        }
        # Inject credentials only when required.
        # MarketDataClient (public-only) MUST NOT set credentials: on bybit,
        # load_markets() calls fetch_currencies() (a PRIVATE endpoint) which
        # will 403/block if credentials are present but invalid/geo-blocked,
        # making ALL fetch_ohlcv calls fail before any data can be cached.
        if set_credentials:
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

    async def __aenter__(self) -> Self:
        # `Self` (rather than `_BaseClient`) is what makes static type
        # checkers (Pylance/mypy) preserve the concrete subclass across
        # `async with MarketDataClient(config) as client:` — otherwise
        # `client` gets widened to the base class and loses access to
        # subclass-only methods like `fetch_tickers`/`available_symbols`.
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
    """Public market-data access: OHLCV, ticker, order book, markets.

    Does NOT set exchange API credentials — all data comes from public
    endpoints.  Bybit's ccxt implementation calls the private
    ``fetch_currencies()`` during ``load_markets()`` when credentials are
    present, which fails on testnet (CloudFront 403).  Not setting credentials
    allows ``fetch_currencies()`` to short-circuit with ``check_required_credentials(False)``.

    Avoids calling ccxt's ``load_markets()`` entirely by pre-seeding a
    minimal market registry from the configured universe.  This makes the
    client start instantly even when the exchange is geo-blocked — every
    API call can still fail, but the client itself won't.
    """

    def __init__(self, config: Config) -> None:
        super().__init__(config, set_credentials=False)
        # Pre-seed a minimal market registry so ccxt's fetch_ohlcv / fetch_tickers
        # don't trigger a full load_markets() call (which on bybit makes
        # multiple slow HTTP requests and can fail outright on a geo-blocked
        # testnet).  We only need: symbol -> market['id'] (exchange symbol)
        # and market['spot'] (True for all our pairs).
        self._seed_markets(config)

    def _seed_markets(self, config: Config) -> None:
        """Create minimal market entries for the configured universe."""
        from .feed import build_symbols
        symbols = build_symbols(config)
        if not symbols:
            return
        quote = config.settings.universe.quote.upper()
        markets: dict[str, dict[str, Any]] = {}
        markets_by_id: dict[str, list[dict[str, Any]]] = {}
        for sym in symbols:
            base = sym.split("/")[0]
            exch_id = base + quote  # e.g. "BTCUSDT"
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

    async def load_markets(self) -> dict[str, Any]:
        """Return pre-seeded markets without making any API call."""
        return cast(dict[str, Any], self._ex.markets or {})

    async def fetch_ohlcv(
        self, symbol: str, timeframe: str, limit: int = 200, since: int | None = None
    ) -> list[list[Any]]:
        """Fetch OHLCV data.  Fails fast — caller (feed) falls back to DB cache."""
        limit = max(1, min(int(limit), policy.MAX_CANDLES_LOOKBACK))
        try:
            rows = await self._ex.fetch_ohlcv(symbol, timeframe, since, limit)
            return cast(list[list[Any]], rows or [])
        except Exception as exc:
            raise DataFeedError(f"fetch_ohlcv failed for {symbol} {timeframe}: {exc}") from exc

    async def fetch_ticker(self, symbol: str) -> dict[str, Any]:
        return cast(dict[str, Any], await self._ex.fetch_ticker(symbol))

    async def fetch_order_book(self, symbol: str, limit: int = 50) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await self._ex.fetch_order_book(symbol, limit),
        )

    async def fetch_tickers(self, symbols: list[str] | None = None) -> dict[str, Any]:
        """Fetch tickers.  Returns empty dict on failure (feed falls back to DB cache)."""
        try:
            return cast(dict[str, Any], await asyncio.wait_for(
                self._ex.fetch_tickers(symbols), timeout=15,
            ))
        except asyncio.TimeoutError:
            _log.warning("fetch_tickers timed out after 15s (will use DB fallback)")
            return {}
        except Exception as exc:
            _log.warning(
                "fetch_tickers failed (will use DB fallback): [%s] %s",
                type(exc).__name__, exc,
            )
            return {}

    async def available_symbols(self) -> set[str]:
        """All symbols the exchange currently lists (mainnet vs testnet differ).

        Used to filter a configured watchlist before it hits ``fetch_tickers``/
        ``fetch_ohlcv`` — testnet in particular carries a much smaller market
        set, and asking for a delisted/unlisted symbol raises ``ccxt.BadSymbol``,
        which is a hard, non-retryable error that would otherwise abort the
        whole scan cycle for every symbol, not just the missing one.

        Raises ``ExchangeError`` on failure so ``resolve_symbols`` can
        distinguish "no symbols exist" from "exchange is unreachable" and
        fall back to the explicit watchlist (DB cache mode).
        """
        markets = await self.load_markets()
        if markets:
            return set(markets.keys())
        raise ExchangeError("markets loaded but empty")


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