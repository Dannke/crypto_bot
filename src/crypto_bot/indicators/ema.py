"""Exponential Moving Average and EMA-stack state.

The EMA stack (fast < mid < slow for an uptrend) is the backbone of the trend
score. ``ema_cross_state`` returns a small, typed verdict so the signal engine
doesn't reimplement comparisons.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import numpy as np
import pandas as pd


def ema(values: pd.Series | np.ndarray[Any, Any], period: int) -> pd.Series:
    """EMA as a pandas Series (NaN warmup of ``period-1`` bars)."""
    if period < 1:
        raise ValueError("EMA period must be >= 1")
    s = pd.Series(values, dtype="float64").reset_index(drop=True)
    return s.ewm(span=period, adjust=False, min_periods=period).mean()


class EmaStack(StrEnum):
    BULL = "bull"        # fast > mid > slow
    BEAR = "bear"        # fast < mid < slow
    MIXED = "mixed"      # no clean alignment


@dataclass(frozen=True, slots=True)
class EmaState:
    stack: EmaStack
    fast: float
    mid: float
    slow: float


def ema_cross_state(
    closes: pd.Series | np.ndarray[Any, Any], fast: int, mid: int, slow: int
) -> EmaState:
    """Compute the three EMAs and classify their stacking at the latest bar."""
    if not (fast < mid < slow):
        raise ValueError("require fast < mid < slow")
    c = pd.Series(closes, dtype="float64")
    if len(c) < slow:
        # Not enough data for the slow EMA to be defined; report MIXED with NaNs.
        return EmaState(EmaStack.MIXED, float("nan"), float("nan"), float("nan"))
    ef, em, es = ema(c, fast).iloc[-1], ema(c, mid).iloc[-1], ema(c, slow).iloc[-1]
    if np.isnan(ef) or np.isnan(em) or np.isnan(es):
        return EmaState(EmaStack.MIXED, float("nan"), float("nan"), float("nan"))
    if ef > em > es:
        return EmaState(EmaStack.BULL, ef, em, es)
    if ef < em < es:
        return EmaState(EmaStack.BEAR, ef, em, es)
    return EmaState(EmaStack.MIXED, ef, em, es)
