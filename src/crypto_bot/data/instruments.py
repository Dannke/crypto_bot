"""Bybit instruments info client and cache for R0.4.

Provides per-symbol instrument specifications:
- qtyStep (lot size step)
- minOrderQty (minimum order quantity)
- minNotionalValue (minimum order notional)
- tickSize (price step)
- maxLeverage (maximum leverage)

Used for universe filtering (fail closed) and position sizing (rounding + renormalization).

Specs of both markets an instrument trades on are kept apart: ``linear`` —
USDT perpetuals, with the ``launchTime`` Bybit publishes for them — and
``spot``, which has no launch time. A backtest takes its specs from a
versioned snapshot (:func:`write_snapshot`, :meth:`InstrumentCache.from_snapshot`),
pinned by sha256, rather than from the 24-hour cache: the cache holds whatever
list the last fetch returned, testnet or mainnet, on the day of the run.
"""
from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..config.env import Config
from ..core.exceptions import ExchangeError
from ..core.logging_setup import get_logger

_log = get_logger("data.instruments")

# Cache file for instrument specs (persisted across restarts)
CACHE_DIR = Path("data/cache")
CACHE_FILE = CACHE_DIR / "bybit_instruments.json"
CACHE_TTL_SECONDS = 24 * 3600  # 24 hours

MARKETS = ("linear", "spot")
SNAPSHOT_FORMAT = 2
MAINNET_URL = "https://api.bybit.com"
TESTNET_URL = "https://api-testnet.bybit.com"


@dataclass(frozen=True, slots=True)
class InstrumentSpec:
    """Instrument specification from Bybit instruments-info."""
    symbol: str                    # e.g., "BTCUSDT"
    qty_step: float               # lotSizeFilter.qtyStep (spot: basePrecision)
    min_order_qty: float          # lotSizeFilter.minOrderQty
    min_notional_value: float     # lotSizeFilter.minNotionalValue (spot: minOrderAmt)
    tick_size: float              # priceFilter.tickSize
    max_leverage: int             # leverageFilter.maxLeverage (spot: 1)
    status: str                   # Trading, PreLaunch, etc.
    contract_type: str            # LinearPerpetual, LinearFutures; Spot for spot pairs
    market: str = "linear"        # instruments-info category the spec came from
    launch_time_ms: int | None = None  # launchTime when the API gives one; none in old cache files

    def __post_init__(self) -> None:
        if self.market not in MARKETS:
            raise ValueError(f"market must be one of {MARKETS}, got {self.market!r}")
        if self.qty_step <= 0:
            raise ValueError("qty_step must be positive")
        if self.min_order_qty < 0:
            raise ValueError("min_order_qty must be non-negative")
        if self.min_notional_value < 0:
            raise ValueError("min_notional_value must be non-negative")
        if self.tick_size <= 0:
            raise ValueError("tick_size must be positive")
        if self.max_leverage < 1:
            raise ValueError("max_leverage must be >= 1")


