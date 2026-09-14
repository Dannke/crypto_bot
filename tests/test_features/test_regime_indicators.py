"""Tests for regime indicators (R1)."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from crypto_bot.indicators.regime import (
    classify_regime_from_signals,
    regime_trend_strength,
    rolling_atr_percentile,
)


class TestRegimeTrendStrength:
    """Tests for regime_trend_strength indicator."""

    def test_trending_market_returns_positive(self):
        """Strong uptrend should give positive trend strength."""
        # Create a strong trending market: steady uptrend
        n = 200
        close = np.linspace(100, 150, n)  # steady uptrend
        high = close + 0.5
        low = close - 0.5

        result = regime_trend_strength(high, low, close, period=14, threshold=25.0)
        last_val = result.iloc[-1]

        assert not np.isnan(last_val)
        assert last_val > 0  # Positive = trending

    def test_ranging_market_returns_negative(self):
        """Ranging market should give negative trend strength."""
        # Create a ranging market: oscillating around a mean
        n = 200
        base = 100
        amplitude = 2
        close = base + amplitude * np.sin(np.linspace(0, 20 * np.pi, n))
        high = close + 0.5
        low = close - 0.5

        result = regime_trend_strength(high, low, close, period=14, threshold=25.0)
        last_val = result.iloc[-1]

        assert not np.isnan(last_val)
        assert last_val <= 0  # Negative or zero = ranging

    def test_insufficient_data_returns_nan(self):
        """Insufficient data should return NaN series."""
        close = np.array([100.0, 101.0, 102.0])
        high = close + 0.5
        low = close - 0.5

        result = regime_trend_strength(high, low, close, period=14, threshold=25.0)

        assert len(result) == 3
        assert all(np.isnan(v) for v in result)

    def test_low_threshold_treats_more_as_trending(self):
        """Lower threshold treats more ADX values as trending."""
        n = 100
        close = np.linspace(100, 110, n)
        high = close + 0.5
        low = close - 0.5

        result_high_thresh = regime_trend_strength(high, low, close, period=10, threshold=25.0)
        result_low_thresh = regime_trend_strength(high, low, close, period=10, threshold=10.0)

        high_last = result_high_thresh.iloc[-1]
        low_last = result_low_thresh.iloc[-1]

        assert not np.isnan(high_last)
        assert not np.isnan(low_last)
        # Lower threshold should give higher (more positive) trend strength
        assert low_last >= high_last


class TestRollingAtrPercentile:
    """Tests for rolling_atr_percentile indicator."""

    def test_trending_volatility_returns_high_percentile(self):
        """Increasing volatility should give high percentile."""
        n = 250
        # Start with low volatility, then high volatility
        close = np.concatenate([
            np.linspace(100, 102, n // 2),  # low vol
            np.linspace(102, 120, n // 2),  # high vol
        ])
        # Add more noise in second half
        noise = np.zeros(n)
        noise[n // 2:] = np.random.normal(0, 2, n // 2)
        close = close + noise
        high = close + np.abs(noise) + 0.5
        low = close - np.abs(noise) - 0.5

        result = rolling_atr_percentile(high, low, close, lookback_bars=50, period=14)

        # First half should have low percentile, second half high percentile
        mid = n // 2
        first_half_avg = np.nanmean(result.iloc[:mid])
        second_half_avg = np.nanmean(result.iloc[mid:])

        assert not np.isnan(first_half_avg)
        assert not np.isnan(second_half_avg)
        assert second_half_avg > first_half_avg  # Volatility increased

    def test_constant_volatility_returns_mid_percentile(self):
        """Constant volatility should give percentile around 0.5."""
        n = 200
        close = np.linspace(100, 105, n) + np.random.normal(0, 0.5, n)
        high = close + 0.5
        low = close - 0.5

        result = rolling_atr_percentile(high, low, close, lookback_bars=50, period=14)

        # Skip NaN warmup period
        valid = result.dropna()
        avg = np.mean(valid)

        # Should be around 0.5 (median)
        assert 0.3 < avg < 0.7

    def test_insufficient_data_returns_nan(self):
        """Insufficient data should return NaN series."""
        close = np.array([100.0, 101.0, 102.0])
        high = close + 0.5
        low = close - 0.5

        result = rolling_atr_percentile(high, low, close, lookback_bars=50, period=14)

        assert len(result) == 3
        assert all(np.isnan(v) for v in result)

    def test_lookback_validation(self):
        """lookback_bars must be >= 2 * period."""
        close = np.linspace(100, 110, 50)
        high = close + 0.5
        low = close - 0.5

        with pytest.raises(ValueError, match="lookback_bars must be >= 2 \\* period"):
            rolling_atr_percentile(high, low, close, lookback_bars=10, period=14)


class TestClassifyRegimeFromSignals:
    """Tests for classify_regime_from_signals."""

    def test_trend_low_vol(self):
        """Positive trend strength + low vol percentile = trend_low_vol."""
        regime = classify_regime_from_signals(0.5, 0.3, vol_threshold=0.75)
        assert regime == "trend_low_vol"

    def test_trend_high_vol(self):
        """Positive trend strength + high vol percentile = trend_high_vol."""
        regime = classify_regime_from_signals(0.5, 0.9, vol_threshold=0.75)
        assert regime == "trend_high_vol"

    def test_range_low_vol(self):
        """Negative trend strength + low vol percentile = range_low_vol."""
        regime = classify_regime_from_signals(-0.5, 0.3, vol_threshold=0.75)
        assert regime == "range_low_vol"

    def test_range_high_vol(self):
        """Negative trend strength + high vol percentile = range_high_vol."""
        regime = classify_regime_from_signals(-0.5, 0.9, vol_threshold=0.75)
        assert regime == "range_high_vol"

    def test_boundary_conditions(self):
        """Test boundary conditions at vol_threshold."""
        # Exactly at threshold
        regime = classify_regime_from_signals(0.5, 0.75, vol_threshold=0.75)
        assert regime == "trend_low_vol"  # Not > threshold, so low vol

        # Just above threshold
        regime = classify_regime_from_signals(0.5, 0.76, vol_threshold=0.75)
        assert regime == "trend_high_vol"

    def test_nan_inputs_returns_default(self):
        """NaN inputs should return default (range_low_vol)."""
        regime = classify_regime_from_signals(np.nan, 0.5)
        assert regime == "range_low_vol"

        regime = classify_regime_from_signals(0.5, np.nan)
        assert regime == "range_low_vol"


class TestFeatureBuilderRegimeIntegration:
    """Integration tests for FeatureBuilder regime fields."""

    def test_feature_set_has_regime_fields(self):
        """FeatureSet should have regime_trend_strength and regime_vol_percentile."""
        from crypto_bot.core.types import FeatureSet
        from crypto_bot.indicators.regime import classify_regime_from_signals

        fs = FeatureSet(
            symbol="BTC/USDT",
            timeframe="1h",
            trend_score=0.5,
            momentum_score=0.5,
            volatility_score=0.5,
            volume_score=0.5,
            adx=25.0,
            rsi=50.0,
            atr_pct=1.0,
            ema_fast=100.0,
            ema_mid=99.0,
            ema_slow=98.0,
            candle_timestamp_ms=1_000_000,
        )

        # Check default values
        assert fs.regime_trend_strength == 0.0
        assert fs.regime_vol_percentile == 0.0

        # Can be set
        fs = FeatureSet(
            symbol="BTC/USDT",
            timeframe="1h",
            trend_score=0.5,
            momentum_score=0.5,
            volatility_score=0.5,
            volume_score=0.5,
            adx=25.0,
            rsi=50.0,
            atr_pct=1.0,
            ema_fast=100.0,
            ema_mid=99.0,
            ema_slow=98.0,
            candle_timestamp_ms=1_000_000,
            regime_trend_strength=0.5,
            regime_vol_percentile=0.75,
        )

        assert fs.regime_trend_strength == 0.5
        assert fs.regime_vol_percentile == 0.75


if __name__ == "__main__":
    pytest.main([__file__, "-v"])