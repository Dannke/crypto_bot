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
    s = pd.Series(closes, dtype="float64").reset_index(drop=True)
    mid = s.rolling(period, min_periods=period).mean()
    sigma = s.rolling(period, min_periods=period).std(ddof=0)
    upper = mid + std * sigma
    lower = mid - std * sigma
    return BollingerResult(upper=upper, mid=mid, lower=lower)


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
