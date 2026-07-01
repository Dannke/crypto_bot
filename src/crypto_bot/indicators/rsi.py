"""Relative Strength Index (Wilder's smoothing)."""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def rsi(closes: pd.Series | np.ndarray[Any, Any], period: int = 14) -> pd.Series:
    """RSI as a Series in [0, 100], NaN for the first ``period`` bars.

    Uses Wilder's smoothing (EMA of gains/losses with alpha = 1/period), the
    canonical RSI definition, matching most trading platforms.
    """
    if period < 1:
        raise ValueError("RSI period must be >= 1")
    s = pd.Series(closes, dtype="float64").reset_index(drop=True)
    if len(s) <= period:
        return pd.Series(np.full(len(s), np.nan))
    delta = s.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    # Wilder smoothing == ewm with alpha = 1/period (span = 2*period - 1).
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - (100.0 / (1.0 + rs))
    # Edge cases (after warmup, where both averages are defined):
    #   avg_loss == 0 & avg_gain  > 0  -> relentless up move   -> RSI = 100
    #   avg_gain == 0 & avg_loss  > 0  -> relentless down move -> RSI = 0
    #   avg_gain == 0 & avg_loss == 0  -> no movement at all   -> RSI = 50
    out[avg_loss.eq(0.0) & avg_gain.notna() & (avg_gain > 0)] = 100.0
    out[avg_gain.eq(0.0) & avg_loss.notna() & (avg_loss > 0)] = 0.0
    out[avg_gain.eq(0.0) & avg_loss.eq(0.0) & avg_gain.notna()] = 50.0
    return out
