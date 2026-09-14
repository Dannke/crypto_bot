"""Bybit instruments info client and cache for R0.4.

Provides per-symbol instrument specifications:
- qtyStep (lot size step)
- minOrderQty (minimum order quantity)
- minNotionalValue (minimum order notional)
- tickSize (price step)
- maxLeverage (maximum leverage)

Used for universe filtering (fail closed) and position sizing (rounding + renormalization).
"""
from __future__ import annotations

import json
import time
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


@dataclass(frozen=True, slots=True)
class InstrumentSpec:
    """Instrument specification from Bybit instruments-info."""
    symbol: str                    # e.g., "BTCUSDT"
    qty_step: float               # lotSizeFilter.qtyStep
    min_order_qty: float          # lotSizeFilter.minOrderQty
    min_notional_value: float     # lotSizeFilter.minNotionalValue
    tick_size: float              # priceFilter.tickSize
    max_leverage: int             # leverageFilter.maxLeverage
    status: str                   # Trading, PreLaunch, etc.
    contract_type: str            # LinearPerpetual, etc.

    def __post_init__(self) -> None:
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

    async def fetch_all_linear_perpetuals(self) -> list[InstrumentSpec]:
        """Fetch all linear perpetual instruments from Bybit."""
        if self._closed:
            raise ExchangeError("client is closed")

        all_items: list[dict[str, Any]] = []
        cursor: str | None = None

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
            while True:
                params: dict[str, Any] = {"category": "linear", "limit": 1000}
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
                spec = self._parse_item(item)
                specs.append(spec)
            except Exception as exc:
                _log.warning("failed to parse instrument %s: %s", item.get("symbol", "?"), exc)

        return specs

    def _parse_item(self, item: dict[str, Any]) -> InstrumentSpec:
        """Parse a single instrument item from Bybit response."""
        symbol = item["symbol"]

        # lotSizeFilter
        lot = item.get("lotSizeFilter", {})
        qty_step = float(lot.get("qtyStep", "0"))
        min_order_qty = float(lot.get("minOrderQty", "0"))
        min_notional = float(lot.get("minNotionalValue", "0"))

        # priceFilter
        price = item.get("priceFilter", {})
        tick_size = float(price.get("tickSize", "0"))

        # leverageFilter
        lev = item.get("leverageFilter", {})
        max_leverage = int(float(lev.get("maxLeverage", "1")))

        status = item.get("status", "")
        contract_type = item.get("contractType", "")

        return InstrumentSpec(
            symbol=symbol,
            qty_step=qty_step,
            min_order_qty=min_order_qty,
            min_notional_value=min_notional,
            tick_size=tick_size,
            max_leverage=max_leverage,
            status=status,
            contract_type=contract_type,
        )


class InstrumentCache:
    """Disk-cached instrument specifications with TTL."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._cache: dict[str, InstrumentSpec] = {}
        self._loaded = False

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

        _log.info("fetching instrument specs from Bybit API...")
        async with BybitInstrumentsClient(self._config) as client:
            specs = await client.fetch_all_linear_perpetuals()
            for spec in specs:
                self._cache[spec.symbol] = spec
            self._loaded = True
            self._save_to_disk()

    def get_spec(self, symbol: str) -> InstrumentSpec | None:
        """Get spec for a symbol (e.g., 'BTCUSDT' or 'BTC/USDT')."""
        # Normalize: remove slash if present
        normalized = symbol.replace("/", "")
        return self._cache.get(normalized)

    def is_tradable_linear_perpetual(self, symbol: str) -> bool:
        """Check if symbol is a tradable LinearPerpetual."""
        spec = self.get_spec(symbol)
        if not spec:
            return False
        return spec.contract_type == "LinearPerpetual" and spec.status == "Trading"

    def get_tradable_symbols(self) -> list[str]:
        """Get all tradable LinearPerpetual symbols."""
        return [
            spec.symbol for spec in self._cache.values()
            if spec.contract_type == "LinearPerpetual" and spec.status == "Trading"
        ]

    def round_qty_down(self, symbol: str, qty: float) -> float:
        """Round quantity DOWN to qtyStep."""
        spec = self.get_spec(symbol)
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

    def check_min_notional(self, symbol: str, qty: float, price: float) -> bool:
        """Check if qty * price meets minNotionalValue."""
        spec = self.get_spec(symbol)
        if not spec:
            return True  # fail open if no spec (shouldn't happen if universe filtered)
        notional = qty * price
        return notional >= spec.min_notional_value - 1e-9  # small epsilon


async def build_instrument_cache(config: Config) -> InstrumentCache:
    """Build and load instrument cache (one-time at startup)."""
    cache = InstrumentCache(config)
    await cache.ensure_loaded()
    return cache