"""Average Directional Index (Wilder) — trend strength, not direction.

ADX > ~25 is generally considered a trending regime; below ~20 is choppy. The
signal engine uses it to suppress signals in non-trending markets.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ._numpy import ewm_mean


def _high_low_close_np(
    high, low=None, close=None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Accept either (high, low, close) or a DataFrame with those columns."""
    if hasattr(high, "columns") and "high" in high:
        return (
            np.asarray(high["high"], dtype="float64"),
            np.asarray(high["low"], dtype="float64"),
            np.asarray(high["close"], dtype="float64"),
        )
    if low is None or close is None:
        raise TypeError("pass either a DataFrame or (high, low, close)")
    return (
        np.asarray(high, dtype="float64"),
        np.asarray(low, dtype="float64"),
        np.asarray(close, dtype="float64"),
    )


def true_range(
    high: pd.DataFrame | pd.Series | np.ndarray,
    low: pd.Series | np.ndarray | None = None,
    close: pd.Series | np.ndarray | None = None,
) -> pd.Series:
    """True Range series. The first bar has only H-L (no previous close)."""
    return pd.Series(true_range_np(high, low, close))


def true_range_np(high, low=None, close=None) -> np.ndarray:
    h, low_s, c = _high_low_close_np(high, low, close)
    prev_close = np.concatenate(([np.nan], c[:-1]))
    return np.fmax.reduce(
        [np.abs(h - low_s), np.abs(h - prev_close), np.abs(low_s - prev_close)]
    )


def atr(
    high: pd.DataFrame | pd.Series | np.ndarray,
    low: pd.Series | np.ndarray | None = None,
    close: pd.Series | np.ndarray | None = None,
    period: int = 14,
) -> pd.Series:
    """Wilder-smoothed ATR series."""
    if period < 1:
        raise ValueError("ATR period must be >= 1")
    return pd.Series(atr_np(high, low, close, period))


def atr_np(high, low=None, close=None, period: int = 14) -> np.ndarray:
    tr = true_range_np(high, low, close)
    return ewm_mean(tr, 1.0 / period, period)


def atr_pct(
    high: pd.DataFrame | pd.Series | np.ndarray,
    low: pd.Series | np.ndarray | None = None,
    close: pd.Series | np.ndarray | None = None,
    period: int = 14,
) -> pd.Series:
    """ATR as a percentage of close (x100), handy for the volatility gate."""
    return pd.Series(atr_pct_np(high, low, close, period))


def atr_pct_np(high, low=None, close=None, period: int = 14) -> np.ndarray:
    a = atr_np(high, low, close, period)
    c = _high_low_close_np(high, low, close)[2]
    return (a / c) * 100.0


def last_atr_pct(
    high: pd.DataFrame | pd.Series | np.ndarray,
    low: pd.Series | np.ndarray | None = None,
    close: pd.Series | np.ndarray | None = None,
    period: int = 14,
) -> float:
    """Convenience: latest ATR%, or NaN if not enough data."""
    series = atr_pct(high, low, close, period)
    if series.empty:
        return float("nan")
    return float(series.iloc[-1])