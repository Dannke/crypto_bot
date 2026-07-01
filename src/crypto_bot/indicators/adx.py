"""Average Directional Index (Wilder) — trend strength, not direction.

ADX > ~25 is generally considered a trending regime; below ~20 is choppy. The
signal engine uses it to suppress signals in non-trending markets.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .atr import _high_low_close


def adx(
    high: pd.DataFrame | pd.Series | np.ndarray,
    low: pd.Series | np.ndarray | None = None,
    close: pd.Series | np.ndarray | None = None,
    period: int = 14,
) -> pd.Series:
    """ADX series (0..100), NaN for the warmup bars.

    Classic Wilder implementation: smoothed +DM/-DM, DI+/DI-, then ADX as the
    Wilder EMA of |DI+ - DI-| / (DI+ + DI-).
    """
    if period < 1:
        raise ValueError("ADX period must be >= 1")
    h, low_s, c = _high_low_close(high, low, close)
    if len(c) < 2 * period + 1:
        return pd.Series(np.full(len(c), np.nan))

    up = h.diff()
    down = -low_s.diff()
    # +DM: up move exceeds down move AND is positive; else 0
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), dtype="float64")
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), dtype="float64")

    tr = (h - low_s).abs()
    prev_close = c.shift(1)
    tr = pd.concat(
        [tr, (h - prev_close).abs(), (low_s - prev_close).abs()], axis=1
    ).max(axis=1)

    atr_ = tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    plus_di = 100.0 * (
        plus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr_
    )
    minus_di = 100.0 * (
        minus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr_
    )
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    adx_series = dx.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    return adx_series


def last_adx(
    high: pd.DataFrame | pd.Series | np.ndarray,
    low: pd.Series | np.ndarray | None = None,
    close: pd.Series | np.ndarray | None = None,
    period: int = 14,
) -> float:
    series = adx(high, low, close, period)
    if series.empty:
        return float("nan")
    return float(series.iloc[-1])
