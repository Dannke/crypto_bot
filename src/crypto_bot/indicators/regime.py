"""Market regime indicators.

Provides pure functions for classifying market regime on a reference series.
Used by FeatureBuilder to add regime_trend_strength and regime_vol_percentile
to FeatureSet without introducing a separate pipeline.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .adx import adx
from .atr import atr_pct


def regime_trend_strength(
    high: pd.Series | np.ndarray,
    low: pd.Series | np.ndarray,
    close: pd.Series | np.ndarray,
    period: int = 14,
    threshold: float = 25.0,
) -> pd.Series:
    """Compute trend strength signal based on ADX.

    Returns a series in [-1, 1] where:
    - Positive values = trending regime (ADX > threshold)
    - Negative values = ranging regime (ADX <= threshold)
    - Magnitude = distance from threshold, normalized to [0, 1]

    This is a continuous signal, not a hard classification. The hard
    classification (4 regimes) is done in classify_regime() using this
    plus the volatility percentile.
    """
    if period < 1:
        raise ValueError("period must be >= 1")
    if threshold < 0:
        raise ValueError("threshold must be >= 0")

    # Convert to numpy for computation (ADX expects numpy arrays)
    high_arr = np.asarray(high)
    low_arr = np.asarray(low)
    close_arr = np.asarray(close)

    if len(close_arr) < 2 * period + 1:
        return pd.Series(np.full(len(close_arr), np.nan))

    # Use existing ADX function (takes numpy arrays)
    adx_series = adx(high_arr, low_arr, close_arr, period)
    adx_vals = adx_series.values

    # Normalize: (ADX - threshold) / threshold, clipped to [-1, 1]
    # This gives ~0 at threshold, +1 when ADX is 2*threshold, -1 when ADX=0
    normalized = np.clip((adx_vals - threshold) / threshold, -1.0, 1.0)

    return pd.Series(normalized, index=adx_series.index)


def rolling_vol_percentile(
    close: pd.Series | np.ndarray,
    lookback_bars: int = 168,
    period: int = 14,
) -> pd.Series:
    """Compute rolling percentile of ATR% (realized volatility proxy).

    Returns a series in [0, 1] representing the percentile rank of current
    ATR% within the trailing `lookback_bars` window.

    - 0.0 = lowest volatility in lookback window
    - 1.0 = highest volatility in lookback window
    - 0.5 = median

    Uses ATR% as the volatility measure (already normalized by price).
    """
    if period < 1:
        raise ValueError("period must be >= 1")
    if lookback_bars < period * 2:
        raise ValueError("lookback_bars must be >= 2 * period")

    c = close.values if isinstance(close, pd.Series) else np.asarray(close)

    if len(c) < lookback_bars + period + 1:
        return pd.Series(np.full(len(c), np.nan))

    # Compute ATR% series
    # We need high/low for ATR; approximate with close-to-close volatility
    # as a proxy when high/low not available. For regime classification,
    # we use the existing atr_pct which requires high/low.
    # This function expects the caller to provide ATR% or we compute from close.
    raise NotImplementedError(
        "Use rolling_atr_percentile which takes high/low/close for proper ATR%"
    )


def rolling_atr_percentile(
    high: pd.Series | np.ndarray,
    low: pd.Series | np.ndarray,
    close: pd.Series | np.ndarray,
    lookback_bars: int = 168,
    period: int = 14,
) -> pd.Series:
    """Compute rolling percentile of ATR% over lookback window.

    Returns a series in [0, 1] representing the percentile rank of current
    ATR% within the trailing `lookback_bars` window.

    ATR% is computed using the existing atr_pct function (ATR/close * 100).
    """
    if period < 1:
        raise ValueError("period must be >= 1")
    if lookback_bars < period * 2:
        raise ValueError("lookback_bars must be >= 2 * period")

    c = np.asarray(close)

    if len(c) < lookback_bars + period + 1:
        return pd.Series(np.full(len(c), np.nan))

    # Compute ATR% using existing function (atr_pct handles conversion internally)
    atr_pct_series = atr_pct(high, low, close, period)
    atr_pct_vals = atr_pct_series.values

    # Rolling percentile: for each point, compute rank within lookback window
    percentiles = np.full(len(atr_pct_vals), np.nan)

    for i in range(len(atr_pct_vals)):
        start = max(0, i - lookback_bars + 1)
        window = atr_pct_vals[start:i+1]
        # Remove NaN values
        valid = window[~np.isnan(window)]
        if len(valid) < 10:  # Minimum sample size for meaningful percentile
            percentiles[i] = np.nan
        else:
            current = atr_pct_vals[i]
            if np.isnan(current):
                percentiles[i] = np.nan
            else:
                # Percentile rank: fraction of values <= current
                percentiles[i] = np.sum(valid <= current) / len(valid)

    return pd.Series(percentiles, index=atr_pct_series.index)


def classify_regime_from_signals(
    trend_strength: float,
    vol_percentile: float,
    vol_threshold: float = 0.75,
) -> str:
    """Classify regime from continuous signals into 4-regime taxonomy.

    Args:
        trend_strength: Output from regime_trend_strength ([-1, 1])
        vol_percentile: Output from rolling_atr_percentile ([0, 1])
        vol_threshold: Percentile threshold for high vs low vol (default 0.75)

    Returns:
        One of: "trend_low_vol", "trend_high_vol", "range_low_vol", "range_high_vol"
    """
    if np.isnan(trend_strength) or np.isnan(vol_percentile):
        return "range_low_vol"  # Default/fallback

    is_trend = trend_strength > 0  # ADX > threshold
    is_high_vol = vol_percentile > vol_threshold

    if is_trend:
        if is_high_vol:
            return "trend_high_vol"
        return "trend_low_vol"
    else:
        if is_high_vol:
            return "range_high_vol"
        return "range_low_vol"