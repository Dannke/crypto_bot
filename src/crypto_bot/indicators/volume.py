"""Volume analytics: moving average and spike ratio.

A volume spike (current volume >> its MA) is a confirmation signal — moves on
rising volume are more trustworthy than those on falling volume.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def volume_ma(volumes: pd.Series | np.ndarray[Any, Any], period: int = 20) -> pd.Series:
    """Simple moving average of volume."""
    if period < 1:
        raise ValueError("volume MA period must be >= 1")
    s = pd.Series(volumes, dtype="float64").reset_index(drop=True)
    return s.rolling(period, min_periods=period).mean()


def volume_spike_ratio(
    volumes: pd.Series | np.ndarray[Any, Any], period: int = 20
) -> pd.Series:
    """Latest volume / its moving average. NaN during warmup."""
    ma = volume_ma(volumes, period)
    return pd.Series(volumes, dtype="float64").reset_index(drop=True) / ma.replace(
        0.0, np.nan
    )
