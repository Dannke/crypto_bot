"""Bollinger Bands.

Used as an optional mean-reversion / overextension check: a close outside the
bands flags a stretched move. Returns upper/lower/midband so callers can derive
a position-in-band score without recomputing the SMA/std.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from ._numpy import rolling_mean, rolling_std


@dataclass(frozen=True, slots=True)
class BollingerResult:
    upper: pd.Series
    mid: pd.Series
    lower: pd.Series

    def last(self) -> tuple[float, float, float]:
        """Latest (upper, mid, lower); NaN if undefined."""
        return (
            float(self.upper.iloc[-1]) if len(self.upper) else float("nan"),
            float(self.mid.iloc[-1]) if len(self.mid) else float("nan"),
            float(self.lower.iloc[-1]) if len(self.lower) else float("nan"),
        )


def bollinger_bands(
    closes: pd.Series | np.ndarray[Any, Any], period: int = 20, std: float = 2.0
) -> BollingerResult:
    if period < 1:
        raise ValueError("Bollinger period must be >= 1")
    if std <= 0:
        raise ValueError("Bollinger std must be > 0")
    s = np.asarray(closes, dtype="float64")
    mid = rolling_mean(s, period)
    sigma = rolling_std(s, period)
    upper = mid + std * sigma
    lower = mid - std * sigma
    return BollingerResult(
        upper=pd.Series(upper), mid=pd.Series(mid), lower=pd.Series(lower)
    )


def bollinger_position(
    closes: pd.Series | np.ndarray[Any, Any], period: int = 20, std: float = 2.0
) -> pd.Series:
    """Position of close within the bands, normalised to ~[0, 1].

    0.5 == at the midband; >0.5 above mid; <0.5 below mid. Values can fall
    outside [0,1] when price exceeds the bands.
    """
    bb = bollinger_bands(closes, period, std)
    width = bb.upper - bb.lower
    return (pd.Series(closes, dtype="float64").reset_index(drop=True) - bb.lower) / width.replace(
        0.0, np.nan
    )


def bollinger_position_np(
    closes: np.ndarray, period: int = 20, std: float = 2.0
) -> np.ndarray:
    """Numpy variant returning the raw position array (backtest hot path)."""
    if period < 1:
        raise ValueError("Bollinger period must be >= 1")
    if std <= 0:
        raise ValueError("Bollinger std must be > 0")
    mid = rolling_mean(closes, period)
    sigma = rolling_std(closes, period)
    upper = mid + std * sigma
    lower = mid - std * sigma
    width = upper - lower
    with np.errstate(divide="ignore", invalid="ignore"):
        out = (closes - lower) / width
    out[width == 0.0] = np.nan
    return out