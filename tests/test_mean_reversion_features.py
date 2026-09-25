"""The mean reversion z-score is the statistic it claims to be.

Unit and wiring checks cannot see a dimension error inside a statistic: the
v1-v4 feature divided an 8h return by the std of 1h returns, both "in
percent", and survived four pre-registrations (MR closure). These tests pin the
definition of docs/research/mr_cycle2_signal_definition.md, section 1, and
check its null distribution directly on the real function.
"""
from __future__ import annotations

from math import erfc, exp, log, sqrt

import numpy as np
import pytest

from crypto_bot.core.types import Candle
from crypto_bot.portfolio import get_market_snapshot, get_universe_snapshot
from crypto_bot.portfolio.mean_reversion_features import (
    compute_zscore_snapshot,
    min_history_bars,
    zscore_from_closes,
)
from crypto_bot.simulation.historical_source import HistoricalCandleSource

PERIOD_MS = 3_600_000
BASE_TS = 1_700_000_000_000
H = 4      # registered signal horizon, bars
W = 168    # registered estimation window, bars


def _closes_from_log_returns(returns: list[float], start: float = 100.0) -> list[float]:
    closes = [start]
    for r in returns:
        closes.append(closes[-1] * exp(r))
    return closes


def _candles(closes: list[float], first_index: int = 0) -> list[Candle]:
    return [
        Candle(timestamp=BASE_TS + (first_index + i) * PERIOD_MS, open=c,
               high=c * 1.001, low=c * 0.999, close=c, volume=1.0)
        for i, c in enumerate(closes)
    ]


class TestDefinition:
    def test_min_history_is_window_plus_horizon_plus_one(self) -> None:
        assert min_history_bars(W, H) == 173

    def test_matches_the_closed_form(self) -> None:
        """z = ln(P_t/P_{t-h}) / (sqrt(h) * RMS of the W returns ending at t-h)."""
        rng = np.random.default_rng(1)
        returns = list(rng.normal(0.0, 0.01, W + H))
        closes = _closes_from_log_returns(returns)

        result = zscore_from_closes(closes, window_bars=W, signal_lookback_bars=H)
        assert result is not None
        z, sigma_h = result

        window = returns[:W]                  # the W returns ending at close_{t-h}
        expected_sigma_h = sqrt(H) * sqrt(sum(r * r for r in window) / W)
        expected_z = log(closes[-1] / closes[-1 - H]) / expected_sigma_h
        assert sigma_h == pytest.approx(expected_sigma_h, rel=1e-12)
        assert z == pytest.approx(expected_z, rel=1e-12)

    def test_uses_only_the_last_window_plus_horizon_closes(self) -> None:
        rng = np.random.default_rng(2)
        closes = _closes_from_log_returns(list(rng.normal(0.0, 0.01, W + H)))
        older = _closes_from_log_returns(list(rng.normal(0.0, 0.05, 50)), start=closes[0])
        longer = older[:-1] + closes
        assert zscore_from_closes(longer, window_bars=W, signal_lookback_bars=H) == \
            zscore_from_closes(closes, window_bars=W, signal_lookback_bars=H)

    def test_estimation_window_is_disjoint_from_the_signal_window(self) -> None:
        """The current move cannot inflate its own scale.

        Replacing the h signal returns leaves sigma_h unchanged; changing the
        return that ends at t-h, the last one of the window, changes it.
        """
        rng = np.random.default_rng(3)
        returns = list(rng.normal(0.0, 0.01, W + H))
        base = zscore_from_closes(_closes_from_log_returns(returns),
                                  window_bars=W, signal_lookback_bars=H)

        shocked_signal = returns[:W] + [0.05] * H
        shocked = zscore_from_closes(_closes_from_log_returns(shocked_signal),
                                     window_bars=W, signal_lookback_bars=H)
        assert base is not None and shocked is not None
        assert shocked[1] == pytest.approx(base[1], rel=1e-12)
        assert shocked[0] > base[0]

        shocked_window = returns[:W - 1] + [0.05] + returns[W:]
        moved = zscore_from_closes(_closes_from_log_returns(shocked_window),
                                   window_bars=W, signal_lookback_bars=H)
        assert moved is not None
        assert moved[1] != pytest.approx(base[1], rel=1e-6)

    def test_no_rolling_mean_is_subtracted(self) -> None:
        """A steady trend in the window is not netted out of the current move."""
        steady = [0.001] * (W + H)
        result = zscore_from_closes(_closes_from_log_returns(steady),
                                    window_bars=W, signal_lookback_bars=H)
        assert result is not None
        # r_h = 4 * 0.001, sigma_h = sqrt(4) * 0.001  ->  z = 2 (not 0)
        assert result[0] == pytest.approx(2.0, rel=1e-9)

    def test_short_history_gives_no_zscore(self) -> None:
        closes = _closes_from_log_returns([0.01] * (W + H - 1))
        assert zscore_from_closes(closes, window_bars=W, signal_lookback_bars=H) is None

    def test_flat_window_gives_no_zscore_instead_of_a_huge_one(self) -> None:
        """A floor on the scale would turn a stale price into an entry signal."""
        closes = [100.0] * (W + 1) + [101.0, 102.0, 103.0, 104.0]
        assert zscore_from_closes(closes, window_bars=W, signal_lookback_bars=H) is None