class BybitInstrumentsClient:
    """Fetches instrument specifications from Bybit v5 API."""

    ENDPOINT = "/v5/market/instruments-info"

    def __init__(self, config: Config, base_url: str | None = None) -> None:
        """``base_url`` overrides the endpoint the config's sandbox flag would pick."""
        self._config = config
        self._base_url = base_url
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
            "timeout": 10000,
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

    async def __aenter__(self) -> BybitInstrumentsClient:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._ex.close()
        except Exception as exc:
            _log.warning("error closing instruments client: %s", exc)

    @property
    def base_url(self) -> str:
        """Endpoint the specs come from: the override, else the config's sandbox flag."""
        if self._base_url is not None:
            return self._base_url
        sandbox = (
            self._config.env.exchange_sandbox
            if self._config.env.exchange_sandbox is not None
            else self._config.settings.exchange.sandbox
        )
        return TESTNET_URL if sandbox else MAINNET_URL

    async def fetch_all_linear_perpetuals(self) -> list[InstrumentSpec]:
        """Fetch all linear instruments from Bybit (perpetuals and dated futures)."""
        return await self.fetch_instruments("linear")

    async def fetch_instruments(self, category: str) -> list[InstrumentSpec]:
        """Fetch every instrument of a category — ``linear`` or ``spot`` — from Bybit."""
        if category not in MARKETS:
            raise ValueError(f"category must be one of {MARKETS}, got {category!r}")
        if self._closed:
            raise ExchangeError("client is closed")

        all_items: list[dict[str, Any]] = []
        cursor: str | None = None

        # Use aiohttp directly for the public API endpoint (no auth needed)
        import aiohttp

        url = f"{self.base_url}{self.ENDPOINT}"

        async with aiohttp.ClientSession() as session:
            while True:
                params: dict[str, Any] = {"category": category, "limit": 1000}
                if cursor:
                    params["cursor"] = cursor

                try:
                    async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        resp.raise_for_status()
                        data = await resp.json()
                except Exception as exc:
                    raise ExchangeError(f"fetch_instruments_info: {exc}") from exc

                result = data.get("result", {})
                items = result.get("list", [])
                all_items.extend(items)

                cursor = result.get("nextPageCursor")
                if not cursor:
                    break

        specs: list[InstrumentSpec] = []
        for item in all_items:
            try:
                spec = self._parse_item(item, category)
                specs.append(spec)
            except Exception as exc:
                _log.warning("failed to parse instrument %s: %s", item.get("symbol", "?"), exc)

        return specs

    @staticmethod
    def _parse_item(item: dict[str, Any], category: str) -> InstrumentSpec:
        """Parse a single instrument item of a category from Bybit response."""
        symbol = item["symbol"]
        lot = item.get("lotSizeFilter", {})
        price = item.get("priceFilter", {})
        tick_size = float(price.get("tickSize", "0"))
        status = item.get("status", "")
        launch = item.get("launchTime")
        launch_time_ms = int(launch) if launch else None

        if category == "spot":
            # Spot pairs have no qtyStep, minNotionalValue or leverage: the quantity
            # step is basePrecision and the minimum order is minOrderAmt
            return InstrumentSpec(
                symbol=symbol,
                qty_step=float(lot.get("basePrecision", "0")),
                min_order_qty=float(lot.get("minOrderQty", "0")),
                min_notional_value=float(lot.get("minOrderAmt", "0")),
                tick_size=tick_size,
                max_leverage=1,
                status=status,
                contract_type="Spot",
                market="spot",
                launch_time_ms=launch_time_ms,
            )

        lev = item.get("leverageFilter", {})
        return InstrumentSpec(
            symbol=symbol,
            qty_step=float(lot.get("qtyStep", "0")),
            min_order_qty=float(lot.get("minOrderQty", "0")),
            min_notional_value=float(lot.get("minNotionalValue", "0")),
            tick_size=tick_size,
            max_leverage=int(float(lev.get("maxLeverage", "1"))),
            status=status,
            contract_type=item.get("contractType", ""),
            market="linear",
            launch_time_ms=launch_time_ms,
        )


