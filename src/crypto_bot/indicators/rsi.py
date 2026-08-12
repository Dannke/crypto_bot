"""Relative Strength Index (Wilder's smoothing)."""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from ._numpy import ewm_mean


def rsi(closes: pd.Series | np.ndarray[Any, Any], period: int = 14) -> pd.Series:
    """RSI as a Series in [0, 100], NaN for the first ``period`` bars.

    Uses Wilder's smoothing (EMA of gains/losses with alpha = 1/period), the
    canonical RSI definition, matching most trading platforms.
    """
    if period < 1:
        raise ValueError("RSI period must be >= 1")
    s = np.asarray(closes, dtype="float64")
    if len(s) <= period:
        return pd.Series(np.full(len(s), np.nan))
    delta = np.diff(s)
    nan_delta = np.isnan(delta)
    gain = np.where((delta > 0.0) & ~nan_delta, delta, 0.0)
    loss = np.where((delta < 0.0) & ~nan_delta, -delta, 0.0)
    # pandas diff/clip keeps NaN where the input had NaN (its clip(lower=0)
    # does not touch NaN); replicate that: NaN deltas stay NaN.
    gain = np.where(nan_delta, np.nan, gain)
    loss = np.where(nan_delta, np.nan, loss)
    # First bar has no delta: pandas diff() yields NaN there, which the ewm
    # skips without consuming the observation count.
    gain = np.concatenate(([np.nan], gain))
    loss = np.concatenate(([np.nan], loss))
    # Wilder smoothing == ewm with alpha = 1/period (span = 2*period - 1).
    alpha = 1.0 / period
    avg_gain = ewm_mean(gain, alpha, period)
    avg_loss = ewm_mean(loss, alpha, period)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = np.where(avg_loss == 0.0, np.nan, avg_gain / avg_loss)
    out = 100.0 - (100.0 / (1.0 + rs))
    # Edge cases (after warmup, where both averages are defined):
    #   avg_loss == 0 & avg_gain  > 0  -> relentless up move   -> RSI = 100
    #   avg_gain == 0 & avg_loss  > 0  -> relentless down move -> RSI = 0
    #   avg_gain == 0 & avg_loss == 0  -> no movement at all   -> RSI = 50
    out = np.where((avg_loss == 0.0) & ~np.isnan(avg_gain) & (avg_gain > 0), 100.0, out)
    out = np.where((avg_gain == 0.0) & ~np.isnan(avg_loss) & (avg_loss > 0), 0.0, out)
    out = np.where(
        (avg_gain == 0.0) & (avg_loss == 0.0) & ~np.isnan(avg_gain), 50.0, out
    )
    return pd.Series(out)