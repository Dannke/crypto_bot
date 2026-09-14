"""Mean reversion feature computation: cross-sectional z-score of short-horizon returns.

This module provides the core signal for the MeanReversionV0 strategy.
It computes rolling mean/std of returns per symbol and the current z-score
on the signal_lookback horizon, with the same no-look-ahead guarantees as
market_snapshot.py (single anchor, single timeframe, closed bars only).
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite, sqrt

from ..portfolio.market_snapshot import MarketSnapshot


@dataclass(frozen=True, slots=True)
class ZScoreSnapshot:
    """Per-symbol z-score snapshot at a single anchor timestamp."""

    as_of_ms: int
    timeframe: str
    zscores: Mapping[str, float]           # symbol -> z-score
    rolling_means: Mapping[str, float]     # symbol -> rolling mean of returns
    rolling_stds: Mapping[str, float]      # symbol -> rolling std of returns
    signal_lookback_bars: int              # horizon of "current deviation"
    window_bars: int                       # rolling estimation window

    def __post_init__(self) -> None:
        if not isinstance(self.as_of_ms, int) or self.as_of_ms < 0:
            raise ValueError("as_of_ms must be a non-negative integer timestamp in milliseconds")
        if not isinstance(self.timeframe, str) or not self.timeframe:
            raise ValueError("timeframe must be a non-empty string")
        if not isinstance(self.zscores, Mapping):
            raise ValueError("zscores must be a mapping")
        if not isinstance(self.rolling_means, Mapping):
            raise ValueError("rolling_means must be a mapping")
        if not isinstance(self.rolling_stds, Mapping):
            raise ValueError("rolling_stds must be a mapping")
        if set(self.zscores.keys()) != set(self.rolling_means.keys()) \
           or set(self.zscores.keys()) != set(self.rolling_stds.keys()):
            raise ValueError("zscores, rolling_means, rolling_stds must have identical keys")
        for symbol, z in self.zscores.items():
            if not isinstance(z, (int, float)) or not isfinite(z):
                raise ValueError(f"zscore for {symbol} must be a finite number")
        for symbol, m in self.rolling_means.items():
            if not isinstance(m, (int, float)) or not isfinite(m):
                raise ValueError(f"rolling_mean for {symbol} must be a finite number")
        for symbol, s in self.rolling_stds.items():
            if not isinstance(s, (int, float)) or not isfinite(s) or s < 0:
                raise ValueError(f"rolling_std for {symbol} must be a finite non-negative number")
        if not isinstance(self.signal_lookback_bars, int) or self.signal_lookback_bars < 1:
            raise ValueError("signal_lookback_bars must be a positive integer")
        if not isinstance(self.window_bars, int) or self.window_bars < 10:
            raise ValueError("window_bars must be >= 10")


def _returns_from_closes(closes: tuple[float, ...]) -> tuple[float, ...]:
    """Compute log returns from a sequence of close prices.

    Returns tuple of length len(closes) - 1.
    """
    if len(closes) < 2:
        return ()
    return tuple(
        (closes[i] / closes[i - 1]) - 1.0
        for i in range(1, len(closes))
    )


def _rolling_mean_std(values: tuple[float, ...], window: int) -> tuple[float, float]:
    """Compute mean and std of the last `window` values.

    Requires at least `window` values. Returns (mean, std).
    """
    if len(values) < window:
        raise ValueError(f"need at least {window} values, got {len(values)}")
    window_vals = values[-window:]
    mean = sum(window_vals) / window
    if window == 1:
        return mean, 0.0
    variance = sum((x - mean) ** 2 for x in window_vals) / (window - 1)
    return mean, sqrt(variance)


def compute_zscore_snapshot(
    snapshot: MarketSnapshot,
    *,
    window_bars: int,
    signal_lookback_bars: int,
) -> ZScoreSnapshot:
    """Compute cross-sectional z-scores at the snapshot's anchor.

    For each symbol with sufficient history:
    1. Compute returns over signal_lookback_bars (the "current deviation")
    2. Compute rolling mean/std of 1-bar returns over window_bars
    3. z = (current_return - rolling_mean) / rolling_std (with floor on std)

    Args:
        snapshot: MarketSnapshot with closed bars only (enforced by caller)
        window_bars: Rolling estimation window for mean/std (e.g., 48)
        signal_lookback_bars: Horizon of the "current" return (e.g., 4 for 4h on 1h tf)

    Returns:
        ZScoreSnapshot with per-symbol z-scores and components.

    Raises:
        ValueError: If window_bars < 10 or signal_lookback_bars < 1.
    """
    if window_bars < 10:
        raise ValueError("window_bars must be >= 10")
    if signal_lookback_bars < 1:
        raise ValueError("signal_lookback_bars must be >= 1")

    zscores: dict[str, float] = {}
    rolling_means: dict[str, float] = {}
    rolling_stds: dict[str, float] = {}

    # Required bars = window_bars (for rolling) + signal_lookback_bars (for current return) + 1
    # Actually we need window_bars returns for rolling + signal_lookback_bars for current
    # Returns need (window_bars + signal_lookback_bars + 1) closes
    min_bars = window_bars + signal_lookback_bars + 1

    for symbol, bars in snapshot.candles_by_symbol.items():
        if len(bars) < min_bars:
            continue

        closes = tuple(bar.close for bar in bars)

        # Current return over signal_lookback_bars
        # close[-1] / close[-1 - signal_lookback_bars] - 1
        current_return = (
            closes[-1] / closes[-1 - signal_lookback_bars] - 1.0
        )

        # 1-bar returns for rolling estimation
        returns = _returns_from_closes(closes)
        # We need the last `window_bars` returns for rolling mean/std
        if len(returns) < window_bars:
            continue

        try:
            mean, std = _rolling_mean_std(returns, window_bars)
        except ValueError:
            continue

        # Floor std to avoid division by zero / extreme z-scores
        floored_std = max(std, 1e-8)

        z = (current_return - mean) / floored_std

        zscores[symbol] = z
        rolling_means[symbol] = mean
        rolling_stds[symbol] = std

    return ZScoreSnapshot(
        as_of_ms=snapshot.as_of_ms,
        timeframe=snapshot.timeframe,
        zscores=zscores,
        rolling_means=rolling_means,
        rolling_stds=rolling_stds,
        signal_lookback_bars=signal_lookback_bars,
        window_bars=window_bars,
    )