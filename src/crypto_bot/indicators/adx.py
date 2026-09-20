"""Average Directional Index (Wilder) — trend strength, not direction.

ADX > ~25 is generally considered a trending regime; below ~20 is choppy. The
signal engine uses it to suppress signals in non-trending markets.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ._numpy import ewm_mean
from .atr import _high_low_close_np, true_range_np


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
    h, low_s, c = _high_low_close_np(high, low, close)
    if len(c) < 2 * period + 1:
        return pd.Series(np.full(len(c), np.nan))

    up = np.diff(h, prepend=np.nan)
    down = -np.diff(low_s, prepend=np.nan)
    # +DM: up move exceeds down move AND is positive; else 0
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)

    tr = true_range_np(h, low_s, c)

    alpha = 1.0 / period
    atr_ = ewm_mean(tr, alpha, period)
    p_dm_ema = ewm_mean(plus_dm, alpha, period)
    m_dm_ema = ewm_mean(minus_dm, alpha, period)
    plus_di = 100.0 * p_dm_ema / atr_
    minus_di = 100.0 * m_dm_ema / atr_
    di_sum = plus_di + minus_di
    # np.where вычисляет обе ветки целиком, поэтому деление выполнялось и при
    # di_sum == 0 — отсюда RuntimeWarning на каждом таком баре. np.divide с
    # where= считает только там, где знаменатель ненулевой; остальное остаётся NaN.
    dx = np.full(di_sum.shape, np.nan, dtype=float)
    np.divide(100.0 * np.abs(plus_di - minus_di), di_sum, out=dx, where=di_sum != 0.0)
    adx_series = ewm_mean(dx, alpha, period)
    return pd.Series(adx_series)


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