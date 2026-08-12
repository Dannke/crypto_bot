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

from ._numpy import ewm_mean


def ema(values: pd.Series | np.ndarray[Any, Any], period: int) -> pd.Series:
    """EMA as a pandas Series (NaN warmup of ``period-1`` bars)."""
    if period < 1:
        raise ValueError("EMA period must be >= 1")
    return pd.Series(ewm_mean(np.asarray(values, dtype="float64"), 2.0 / (period + 1), period))


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
    c = np.asarray(closes, dtype="float64")
    if len(c) < slow:
        # Not enough data for the slow EMA to be defined; report MIXED with NaNs.
        return EmaState(EmaStack.MIXED, float("nan"), float("nan"), float("nan"))
    ef = ewm_mean(c, 2.0 / (fast + 1), fast)[-1]
    em = ewm_mean(c, 2.0 / (mid + 1), mid)[-1]
    es = ewm_mean(c, 2.0 / (slow + 1), slow)[-1]
    if np.isnan(ef) or np.isnan(em) or np.isnan(es):
        return EmaState(EmaStack.MIXED, float("nan"), float("nan"), float("nan"))
    if ef > em > es:
        return EmaState(EmaStack.BULL, ef, em, es)
    if ef < em < es:
        return EmaState(EmaStack.BEAR, ef, em, es)
    return EmaState(EmaStack.MIXED, ef, em, es)