class InstrumentCache:
    """Instrument specifications of both markets: a 24-hour disk cache or a snapshot."""

    def __init__(self, config: Config | None) -> None:
        self._config = config
        self._cache: dict[str, InstrumentSpec] = {}  # linear, keyed by symbol without slash
        self._spot: dict[str, InstrumentSpec] = {}
        self._loaded = False
        self.api_base_url: str | None = None  # endpoint the specs came from, when known
        self.snapshot_sha256: str | None = None  # set when loaded from a snapshot

    @classmethod
    def from_specs(cls, specs: Iterable[InstrumentSpec]) -> InstrumentCache:
        """A loaded cache holding ``specs``; it never reads the disk cache or the API."""
        cache = cls(None)
        for spec in specs:
            cache._specs(spec.market)[spec.symbol] = spec
        cache._loaded = True
        return cache

    @classmethod
    def from_snapshot(cls, path: str | Path, expected_sha256: str | None = None) -> InstrumentCache:
        """Load a snapshot written by :func:`write_snapshot`, or an old 24-hour cache file.

        No TTL applies and nothing is fetched. A cache file of the old format
        holds linear perpetuals only, without launch times or a source.
        """
        raw = Path(path).read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if expected_sha256 is not None and digest != expected_sha256.lower():
            raise ValueError(f"{path}: sha256 {digest}, expected {expected_sha256}")
        data = json.loads(raw)
        if data.get("format") == SNAPSHOT_FORMAT:
            specs = [
                InstrumentSpec(**spec)
                for by_symbol in data["specs"].values()
                for spec in by_symbol.values()
            ]
            source = data.get("source")
        else:
            specs = [InstrumentSpec(**spec) for spec in data.get("specs", {}).values()]
            source = None
        cache = cls.from_specs(specs)
        cache.api_base_url = source
        cache.snapshot_sha256 = digest
        _log.info("loaded %d instrument specs from snapshot %s (sha256 %s)", len(specs), path, digest)
        return cache

    def _specs(self, market: str) -> dict[str, InstrumentSpec]:
        if market == "linear":
            return self._cache
        if market == "spot":
            return self._spot
        raise ValueError(f"market must be one of {MARKETS}, got {market!r}")

    def _load_from_disk(self) -> bool:
        """Load cache from disk if not expired. Returns True if valid."""
        if not CACHE_FILE.exists():
            return False
        try:
            data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            saved_at = data.get("saved_at", 0)
            if time.time() - saved_at > CACHE_TTL_SECONDS:
                return False
            for sym, spec_data in data.get("specs", {}).items():
                self._cache[sym] = InstrumentSpec(**spec_data)
            self._loaded = True
            _log.info("loaded %d instrument specs from cache", len(self._cache))
            return True
        except Exception as exc:
            _log.warning("failed to load instrument cache: %s", exc)
            return False

    def _save_to_disk(self) -> None:
        """Save cache to disk."""
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "saved_at": time.time(),
            "specs": {sym: asdict(spec) for sym, spec in self._cache.items()},
        }
        CACHE_FILE.write_text(json.dumps(data), encoding="utf-8")
        _log.info("saved %d instrument specs to cache", len(self._cache))

    async def ensure_loaded(self) -> None:
        """Ensure specs are loaded (from disk or API)."""
        if self._loaded:
            return

        if self._load_from_disk():
            return

        if self._config is None:
            raise ExchangeError("an instrument cache without a config cannot fetch specs")
        _log.info("fetching instrument specs from Bybit API...")
        async with BybitInstrumentsClient(self._config) as client:
            specs = await client.fetch_all_linear_perpetuals()
            for spec in specs:
                self._cache[spec.symbol] = spec
            self.api_base_url = client.base_url
            self._loaded = True
            self._save_to_disk()

    def get_spec(self, symbol: str, market: str = "linear") -> InstrumentSpec | None:
        """Get spec for a symbol (e.g., 'BTCUSDT' or 'BTC/USDT') on a market."""
        # Normalize: remove slash if present
        normalized = symbol.replace("/", "")
        return self._specs(market).get(normalized)

    def is_tradable(self, symbol: str, market: str = "linear", at_ms: int | None = None) -> bool:
        """Whether the instrument trades on the market, at ``at_ms`` when given.

        A linear instrument must be a perpetual. An instrument is not admitted
        before its launch time; one without a known launch time - spot, or a
        spec from an old cache file - is admitted as before.
        """
        spec = self.get_spec(symbol, market)
        if spec is None or spec.status != "Trading":
            return False
        if market == "linear" and spec.contract_type != "LinearPerpetual":
            return False
        return at_ms is None or spec.launch_time_ms is None or spec.launch_time_ms <= at_ms

    def is_tradable_linear_perpetual(self, symbol: str, at_ms: int | None = None) -> bool:
        """Check if symbol is a tradable LinearPerpetual (launched by ``at_ms``, when given)."""
        return self.is_tradable(symbol, "linear", at_ms)

    def get_tradable_symbols(self) -> list[str]:
        """Get all tradable LinearPerpetual symbols."""
        return [
            spec.symbol for spec in self._cache.values()
            if spec.contract_type == "LinearPerpetual" and spec.status == "Trading"
        ]

    def round_qty_down(self, symbol: str, qty: float, market: str = "linear") -> float:
        """Round quantity DOWN to qtyStep."""
        spec = self.get_spec(symbol, market)
        if not spec:
            return qty
        if qty <= 0:
            return 0.0
        step = spec.qty_step
        if step <= 0:
            return qty
        # Use Decimal for precise rounding down
        from decimal import ROUND_DOWN, Decimal
        d_qty = Decimal(str(qty))
        d_step = Decimal(str(step))
        result = (d_qty / d_step).to_integral_value(rounding=ROUND_DOWN) * d_step
        return float(result)

    def check_min_notional(self, symbol: str, qty: float, price: float, market: str = "linear") -> bool:
        """Check if qty * price meets minNotionalValue."""
        spec = self.get_spec(symbol, market)
        if not spec:
            return True  # fail open if no spec (shouldn't happen if universe filtered)
        notional = qty * price
        return notional >= spec.min_notional_value - 1e-9  # small epsilon


def write_snapshot(
    path: str | Path, specs: Iterable[InstrumentSpec], api_base_url: str, fetched_at_ms: int,
) -> str:
    """Write a versioned snapshot of instrument specs; return its sha256.

    The file records where and when the specs were fetched, and an existing
    file is never overwritten: a snapshot is pinned by its hash.
    """
    by_market: dict[str, dict[str, Any]] = {market: {} for market in MARKETS}
    for spec in specs:
        by_market[spec.market][spec.symbol] = asdict(spec)
    payload = {
        "format": SNAPSHOT_FORMAT,
        "source": api_base_url,
        "fetched_at_ms": fetched_at_ms,
        "specs": by_market,
    }
    raw = json.dumps(payload, sort_keys=True, indent=1).encode("utf-8")
    with open(path, "xb") as f:
        f.write(raw)
    return hashlib.sha256(raw).hexdigest()


async def build_instrument_cache(config: Config) -> InstrumentCache:
    """Build and load instrument cache (one-time at startup)."""
    cache = InstrumentCache(config)
    await cache.ensure_loaded()
    return cache