class TestSnapshot:
    def test_symbols_without_enough_history_are_absent(self) -> None:
        rng = np.random.default_rng(4)
        full = _closes_from_log_returns(list(rng.normal(0.0, 0.01, W + H)))
        source = HistoricalCandleSource()
        source.load_all("FULL/USDT", "1h", _candles(full))
        source.load_all("SHORT/USDT", "1h", _candles(full[-20:], first_index=len(full) - 20))
        anchor = BASE_TS + len(full) * PERIOD_MS
        universe = get_universe_snapshot(source, ["FULL/USDT", "SHORT/USDT"], "1h", anchor)

        snap = compute_zscore_snapshot(get_market_snapshot(source, universe, "1h"),
                                       window_bars=W, signal_lookback_bars=H)

        assert set(snap.zscores) == {"FULL/USDT"}
        assert set(snap.sigma_horizon) == {"FULL/USDT"}
        assert snap.window_bars == W and snap.signal_lookback_bars == H

    def test_bars_after_the_anchor_do_not_change_the_zscore(self) -> None:
        rng = np.random.default_rng(5)
        closes = _closes_from_log_returns(list(rng.normal(0.0, 0.01, W + H + 30)))
        n_known = W + H + 1
        anchor = BASE_TS + n_known * PERIOD_MS

        def z_at_anchor(bars: list[float]) -> float:
            source = HistoricalCandleSource()
            source.load_all("SYM/USDT", "1h", _candles(bars))
            universe = get_universe_snapshot(source, ["SYM/USDT"], "1h", anchor)
            snap = compute_zscore_snapshot(get_market_snapshot(source, universe, "1h"),
                                           window_bars=W, signal_lookback_bars=H)
            return snap.zscores["SYM/USDT"]

        assert z_at_anchor(closes) == z_at_anchor(closes[:n_known])


@pytest.fixture(scope="module")
def null_zscores() -> np.ndarray:
    """z at every bar of a seeded Gaussian random walk, via the real function."""
    rng = np.random.default_rng(20260925)
    closes = _closes_from_log_returns(list(rng.normal(0.0, 0.005, 20_000)))
    needed = min_history_bars(W, H)
    out = []
    for end in range(needed, len(closes) + 1):
        result = zscore_from_closes(closes[end - needed:end],
                                    window_bars=W, signal_lookback_bars=H)
        assert result is not None
        out.append(result[0])
    return np.asarray(out)


class TestNullDistribution:
    """On a Gaussian random walk z ~ t_W: std 1, P(|z| >= 2) close to 4.71%.

    The v1-v4 statistic gives about 47% on the same null and the v4 scan's
    "true" z about 8.4% (scripts/sigma_definition_theory.py), so both fail here.
    """

    def test_unit_scale(self, null_zscores: np.ndarray) -> None:
        assert 0.95 <= float(np.std(null_zscores)) <= 1.05

    def test_two_sigma_frequency(self, null_zscores: np.ndarray) -> None:
        # Hourly z values overlap by h-1 bars, so the effective sample is a
        # few thousand; +-0.9 pp is over three standard errors.
        frequency = float(np.mean(np.abs(null_zscores) >= 2.0))
        normal = erfc(2.0 / sqrt(2.0))
        assert abs(frequency - 0.0471) <= 0.009, (frequency, normal)
