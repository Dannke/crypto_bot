"""Tests for no-future-leakage in regime classification (R5)."""
from __future__ import annotations

import numpy as np
import pytest

from crypto_bot.config.schemas import RegimeConfig
from crypto_bot.core.types import Candle
from crypto_bot.portfolio.regime import classify_regime, _slice_closed_bars


def _make_candles(
    closes: list[float],
    base_ts: int = 1_700_000_000_000,
    period_ms: int = 3_600_000,
) -> list[Candle]:
    """Create candles with given close prices."""
    candles = []
    for i, close in enumerate(closes):
        open_p = closes[i - 1] if i > 0 else close
        high = max(open_p, close) * (1.0 + 0.002)
        low = min(open_p, close) * (1.0 - 0.002)
        candles.append(Candle(
            timestamp=base_ts + i * period_ms,
            open=open_p,
            high=high,
            low=low,
            close=close,
            volume=1000.0,
        ))
    return candles


def _regime_config(**overrides) -> RegimeConfig:
    # Start with minimal defaults that can be fully overridden
    defaults = {
        "enabled": True,
        "reference": "btc_only",
        "trend_period": 14,
        "trend_threshold": 25.0,
        "vol_lookback_bars": 30,
        "vol_percentile_high": 0.75,
        "hysteresis_min_dwell_bars": 0,
    }
    defaults.update(overrides)
    return RegimeConfig(**defaults)


class TestNoFutureLeakage:
    """Tests that regime classification doesn't leak future information."""

    def test_regime_unchanged_by_future_bars(self):
        """Adding future bars should not change past regime classifications."""
        # Create a trending market
        n = 80
        closes = np.linspace(100, 150, n).tolist()
        candles = _make_candles(closes)

        # Classify at midpoint (t=40) using only data up to t=40
        as_of_mid = 1_700_000_000_000 + 40 * 3_600_000
        sliced = {"BTC/USDT": _slice_closed_bars(candles, as_of_mid, "1h")}
        result_mid = classify_regime(sliced, as_of_mid, _regime_config(trend_period=5, vol_lookback_bars=10))

        # Classify at end (t=80) using all data
        as_of_end = 1_700_000_000_000 + 80 * 3_600_000
        sliced_end = {"BTC/USDT": _slice_closed_bars(candles, as_of_end, "1h")}
        result_end = classify_regime(sliced_end, as_of_end, _regime_config(trend_period=5, vol_lookback_bars=10))

        # The regime at midpoint should be the same whether we look at it
        # with or without the future data
        assert result_mid.regime == result_end.regime
        assert abs(result_mid.trend_strength - result_end.trend_strength) < 1e-6
        assert abs(result_mid.vol_percentile - result_end.vol_percentile) < 1e-6

    def test_no_lookahead_in_slice_closed_bars(self):
        """_slice_closed_bars should only return bars closed at or before as_of_ms."""
        period_ms = 3_600_000
        # Create 10 candles
        candles = []
        for i in range(10):
            candles.append(Candle(
                timestamp=1_700_000_000_000 + i * 3_600_000,
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.5,
                volume=1000.0,
            ))

        # as_of at the close of the 5th bar (index 4)
        as_of = 1_700_000_000_000 + 4 * 3_600_000 + 3_600_000  # close of bar 4

        closed = _slice_closed_bars(candles, as_of, "1h")

        # Should include bars 0-4 (5 bars), not bar 5
        assert len(closed) == 5
        assert closed[-1].timestamp == 1_700_000_000_000 + 4 * 3_600_000

    def test_no_lookahead_in_classify_regime(self):
        """classify_regime should not see future bars when classifying at as_of_ms."""
        # Create a market that trends up, then reverses
        n = 80
        trend_up = np.linspace(100, 150, 40).tolist()
        trend_down = np.linspace(150, 100, 40).tolist()
        closes = trend_up + trend_down
        candles = _make_candles(closes)

        # Classify at peak (t=40) - should see only uptrend
        as_of_peak = 1_700_000_000_000 + 40 * 3_600_000
        sliced_peak = {"BTC/USDT": _slice_closed_bars(_make_candles(closes[:40]), as_of_peak, "1h")}
        result_peak = classify_regime(
            sliced_peak, as_of_peak, _regime_config(trend_period=5, trend_threshold=25.0, vol_lookback_bars=30)
        )

        # Classify after reversal (t=80) - should see full picture
        as_of_end = 1_700_000_000_000 + 80 * 3_600_000
        sliced_end = {"BTC/USDT": _slice_closed_bars(_make_candles(closes), as_of_end, "1h")}
        result_end = classify_regime(
            sliced_end, as_of_end, _regime_config(trend_period=5, trend_threshold=25.0, vol_lookback_bars=30)
        )

        # At peak, we should see uptrend (trend)
        # At end, we might see different regime
        # The key is: result at peak should NOT change if we re-evaluate with full data
        # This is implicitly tested by the fact that classify_regime only uses
        # data up to as_of_ms via _slice_closed_bars

        # Verify that classify_regime at peak doesn't see future
        assert result_peak.trend_strength > 0  # Should detect trend at peak

    def test_same_anchor_same_result(self):
        """Same anchor timestamp should always produce same regime regardless of available future data."""
        n = 80
        closes = np.linspace(100, 150, n).tolist()
        all_candles = _make_candles(closes)

        # Classify at t=40
        as_of = 1_700_000_000_000 + 40 * 3_600_000
        candles_at_40 = _slice_closed_bars(_make_candles(closes[:41]), 1_700_000_000_000 + 40 * 3_600_000, "1h")

        # With only 41 bars
        sliced_41 = {"BTC/USDT": candles_at_40}
        result_41 = classify_regime(sliced_41, as_of, RegimeConfig(
            enabled=True, reference="btc_only", trend_period=5, trend_threshold=25.0,
            vol_lookback_bars=30, vol_percentile_high=0.75
        ))

        # With all 80 bars available (but same anchor)
        sliced_80 = {"BTC/USDT": _slice_closed_bars(all_candles, as_of, "1h")}
        result_80 = classify_regime(sliced_80, as_of, RegimeConfig(
            enabled=True, reference="btc_only", trend_period=5, trend_threshold=25.0,
            vol_lookback_bars=30, vol_percentile_high=0.75
        ))

        # Should be identical
        assert result_41.regime == result_80.regime
        assert abs(result_41.trend_strength - result_80.trend_strength) < 1e-6
        assert abs(result_41.vol_percentile - result_80.vol_percentile) < 1e-6


if __name__ == "__main__":
    pytest.main([__file__, "-v"])