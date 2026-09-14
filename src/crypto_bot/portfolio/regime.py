"""Market regime classification for portfolio layer (R2).

Provides deterministic, look-ahead-free regime classification using
the same anchoring discipline as get_market_snapshot/get_universe_snapshot.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

from ..config.schemas import RegimeConfig
from ..core.policy import timeframe_to_seconds
from ..core.types import Candle
from ..indicators.regime import (
    classify_regime_from_signals,
    regime_trend_strength,
    rolling_atr_percentile,
)
from ..portfolio.models import RegimeSnapshot


@dataclass(frozen=True, slots=True)
class RegimeSnapshotDTO:
    """Internal DTO for regime classification result (before validation)."""

    as_of_ms: int
    regime: str
    trend_strength: float
    vol_percentile: float
    reference_universe: tuple[str, ...]


def _arrays(candles: list[Candle]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Extract OHLC columns as float64 arrays."""
    n = len(candles)
    high_a = np.empty(n, dtype="float64")
    low_a = np.empty(n, dtype="float64")
    close_a = np.empty(n, dtype="float64")
    for i, c in enumerate(candles):
        high_a[i] = c.high
        low_a[i] = c.low
        close_a[i] = c.close
    return high_a, low_a, close_a


def _slice_closed_bars(
    candles: list[Candle],
    as_of_ms: int,
    timeframe: str,
) -> list[Candle]:
    """Return only fully closed bars at or before as_of_ms.

    A bar is closed if its close time (timestamp + period_ms) <= as_of_ms.
    """

    period_ms = timeframe_to_seconds(timeframe) * 1000
    return [
        c for c in candles
        if c.timestamp + period_ms <= as_of_ms
    ]


def _get_reference_candles(
    candles_by_symbol: Mapping[str, Sequence[Candle]],
    reference: str,
    quote: str,
) -> list[Candle] | None:
    """Get candles for reference symbol (BTC or basket).

    Returns None if not available.
    """
    if reference == "btc_only":
        symbol = f"BTC/{quote}"
        return list(candles_by_symbol.get(symbol, []))
    # universe_basket: use the first symbol that has data (fallback)
    for _sym, candles in candles_by_symbol.items():
        if candles:
            return list(candles)
    return None


def classify_regime(
    candles_by_symbol: Mapping[str, Sequence[Candle]],
    as_of_ms: int,
    config: RegimeConfig,
    *,
    quote: str = "USDT",
    timeframe: str = "1h",
) -> RegimeSnapshot:
    """Classify market regime at a specific point in time.

    Uses the same anchoring discipline as get_market_snapshot:
    - Single as_of_ms anchor across all symbols
    - Only fully closed bars (open + period <= as_of_ms)
    - Single timeframe (the configured regime timeframe)

    Args:
        candles_by_symbol: Mapping of symbol -> list of candles (ascending time)
        as_of_ms: Anchor timestamp (ms epoch) - only bars with close <= as_of_ms visible
        config: RegimeConfig with classification parameters
        quote: Quote currency for reference symbol construction
        timeframe: Timeframe to use for regime classification

    Returns:
        RegimeSnapshot with regime classification and metrics

    Raises:
        ValueError: If insufficient data for classification
    """
    if not config.enabled:
        # Return neutral regime if disabled
        return RegimeSnapshot(
            as_of_ms=as_of_ms,
            regime="range_low_vol",
            trend_strength=0.0,
            vol_percentile=0.5,
            reference_universe=tuple(),
        )

    # Slice to closed bars only at this anchor
    closed_by_symbol: dict[str, list[Candle]] = {}
    for sym, candles in candles_by_symbol.items():
        closed = _slice_closed_bars(list(candles), as_of_ms, timeframe)
        if closed:
            closed_by_symbol[sym] = closed

    if not closed_by_symbol:
        raise ValueError("No closed bars available for regime classification")

    # Get reference candles for regime calculation
    ref_candles = _get_reference_candles(closed_by_symbol, config.reference, quote)
    if not ref_candles or len(ref_candles) < config.trend_period + 2:
        raise ValueError(f"Insufficient reference data for regime classification (need >{config.trend_period} bars)")

    # Compute regime indicators on reference series
    high, low, close = _arrays(ref_candles)

    # Trend strength from ADX
    trend_strength_series = regime_trend_strength(
        high, low, close,
        period=config.trend_period,
        threshold=config.trend_threshold,
    )
    if len(trend_strength_series) == 0 or pd.isna(trend_strength_series.iloc[-1]):
        raise ValueError("Could not compute trend strength (insufficient data or all NaN)")
    trend_strength = float(trend_strength_series.iloc[-1])

    # Volatility percentile from rolling ATR%
    if len(close) < config.vol_lookback_bars + config.trend_period:
        raise ValueError(f"Insufficient data for vol percentile (need {config.vol_lookback_bars + config.trend_period} bars)")
    vol_percentile_series = rolling_atr_percentile(
        high, low, close,
        lookback_bars=config.vol_lookback_bars,
        period=config.trend_period,
    )
    if len(vol_percentile_series) == 0 or pd.isna(vol_percentile_series.iloc[-1]):
        raise ValueError("Could not compute vol percentile (insufficient data or all NaN)")
    vol_percentile = float(vol_percentile_series.iloc[-1])

    # Classify regime
    regime = classify_regime_from_signals(
        trend_strength,
        vol_percentile,
        vol_threshold=config.vol_percentile_high,
    )

    # Build reference universe list
    reference_universe = tuple(closed_by_symbol.keys())

    return RegimeSnapshot(
        as_of_ms=as_of_ms,
        regime=regime,
        trend_strength=trend_strength,
        vol_percentile=vol_percentile,
        reference_universe=reference_universe,
    )