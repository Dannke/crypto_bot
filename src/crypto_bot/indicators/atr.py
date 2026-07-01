"""Average True Range (Wilder) and ATR as a percentage of price.

ATR% is the volatility gate: coins below ``atr_min_pct`` are too dead to trade,
those above ``atr_max_pct`` are too explosive. Returning both absolute and
percentage forms avoids recomputing close prices in callers.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def _high_low_close(
    df_or_high: pd.DataFrame | pd.Series | np.ndarray[Any, Any],
    low: pd.Series | np.ndarray[Any, Any] | None = None,
    close: pd.Series | np.ndarray[Any, Any] | None = None,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Accept either (high, low, close) or a DataFrame with those columns."""
    if isinstance(df_or_high, pd.DataFrame):
        return (
            pd.Series(df_or_high["high"], dtype="float64").reset_index(drop=True),
            pd.Series(df_or_high["low"], dtype="float64").reset_index(drop=True),
            pd.Series(df_or_high["close"], dtype="float64").reset_index(drop=True),
        )
    if low is None or close is None:
        raise TypeError("pass either a DataFrame or (high, low, close)")
    return (
        pd.Series(df_or_high, dtype="float64").reset_index(drop=True),
        pd.Series(low, dtype="float64").reset_index(drop=True),
        pd.Series(close, dtype="float64").reset_index(drop=True),
    )


def true_range(
    high: pd.DataFrame | pd.Series | np.ndarray[Any, Any],
    low: pd.Series | np.ndarray[Any, Any] | None = None,
    close: pd.Series | np.ndarray[Any, Any] | None = None,
) -> pd.Series:
    """True Range series. The first bar has only H-L (no previous close)."""
    h, low_s, c = _high_low_close(high, low, close)
    prev_close = c.shift(1)
    tr = pd.concat(
        [(h - low_s).abs(), (h - prev_close).abs(), (low_s - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr


def atr(
    high: pd.DataFrame | pd.Series | np.ndarray[Any, Any],
    low: pd.Series | np.ndarray[Any, Any] | None = None,
    close: pd.Series | np.ndarray[Any, Any] | None = None,
    period: int = 14,
) -> pd.Series:
    """Wilder-smoothed ATR series."""
    if period < 1:
        raise ValueError("ATR period must be >= 1")
    tr = true_range(high, low, close)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def atr_pct(
    high: pd.DataFrame | pd.Series | np.ndarray[Any, Any],
    low: pd.Series | np.ndarray[Any, Any] | None = None,
    close: pd.Series | np.ndarray[Any, Any] | None = None,
    period: int = 14,
) -> pd.Series:
    """ATR as a percentage of close (x100), handy for the volatility gate."""
    a = atr(high, low, close, period)
    c = _high_low_close(high, low, close)[2]
    return (a / c) * 100.0


def last_atr_pct(
    high: pd.DataFrame | pd.Series | np.ndarray[Any, Any],
    low: pd.Series | np.ndarray[Any, Any] | None = None,
    close: pd.Series | np.ndarray[Any, Any] | None = None,
    period: int = 14,
) -> float:
    """Convenience: latest ATR%, or NaN if not enough data."""
    series = atr_pct(high, low, close, period)
    if series.empty:
        return float("nan")
    return float(series.iloc[-1])
