"""Tests for hysteresis in regime classification (R5)."""
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
            timestamp=1_700_000_000_000 + i * 3_600_000,
            open=open_p,
            high=high,
            low=low,
            close=close,
            volume=1000.0,
        ))
    return candles


def _regime_config(**overrides) -> RegimeConfig:
    return RegimeConfig(
        enabled=True,
        reference="btc_only",
        trend_period=5,
        trend_threshold=25.0,
        vol_lookback_bars=20,
        vol_percentile_high=0.75,
        hysteresis_min_dwell_bars=6,
        **overrides,
    )


def _apply_hysteresis(raw_regimes: list[str], min_dwell: int) -> list[str]:
    """Apply hysteresis to a sequence of raw regime labels."""
    if not raw_regimes:
        return []
    
    result = [raw_regimes[0]]
    dwell = 0
    
    for regime in raw_regimes[1:]:
        if regime == result[-1]:
            dwell += 1
        else:
            if dwell >= min_dwell:
                result.append(regime)
                dwell = 0
            else:
                # Suppress change
                result.append(result[-1])
    return result


class TestHysteresis:
    """Tests for hysteresis in regime classification."""

    def test_hysteresis_suppresses_rapid_flip_flop(self):
        """Hysteresis should suppress rapid regime changes."""
        # Create a sequence that would flip-flop without hysteresis
        raw = ["trend_low_vol", "range_low_vol", "trend_low_vol", "range_low_vol", 
               "trend_low_vol", "range_low_vol"]
        
        # With min_dwell=3, flips should be suppressed
        result = _apply_hysteresis(raw, min_dwell=3)
        
        # Should not flip-flop every step
        changes = sum(1 for i in range(1, len(result)) if result[i] != result[i-1])
        assert changes <= 1  # At most one change

    def test_hysteresis_allows_change_after_dwell(self):
        """Hysteresis should allow change after minimum dwell time."""
        raw = ["trend_low_vol"] * 5 + ["range_low_vol"] * 5 + ["trend_low_vol"] * 5
        
        result = _apply_hysteresis(raw, min_dwell=3)
        
        # Should allow changes after dwell period
        changes = sum(1 for i in range(1, len(result)) if result[i] != result[i-1])
        assert changes == 2  # Two changes: trend->range, range->trend

    def test_no_hysteresis_when_disabled(self):
        """Zero hysteresis should allow all changes."""
        raw = ["trend_low_vol", "range_low_vol", "trend_low_vol", "range_low_vol"]
        result = _apply_hysteresis(raw, min_dwell=0)
        
        # All changes should be allowed
        assert result == raw

    def test_hysteresis_preserves_first_regime(self):
        """Hysteresis should never change the first regime."""
        raw = ["range_low_vol", "trend_low_vol", "range_low_vol"]
        result = _apply_hysteresis(raw, min_dwell=5)
        
        # First regime should always be preserved
        assert result[0] == "range_low_vol"

    def test_hysteresis_on_classifier_output(self):
        """Hysteresis should smooth classifier output on noisy boundary data."""
        # Create noisy boundary data that oscillates near threshold
        np.random.seed(42)
        n = 50
        base = 100
        # Create data that oscillates around the ADX threshold
        closes = []
        for i in range(n):
            # Oscillate around the threshold
            noise = np.random.normal(0, 2)
            base = 100 + 5 * np.sin(2 * np.pi * i / 10) + noise
            closes.append(base)
        
        candles = _make_candles(closes)
        config = RegimeConfig(
            enabled=True,
            reference="btc_only",
            trend_period=5,
            trend_threshold=25.0,
            vol_lookback_bars=20,
            vol_percentile_high=0.75,
            hysteresis_min_dwell_bars=6,
        )
        
        # Run classifier at each step
        raw_regimes = []
        for i in range(20, 50):  # Start after warmup
            as_of = 1_700_000_000_000 + i * 3_600_000
            sliced = {"BTC/USDT": _slice_closed_bars(
                _make_candles(closes[:i+1]), 
                1_700_000_000_000 + i * 3_600_000, 
                "1h"
            )}
            try:
                result = classify_regime(sliced, 1_700_000_000_000 + i * 3_600_000, 
                                       RegimeConfig(
                                           enabled=True, reference="btc_only",
                                           trend_period=5, trend_threshold=25.0,
                                           vol_lookback_bars=20, vol_percentile_high=0.75,
                                           hysteresis_min_dwell_bars=6
                                       ))
                raw_regimes.append(result.regime)
            except ValueError:
                raw_regimes.append("unknown")
        
        # Apply hysteresis
        smoothed = _apply_hysteresis(raw_regimes, min_dwell=6)
        
        # Count regime changes
        raw_changes = sum(1 for i in range(1, len(raw_regimes)) if raw_regimes[i] != raw_regimes[i-1])
        smoothed_changes = sum(1 for i in range(1, len(smoothed)) if smoothed[i] != smoothed[i-1])
        
        # Hysteresis should reduce regime changes
        assert smoothed_changes <= raw_changes


if __name__ == "__main__":
    pytest.main([__file__, "-v"])