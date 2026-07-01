"""Technical indicators implemented as pure functions.

Conventions:
  * Each indicator takes a pandas Series or numpy array of numbers and returns
    an aligned array/Series of the same length (NaN-padded at the warmup).
  * No I/O, no logging, no config dependency — fully deterministic and unit
    testable. The same functions are reused by the backtester later.
  * Functions raise ``InsufficientDataError`` only when called with explicit
    too-short input; NaN warmup is the normal output for the first N bars.
"""
from __future__ import annotations

from .adx import adx, last_adx
from .atr import atr, atr_pct, last_atr_pct, true_range
from .bollinger import bollinger_bands, bollinger_position
from .ema import ema, ema_cross_state
from .rsi import rsi
from .volume import volume_ma, volume_spike_ratio

__all__ = [
    "atr",
    "atr_pct",
    "last_atr_pct",
    "true_range",
    "adx",
    "last_adx",
    "bollinger_bands",
    "bollinger_position",
    "ema",
    "ema_cross_state",
    "rsi",
    "volume_ma",
    "volume_spike_ratio",
]
