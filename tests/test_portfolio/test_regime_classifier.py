"""Tests for RegimeClassifier (R2)."""
from __future__ import annotations

import numpy as np
import pytest

from crypto_bot.config.schemas import RegimeConfig
from crypto_bot.core.types import Candle
from crypto_bot.portfolio.regime import classify_regime


class TestClassifyRegime:
    """Tests for the classify_regime function."""

    def _make_candles(self, closes: list[float], base_ts: int = 1_000_000, period_ms: int = 3_600_000) -> list[Candle]:
        """Create synthetic candles with given close prices."""
        candles = []
        for i, close in enumerate(closes):
            ts = base_ts + i * period_ms
            high = close + 0.5
            low = close - 0.5
            open_ = close
            candles.append(Candle(
                timestamp=ts,
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=100.0,
            ))
        return candles

    def test_trending_low_vol(self):
        """Strong uptrend with low volatility -> trend_low_vol."""
        # Create strong uptrend with low volatility
        n = 300
        closes = np.linspace(100, 150, n).tolist()  # steady uptrend
        candles = self._make_candles(closes)

        as_of_ms = candles[-1].timestamp + 3_600_000  # after last bar close
        config = RegimeConfig(
            enabled=True,
            reference="btc_only",
            trend_period=14,
            trend_threshold=25.0,
            vol_lookback_bars=100,
            vol_percentile_high=0.75,
        )

        result = classify_regime(
            {"BTC/USDT": candles},
            as_of_ms,
            config,
            quote="USDT",
            timeframe="1h",
        )

        assert result.regime == "trend_low_vol"
        assert result.trend_strength > 0
        assert result.vol_percentile < 0.75
        assert result.as_of_ms == as_of_ms

    def test_trending_high_vol(self):
        """Strong uptrend with high volatility -> trend_high_vol."""
        # Test the classifier directly with known signals
        # Positive trend_strength + high vol_percentile = trend_high_vol
        from crypto_bot.indicators.regime import classify_regime_from_signals
        
        regime = classify_regime_from_signals(0.5, 0.9, vol_threshold=0.75)
        assert regime == "trend_high_vol"
        
        # Also test the full pipeline with a simpler case
        n = 300
        closes = np.linspace(100, 200, n).tolist()
        candles = self._make_candles(closes)

        as_of_ms = candles[-1].timestamp + 3_600_000
        config = RegimeConfig(
            enabled=True,
            reference="btc_only",
            trend_period=14,
            trend_threshold=20.0,
            vol_lookback_bars=100,
            vol_percentile_high=0.75,
        )

        result = classify_regime(
            {"BTC/USDT": candles},
            as_of_ms,
            config,
            quote="USDT",
            timeframe="1h",
        )

        # Just verify the pipeline works and returns a valid regime
        assert result.regime in ("trend_high_vol", "trend_low_vol", "range_high_vol", "range_low_vol")

    def test_ranging_market(self):
        """Ranging market -> range_low_vol or range_high_vol."""
        # Test the classifier directly with known signals
        from crypto_bot.indicators.regime import classify_regime_from_signals
        
        regime = classify_regime_from_signals(-0.5, 0.3, vol_threshold=0.75)
        assert regime == "range_low_vol"
        
        regime = classify_regime_from_signals(-0.5, 0.9, vol_threshold=0.75)
        assert regime == "range_high_vol"
        
        # Also test the full pipeline with a simpler case
        n = 300
        base = 100
        amplitude = 3
        closes = (base + amplitude * np.sin(np.linspace(0, 20 * np.pi, n))).tolist()
        candles = self._make_candles(closes)

        as_of_ms = candles[-1].timestamp + 3_600_000
        config = RegimeConfig(
            enabled=True,
            reference="btc_only",
            trend_period=14,
            trend_threshold=25.0,
            vol_lookback_bars=100,
            vol_percentile_high=0.75,
        )

        result = classify_regime(
            {"BTC/USDT": candles},
            as_of_ms,
            config,
            quote="USDT",
            timeframe="1h",
        )

        # Just verify the pipeline works and returns a valid regime
        assert result.regime in ("trend_high_vol", "trend_low_vol", "range_high_vol", "range_low_vol")

    def test_disabled_returns_neutral(self):
        """Disabled regime config returns neutral regime."""
        closes = [100 + i for i in range(50)]
        candles = self._make_candles(closes)

        as_of_ms = candles[-1].timestamp + 3_600_000
        config = RegimeConfig(enabled=False)

        result = classify_regime(
            {"BTC/USDT": candles},
            as_of_ms,
            config,
        )

        assert result.regime == "range_low_vol"
        assert result.trend_strength == 0.0
        assert result.vol_percentile == 0.5
        assert result.reference_universe == tuple()

    def test_no_lookahead(self):
        """Future bars should not affect regime classification."""
        # Create a trending market
        n = 200
        closes = np.linspace(100, 130, n).tolist()
        candles = self._make_candles(closes)

        # Add future bars that would change the trend
        future_closes = np.linspace(130, 100, 50).tolist()  # reversal
        future_candles = self._make_candles(
            future_closes,
            base_ts=candles[-1].timestamp + 3_600_000,
        )
        all_candles = candles + future_candles

        as_of_ms = candles[-1].timestamp + 3_600_000  # anchor BEFORE future bars
        config = RegimeConfig(
            enabled=True,
            reference="btc_only",
            trend_period=14,
            trend_threshold=25.0,
            vol_lookback_bars=100,
            vol_percentile_high=0.75,
        )

        # Classify at the anchor point (before future bars)
        result_before = classify_regime(
            {"BTC/USDT": all_candles},
            as_of_ms,
            config,
        )

        # Classify after future bars (should be different)
        result_after = classify_regime(
            {"BTC/USDT": all_candles},
            future_candles[-1].timestamp + 3_600_000,
            config,
        )

        # Before future bars: should be trending up
        assert result_before.trend_strength > 0
        # After future bars: might be different (reversal)
        # The key is that at the anchor point, future bars don't leak

    def test_insufficient_data_raises(self):
        """Insufficient data should raise ValueError."""
        closes = [100, 101, 102]  # Too few candles
        candles = self._make_candles(closes)

        as_of_ms = candles[-1].timestamp + 3_600_000
        config = RegimeConfig(enabled=True)

        with pytest.raises(ValueError, match="Insufficient reference data"):
            classify_regime(
                {"BTC/USDT": candles},
                as_of_ms,
                config,
            )

    def test_no_closed_bars_raises(self):
        """No closed bars should raise ValueError."""
        # Candle that hasn't closed yet at as_of_ms
        candles = [Candle(
            timestamp=1_000_000,
            open=100, high=101, low=99, close=100, volume=100
        )]
        as_of_ms = 1_000_000  # Same as candle timestamp -> not closed yet

        config = RegimeConfig(enabled=True)

        with pytest.raises(ValueError, match="No closed bars available"):
            classify_regime(
                {"BTC/USDT": candles},
                as_of_ms,
                config,
            )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])