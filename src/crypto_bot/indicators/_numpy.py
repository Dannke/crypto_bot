"""Numpy-vectorised indicator helpers (drop-in for pandas ewm/rolling).

These replicate the exact semantics of the pandas calls used by the
indicators module (``ewm(alpha=.., adjust=False, min_periods=..).mean()`` and
``rolling(period, min_periods=period).mean()/.std(ddof=0)``) without the
Series/DataFrame construction overhead — the backtest hot path calls these
hundreds of thousands of times.

NaN semantics match pandas:
  * ewm: NaN inputs are skipped (state is carried over, no observation
    consumed); the output AT a NaN position repeats the last computed state
    rather than emitting NaN.
  * rolling: a window is defined only when it contains at least
    ``min_periods`` non-NaN values; the mean/std is computed over the
    non-NaN values only.
"""
from __future__ import annotations

import numpy as np


def ewm_mean(x: np.ndarray, alpha: float, min_periods: int) -> np.ndarray:
    """Wilder-style EMA: out[i] = alpha*x[i] + (1-alpha)*out[i-1], NaN warmup.

    Matches ``pandas.Series.ewm(alpha=alpha, adjust=False,
    min_periods=min_periods).mean()``: the first ``min_periods-1`` non-NaN
    observations yield NaN; NaN inputs are skipped without consuming the
    observation count, and the output at a NaN position repeats the last
    computed state (pandas skipna behaviour).
    """
    n = len(x)
    out = np.full(n, np.nan, dtype="float64")
    if n == 0:
        return out
    start = 0
    while start < n and np.isnan(x[start]):
        start += 1
    if start == n:
        return out
    ema = x[start]
    count = 1
    if count >= min_periods:
        out[start] = ema
    for i in range(start + 1, n):
        xi = x[i]
        if np.isnan(xi):
            if count >= min_periods:
                out[i] = ema
            continue
        ema = alpha * xi + (1.0 - alpha) * ema
        count += 1
        if count >= min_periods:
            out[i] = ema
    return out


def _rolling_counts_and_sums(x: np.ndarray, period: int):
    """Per-window non-NaN counts and sums (both missing -> all-NaN input)."""
    n = len(x)
    valid = ~np.isnan(x)
    csum_valid = np.cumsum(valid.astype("float64"))
    x_clean = np.where(valid, x, 0.0)
    csum_x = np.cumsum(x_clean)
    counts = np.full(n, 0, dtype="int64")
    sums = np.full(n, np.nan, dtype="float64")
    if n < period:
        return counts, sums
    counts[period - 1 :] = csum_valid[period - 1 :] - np.concatenate(([0.0], csum_valid[: n - period])).astype("int64")
    sums[period - 1 :] = csum_x[period - 1 :] - np.concatenate(([0.0], csum_x[: n - period]))
    return counts, sums


def rolling_mean(x: np.ndarray, period: int) -> np.ndarray:
    """Simple moving average with NaN warmup of ``period-1`` bars.

    Matches ``pandas.rolling(period, min_periods=period).mean()`` with
    skipna: the window needs ``period`` non-NaN values; the average is
    computed over the non-NaN values only.
    """
    counts, sums = _rolling_counts_and_sums(np.asarray(x, dtype="float64"), period)
    out = np.full(len(x), np.nan, dtype="float64")
    mask = counts >= period
    out[mask] = sums[mask] / counts[mask]
    return out


def rolling_std(x: np.ndarray, period: int, ddof: int = 0) -> np.ndarray:
    """Rolling population/std (ddof=0) standard deviation, NaN warmup.

    Matches ``pandas.rolling(period, min_periods=period).std(ddof=ddof)``
    with skipna: computed over the non-NaN values of each window.
    """
    x = np.asarray(x, dtype="float64")
    n = len(x)
    out = np.full(n, np.nan, dtype="float64")
    if n < period:
        return out
    valid = ~np.isnan(x)
    x_clean = np.where(valid, x, 0.0)
    csum_valid = np.cumsum(valid.astype("float64"))
    csum_x = np.cumsum(x_clean)
    csum_x2 = np.cumsum(x_clean * x_clean)
    cnt = csum_valid[period - 1 :] - np.concatenate(([0.0], csum_valid[: n - period]))
    s1 = csum_x[period - 1 :] - np.concatenate(([0.0], csum_x[: n - period]))
    s2 = csum_x2[period - 1 :] - np.concatenate(([0.0], csum_x2[: n - period]))
    mask = cnt >= period
    if not mask.any():
        return out
    mean = s1[mask] / cnt[mask]
    var = s2[mask] / cnt[mask] - mean * mean
    var = np.clip(var, 0.0, None)
    denom = cnt[mask] - ddof
    if ddof > 0:
        var = var * cnt[mask] / denom
    out[period - 1 :][mask] = np.sqrt(var)
    return out
