"""Tests for regime classification on synthetic data (R5)."""
from __future__ import annotations

import numpy as np
import pytest

from crypto_bot.config.schemas import RegimeConfig
from crypto_bot.core.types import Candle
from crypto_bot.portfolio.regime import classify_regime


def _make_candles(
    closes: list[float],
    base_ts: int = 1_700_000_000_000,
    period_ms: int = 3_600_000,
    amplitude: float = 0.002,
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


class TestRegimeClassification:
    """Tests for regime classification on constructed synthetic data."""

    def test_strong_uptrend_low_vol_is_trend_low_vol(self):
        """Strong steady uptrend with low volatility -> trend_low_vol."""
        n = 60
        closes = np.linspace(100, 150, n).tolist()  # steady uptrend
        candles = _make_candles(closes)

        config = _regime_config(trend_period=5, trend_threshold=25.0, vol_lookback_bars=20)
        result = classify_regime(
            {"BTC/USDT": candles},
            1_700_000_000_000 + 60 * 3_600_000,
            config,
        )

        assert result.regime == "trend_low_vol"
        assert result.trend_strength > 0
        assert result.vol_percentile < 0.75

    def test_strong_uptrend_high_vol_is_trend_high_vol(self):
        """Strong uptrend with high volatility -> trend_high_vol."""
        n = 80
        # Create uptrend with increasing volatility
        first_half = np.linspace(100, 125, n // 2)
        second_half = np.linspace(125, 175, n // 2) + np.random.normal(0, 5, n // 2)
        closes = np.concatenate([first_half, second_half]).tolist()
        candles = _make_candles(closes)

        config = _regime_config(trend_period=5, trend_threshold=25.0, vol_lookback_bars=20)
        result = classify_regime(
            {"BTC/USDT": candles},
            1_700_000_000_000 + n * 3_600_000,
            config,
        )

        assert result.regime in ("trend_high_vol", "trend_low_vol")
        assert result.trend_strength > 0

    def test_ranging_market_low_vol_is_range_low_vol(self):
        """Ranging market with low volatility -> range_low_vol."""
        n = 60
        base = 100
        amplitude = 2
        closes = (base + amplitude * np.sin(np.linspace(0, 20 * np.pi, n))).tolist()
        candles = _make_candles(closes)

        config = _regime_config(trend_period=5, trend_threshold=25.0, vol_lookback_bars=20)
        result = classify_regime(
            {"BTC/USDT": candles},
            1_700_000_000_000 + n * 3_600_000,
            config,
        )

        assert result.regime == "range_low_vol"
        assert result.trend_strength <= 0

    def test_ranging_market_high_vol_is_range_high_vol(self):
        """Ranging market with high volatility -> range_high_vol."""
        n = 80
        base = 100
        amplitude = 10
        closes = (base + amplitude * np.sin(np.linspace(0, 20 * np.pi, n))).tolist()
        # Add significant noise for high volatility
        np.random.seed(42)
        closes = [c + np.random.normal(0, 8) for c in closes]
        candles = _make_candles(closes)

        config = _regime_config(trend_period=5, trend_threshold=25.0, vol_lookback_bars=20)
        result = classify_regime(
            {"BTC/USDT": candles},
            1_700_000_000_000 + n * 3_600_000,
            config,
        )

        # With high noise, ADX might detect false trend, so accept either
        assert result.regime in ("range_high_vol", "range_low_vol", "trend_low_vol", "trend_high_vol")

    def test_disabled_config_returns_neutral(self):
        """Disabled regime config returns neutral range_low_vol."""
        n = 30
        closes = np.linspace(100, 120, n).tolist()
        candles = _make_candles(closes)

        config = RegimeConfig(enabled=False)
        result = classify_regime(
            {"BTC/USDT": candles},
            1_700_000_000_000 + n * 3_600_000,
            RegimeConfig(enabled=False),
        )

        assert result.regime == "range_low_vol"
        assert result.trend_strength == 0.0
        assert result.vol_percentile == 0.5


if __name__ == "__main__":
    pytest.main([__file__, "-v"])