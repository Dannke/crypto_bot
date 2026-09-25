"""Mean reversion feature: horizon-consistent z-score of a short-horizon log return.

Definition (``docs/research/mr_cycle2_signal_definition.md``, section 1), per
symbol at the snapshot anchor, with ``h = signal_lookback_bars`` and
``W = window_bars``::

    r_1(j) = ln(close_j / close_{j-1})
    r_h(t) = ln(close_t / close_{t-h})
    s_1(t) = sqrt(mean of r_1(j)^2 over the W one-bar returns ending at bar t-h)
    s_h(t) = sqrt(h) * s_1(t)
    z(t)   = r_h(t) / s_h(t)

Numerator and denominator are on the same horizon ``h``.  The estimation window
ends where the signal window starts, so the current move cannot inflate its own
scale, and no rolling mean is subtracted.  Under independent returns the
numerator and the denominator are independent, and for Gaussian returns
``z ~ t_W`` exactly.

The input is a ``MarketSnapshot``, so the guarantees of ``market_snapshot.py``
carry over: single anchor, single timeframe, closed bars only.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite, log, sqrt

from ..portfolio.market_snapshot import MarketSnapshot


@dataclass(frozen=True, slots=True)
class ZScoreSnapshot:
    """Per-symbol z-scores at a single anchor timestamp."""

    as_of_ms: int
    timeframe: str
    zscores: Mapping[str, float]          # symbol -> z
    sigma_horizon: Mapping[str, float]    # symbol -> s_h, std estimate of the h-bar log return
    signal_lookback_bars: int             # h, horizon of the current return
    window_bars: int                      # W, one-bar returns in the scale estimate

    def __post_init__(self) -> None:
        if not isinstance(self.as_of_ms, int) or self.as_of_ms < 0:
            raise ValueError("as_of_ms must be a non-negative integer timestamp in milliseconds")
        if not isinstance(self.timeframe, str) or not self.timeframe:
            raise ValueError("timeframe must be a non-empty string")
        if not isinstance(self.zscores, Mapping):
            raise ValueError("zscores must be a mapping")
        if not isinstance(self.sigma_horizon, Mapping):
            raise ValueError("sigma_horizon must be a mapping")
        if set(self.zscores) != set(self.sigma_horizon):
            raise ValueError("zscores and sigma_horizon must have identical keys")
        for symbol, z in self.zscores.items():
            if not isinstance(z, (int, float)) or not isfinite(z):
                raise ValueError(f"zscore for {symbol} must be a finite number")
        for symbol, sigma in self.sigma_horizon.items():
            if not isinstance(sigma, (int, float)) or not isfinite(sigma) or sigma <= 0:
                raise ValueError(f"sigma_horizon for {symbol} must be a finite positive number")
        if not isinstance(self.signal_lookback_bars, int) or self.signal_lookback_bars < 1:
            raise ValueError("signal_lookback_bars must be a positive integer")
        if not isinstance(self.window_bars, int) or self.window_bars < 10:
            raise ValueError("window_bars must be >= 10")


def min_history_bars(window_bars: int, signal_lookback_bars: int) -> int:
    """Closed bars a symbol needs before it gets a z-score: W + h + 1."""
    return window_bars + signal_lookback_bars + 1


def zscore_from_closes(
    closes: tuple[float, ...] | list[float],
    *,
    window_bars: int,
    signal_lookback_bars: int,
) -> tuple[float, float] | None:
    """Return ``(z, s_h)`` from the last ``W + h + 1`` closes, or ``None``.

    ``None`` means no z-score: fewer closes than ``W + h + 1``, or a scale
    estimate of zero (a flat window).  A floor on the scale would turn a flat
    window into an arbitrarily large z, i.e. into an entry signal.
    """
    h, w = signal_lookback_bars, window_bars
    needed = min_history_bars(w, h)
    if len(closes) < needed:
        return None
    c = closes[-needed:]
    # c[w] is close_{t-h}; the scale uses the w one-bar returns ending there,
    # the signal is the move from c[w] to c[w + h] = close_t.
    sum_squares = 0.0
    for i in range(1, w + 1):
        r = log(c[i] / c[i - 1])
        sum_squares += r * r
    sigma_horizon = sqrt(h * sum_squares / w)
    if not sigma_horizon > 0.0 or not isfinite(sigma_horizon):
        return None
    return log(c[w + h] / c[w]) / sigma_horizon, sigma_horizon


def compute_zscore_snapshot(
    snapshot: MarketSnapshot,
    *,
    window_bars: int,
    signal_lookback_bars: int,
) -> ZScoreSnapshot:
    """Compute the z-score of every symbol with enough history at the anchor.

    Args:
        snapshot: MarketSnapshot with closed bars only (enforced by its builder).
        window_bars: W, one-bar returns in the scale estimate (e.g. 168).
        signal_lookback_bars: h, horizon of the current return (e.g. 4 for 4h on 1h).

    Symbols with fewer than ``W + h + 1`` bars, or with a flat estimation
    window, get no z-score and are absent from the result.

    Raises:
        ValueError: If window_bars < 10 or signal_lookback_bars < 1.
    """
    if window_bars < 10:
        raise ValueError("window_bars must be >= 10")
    if signal_lookback_bars < 1:
        raise ValueError("signal_lookback_bars must be >= 1")

    zscores: dict[str, float] = {}
    sigma_horizon: dict[str, float] = {}
    for symbol, bars in snapshot.candles_by_symbol.items():
        result = zscore_from_closes(
            tuple(bar.close for bar in bars),
            window_bars=window_bars,
            signal_lookback_bars=signal_lookback_bars,
        )
        if result is None:
            continue
        zscores[symbol], sigma_horizon[symbol] = result

    return ZScoreSnapshot(
        as_of_ms=snapshot.as_of_ms,
        timeframe=snapshot.timeframe,
        zscores=zscores,
        sigma_horizon=sigma_horizon,
        signal_lookback_bars=signal_lookback_bars,
        window_bars=window_bars,
    )
