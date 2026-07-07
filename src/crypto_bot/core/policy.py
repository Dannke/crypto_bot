"""Business policy: hard limits, defaults and reference tables.

Everything here is *runtime policy* — not the shape of a config file. It is the
single source of truth for things like "which timeframes are legal", "which
quote currencies we support", "is live trading released yet".

The two most important knobs:

``LIVE_TRADING_RELEASED``
    Master switch. Leave ``False`` until paper trading and the test suite are
    validated. Flipping it is the ONE action that unlocks live order placement.

``ENABLE_*``
    Read from environment; cannot be relaxed from YAML.
"""
from __future__ import annotations

# =========================================================================== #
# Live-trading release gate
# =========================================================================== #
# Flip to True ONLY after paper trading and tests are validated.
LIVE_TRADING_RELEASED: bool = False

# =========================================================================== #
# Timeframes
# =========================================================================== #
# ccxt/Bitfinex-style ids we accept in config. Order is not significant here;
# validators check ascending granularity separately.
ALLOWED_TIMEFRAMES: tuple[str, ...] = (
    "1m", "3m", "5m", "15m", "30m",
    "1h", "2h", "4h", "6h", "8h", "12h",
    "1d", "3d", "1w", "1M",
)

# Minutes per timeframe id, used to enforce ascending granularity
# (trigger -> confirmation -> trend) and for general sanity checks.
_TIMEFRAME_MINUTES: dict[str, int] = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "2h": 120, "4h": 240, "6h": 360, "8h": 480, "12h": 720,
    "1d": 1440, "3d": 4320, "1w": 10080, "1M": 43200,
}


def timeframe_to_minutes(tf: str) -> int:
    """Return the length of a timeframe in minutes, or raise ``KeyError``."""
    return _TIMEFRAME_MINUTES[tf]


def timeframe_to_seconds(tf: str) -> int:
    """Return the length of a timeframe in seconds."""
    return _TIMEFRAME_MINUTES[tf] * 60


def is_timeframe_allowed(tf: str) -> bool:
    return tf in ALLOWED_TIMEFRAMES


def is_bar_closed(candle_open_ts_ms: int, timeframe: str, as_of_ms: int) -> bool:
    """Bar is closed if its full period has elapsed by ``as_of_ms``.

    Used as the single source of truth for bar-boundary checks in both
    live (``as_of_ms = now``) and backtest (``as_of_ms = simulated time``)
    paths so they cannot diverge in behaviour.
    """
    period_ms = timeframe_to_seconds(timeframe) * 1000
    return candle_open_ts_ms + period_ms <= as_of_ms


# =========================================================================== #
# Universe / symbols
# =========================================================================== #
SUPPORTED_QUOTE_CURRENCIES: tuple[str, ...] = ("USDT", "USD", "BTC", "ETH", "EUR")

# Stablecoins treated as non-tradable base assets (they are the quote side or
# pegged to it). Used when ``exclude_stablecoins`` is enabled.
STABLECOIN_BLACKLIST: frozenset[str] = frozenset({
    "USDT", "USDC", "BUSD", "DAI", "TUSD", "FDUSD", "USDP", "PAX",
    "SUSD", "GUSD", "USDD", "USTC", "EURT", "USDS",
})

# Leveraged / structured tokens (e.g. BTCUP, ETHBULL, BTC3L, ETH5S).
LEVERAGED_TOKEN_SUFFIXES: tuple[str, ...] = (
    "UP", "DOWN", "BULL", "BEAR", "3L", "3S", "5L", "5S",
)


def is_stablecoin(base: str) -> bool:
    return base.upper() in STABLECOIN_BLACKLIST


def is_leveraged_token(base: str) -> bool:
    b = base.upper()
    return any(b.endswith(suf) for suf in LEVERAGED_TOKEN_SUFFIXES)


# =========================================================================== #
# Data / runtime limits
# =========================================================================== #
# Hard cap on requested candle history per timeframe. Guards against accidental
# multi-thousand-bar fetches that would hammer the exchange.
MAX_CANDLES_LOOKBACK: int = 1000

# Hard cap on automatic universe discovery. This is intentionally separate
# from candle history: it limits how wide a scan may become in one cycle.
MAX_AUTO_DISCOVER_SYMBOLS: int = 300

# Retry behaviour for transient exchange/network errors (manual implementation,
# no extra dependency).
DEFAULT_RETRY_ATTEMPTS: int = 3
RETRY_BACKOFF_BASE_SECONDS: float = 0.5
RETRY_BACKOFF_MAX_SECONDS: float = 8.0

# Schema version stamped into ``schema_meta`` on migration.
SCHEMA_VERSION: str = "1"

# Sentinel currency string for "no equity recorded yet".
EQUITY_NOT_AVAILABLE: float = float("nan")
