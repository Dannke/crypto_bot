"""Tests for RegimeConfig validation (R6)."""
from __future__ import annotations

import pytest

from crypto_bot.config.schemas import RegimeConfig


class TestRegimeConfig:
    """Tests for RegimeConfig validation and defaults."""

    def test_valid_defaults(self):
        """Default RegimeConfig should be valid."""
        config = RegimeConfig()
        assert config.enabled is True
        assert config.reference == "universe_basket"
        assert config.trend_indicator == "adx"
        assert config.trend_period == 14
        assert config.trend_threshold == 25.0
        assert config.vol_lookback_bars == 168
        assert config.vol_percentile_high == 0.75
        assert config.hysteresis_min_dwell_bars == 6
        assert config.exposure_trend_low_vol == 1.0
        assert config.exposure_trend_high_vol == 0.5
        assert config.exposure_range_low_vol == 0.25
        assert config.exposure_range_high_vol == 0.0

    def test_custom_values(self):
        """Custom values should be accepted when valid."""
        config = RegimeConfig(
            enabled=False,
            reference="btc_only",
            trend_period=20,
            trend_threshold=30.0,
            vol_lookback_bars=200,
            vol_percentile_high=0.8,
            hysteresis_min_dwell_bars=10,
            exposure_trend_low_vol=0.8,
            exposure_trend_high_vol=0.4,
            exposure_range_low_vol=0.3,
            exposure_range_high_vol=0.1,
        )
        assert config.enabled is False
        assert config.reference == "btc_only"
        assert config.trend_period == 20
        assert config.trend_threshold == 30.0
        assert config.vol_lookback_bars == 200
        assert config.vol_percentile_high == 0.8
        assert config.hysteresis_min_dwell_bars == 10
        assert config.exposure_trend_low_vol == 0.8
        assert config.exposure_trend_high_vol == 0.4
        assert config.exposure_range_low_vol == 0.3
        assert config.exposure_range_high_vol == 0.1

    def test_trend_period_validation(self):
        """trend_period must be >= 1."""
        with pytest.raises(ValueError, match="trend_period must be >= 1"):
            RegimeConfig(trend_period=0)

    def test_trend_threshold_validation(self):
        """trend_threshold must be in (0, 100]."""
        with pytest.raises(ValueError, match="trend_threshold must be in"):
            RegimeConfig(trend_threshold=0.0)
        with pytest.raises(ValueError, match="trend_threshold must be in"):
            RegimeConfig(trend_threshold=-1.0)
        with pytest.raises(ValueError, match="trend_threshold must be in"):
            RegimeConfig(trend_threshold=101.0)

    def test_trend_indicator_validation(self):
        """trend_indicator must be 'adx'."""
        with pytest.raises(ValueError, match="trend_indicator must be 'adx'"):
            RegimeConfig(trend_indicator="rsi")

    def test_vol_lookback_bars_validation(self):
        """vol_lookback_bars must be >= 2 * trend_period."""
        with pytest.raises(ValueError, match="vol_lookback_bars.*must be >= 2 \\* trend_period"):
            RegimeConfig(trend_period=14, vol_lookback_bars=20)
        # Valid case
        RegimeConfig(trend_period=14, vol_lookback_bars=28)

    def test_vol_percentile_high_validation(self):
        """vol_percentile_high must be in (0, 1)."""
        with pytest.raises(ValueError, match="vol_percentile_high must be in"):
            RegimeConfig(vol_percentile_high=0.0)
        with pytest.raises(ValueError, match="vol_percentile_high must be in"):
            RegimeConfig(vol_percentile_high=1.0)

    def test_hysteresis_validation(self):
        """hysteresis_min_dwell_bars must be >= 0."""
        with pytest.raises(ValueError, match="hysteresis_min_dwell_bars must be >= 0"):
            RegimeConfig(hysteresis_min_dwell_bars=-1)

    def test_exposure_multiplier_validation(self):
        """Exposure multipliers must be in [0, 1]."""
        for field in (
            "exposure_trend_low_vol",
            "exposure_trend_high_vol",
            "exposure_range_low_vol",
            "exposure_range_high_vol",
        ):
            with pytest.raises(ValueError, match=f"regime\\.{field} must be in"):
                RegimeConfig(**{field: -0.1})
            with pytest.raises(ValueError, match=f"regime\\.{field} must be in"):
                RegimeConfig(**{field: 1.1})

    def test_yaml_roundtrip(self):
        """RegimeConfig should round-trip through YAML."""
        import yaml
        config = RegimeConfig(
            enabled=False,
            reference="btc_only",
            trend_period=20,
            trend_threshold=30.0,
            vol_lookback_bars=200,
            vol_percentile_high=0.8,
            hysteresis_min_dwell_bars=10,
            exposure_trend_low_vol=0.8,
            exposure_trend_high_vol=0.4,
            exposure_range_low_vol=0.3,
            exposure_range_high_vol=0.1,
        )
        yaml_str = yaml.dump(config.model_dump())
        loaded = RegimeConfig(**yaml.safe_load(yaml_str))
        assert loaded.enabled is False
        assert loaded.reference == "btc_only"
        assert loaded.trend_period == 20
        assert loaded.trend_threshold == 30.0
        assert loaded.vol_lookback_bars == 200
        assert loaded.vol_percentile_high == 0.8
        assert loaded.hysteresis_min_dwell_bars == 10
        assert loaded.exposure_trend_low_vol == 0.8
        assert loaded.exposure_trend_high_vol == 0.4
        assert loaded.exposure_range_low_vol == 0.3
        assert loaded.exposure_range_high_vol == 0.1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])