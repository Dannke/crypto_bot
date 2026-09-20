"""Tests for regime-based exposure scaling (R5)."""
from __future__ import annotations

import numpy as np
import pytest

from crypto_bot.config.schemas import RegimeConfig
from crypto_bot.core.types import Candle
from crypto_bot.pipeline.portfolio_fusion import RegimeGatedFusion
from crypto_bot.portfolio.models import PortfolioIntent, PositionIntent
from crypto_bot.portfolio.regime import classify_regime
from crypto_bot.core.enums import Side
from crypto_bot.core.types import Candle


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
            timestamp=base_ts + i * period_ms,
            open=open_p,
            high=high,
            low=low,
            close=close,
            volume=1000.0,
        ))
    return candles


def _make_trending_candles(n: int = 60, trend: float = 1.01) -> list[Candle]:
    """Create trending candles."""
    price = 100.0
    candles = []
    for i in range(n):
        close = price * trend
        open_p = price
        high = max(open_p, close) * 1.002
        low = min(open_p, close) * 0.998
        candles.append(Candle(
            timestamp=1_700_000_000_000 + i * 3_600_000,
            open=open_p,
            high=high,
            low=low,
            close=close,
            volume=1000.0,
        ))
        price = close
    return candles


def _make_ranging_candles(n: int = 60, amplitude: float = 5.0) -> list[Candle]:
    """Create ranging (sinusoidal) candles."""
    base = 100.0
    candles = []
    for i in range(n):
        close = 100.0 + amplitude * np.sin(2 * np.pi * i / 20)
        open_p = 100.0 + amplitude * np.sin(2 * np.pi * (i - 1) / 20) if i > 0 else 100.0
        high = max(open_p, close) * 1.002
        low = min(open_p, close) * 0.998
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
        hysteresis_min_dwell_bars=0,
        exposure_trend_low_vol=1.0,
        exposure_trend_high_vol=0.5,
        exposure_range_low_vol=0.25,
        exposure_range_high_vol=0.0,
    )


def _make_intent(side: str, weight: float = 0.25, strategy_name: str = "test") -> PortfolioIntent:
    from crypto_bot.portfolio.models import PortfolioIntent, PositionIntent
    from crypto_bot.core.enums import Side
    return PortfolioIntent(
        as_of_ms=1_700_000_000_000,
        intents=(PositionIntent(
            symbol="BTC/USDT",
            side=Side(side),
            target_weight=weight,
            timeframe="1h",
            reference_price=100.0,
        ),),
        strategy_name=strategy_name,
    )


class TestRegimeExposureScaling:
    """Tests for regime-based exposure scaling."""

    def test_trend_low_vol_full_exposure(self):
        """trend_low_vol should allow full exposure (multiplier 1.0) for strategies with overrides."""
        fusion = RegimeGatedFusion()
        intent = _make_intent("LONG", 0.5, strategy_name="mean_reversion_v0")

        from crypto_bot.portfolio.models import RegimeSnapshot
        regime = RegimeSnapshot(
            as_of_ms=1_700_000_000_000,
            regime="trend_low_vol",
            trend_strength=0.5,
            vol_percentile=0.5,
            reference_universe=("BTC/USDT",),
        )

        fused = fusion.fuse([intent], regime)
        # Weight should be unchanged (multiplier 1.0 for trend_low_vol)
        assert abs(fused.intents[0].target_weight - 0.5) < 1e-6

    def test_trend_low_vol_no_gating_for_strategies_without_overrides(self):
        """Strategies without overrides get full exposure (multiplier 1.0) in all regimes."""
        fusion = RegimeGatedFusion()
        intent = _make_intent("LONG", 0.5, strategy_name="cross_sectional_momentum_v0")

        from crypto_bot.portfolio.models import RegimeSnapshot
        regime = RegimeSnapshot(
            as_of_ms=1_700_000_000_000,
            regime="trend_low_vol",
            trend_strength=0.5,
            vol_percentile=0.5,
            reference_universe=("BTC/USDT",),
        )

        fused = fusion.fuse([intent], regime)
        # Weight should be unchanged (no regime gating for strategies without overrides)
        assert abs(fused.intents[0].target_weight - 0.5) < 1e-6

    def test_trend_high_vol_half_exposure(self):
        """trend_high_vol should halve exposure (multiplier 0.5) for strategies with overrides."""
        # Need to explicitly provide overrides for mean_reversion_v0 to test regime gating
        fusion = RegimeGatedFusion(
            strategy_overrides={
                "mean_reversion_v0": {
                    "trend_high_vol": 0.5,
                }
            }
        )
        intent = _make_intent("LONG", 0.5, strategy_name="mean_reversion_v0")

        from crypto_bot.portfolio.models import RegimeSnapshot
        regime = RegimeSnapshot(
            as_of_ms=1_700_000_000_000,
            regime="trend_high_vol",
            trend_strength=0.5,
            vol_percentile=0.9,
            reference_universe=("BTC/USDT",),
        )

        fused = fusion.fuse([intent], regime)
        # Weight should be halved (multiplier 0.5 for trend_high_vol with override)
        assert abs(fused.intents[0].target_weight - 0.25) < 1e-6

    def test_trend_high_vol_no_gating_for_strategies_without_overrides(self):
        """Strategies without overrides get full exposure in trend_high_vol."""
        fusion = RegimeGatedFusion()
        intent = _make_intent("LONG", 0.5, strategy_name="cross_sectional_momentum_v0")

        from crypto_bot.portfolio.models import RegimeSnapshot
        regime = RegimeSnapshot(
            as_of_ms=1_700_000_000_000,
            regime="trend_high_vol",
            trend_strength=0.5,
            vol_percentile=0.9,
            reference_universe=("BTC/USDT",),
        )

        fused = fusion.fuse([intent], regime)
        # Weight should be unchanged (no regime gating for strategies without overrides)
        assert abs(fused.intents[0].target_weight - 0.5) < 1e-6

    def test_range_low_vol_quarter_exposure(self):
        """range_low_vol should quarter exposure (multiplier 0.25) for strategies with overrides."""
        fusion = RegimeGatedFusion(
            strategy_overrides={
                "mean_reversion_v0": {
                    "range_low_vol": 0.25,
                }
            }
        )
        intent = _make_intent("LONG", 0.5, strategy_name="mean_reversion_v0")

        from crypto_bot.portfolio.models import RegimeSnapshot
        regime = RegimeSnapshot(
            as_of_ms=1_700_000_000_000,
            regime="range_low_vol",
            trend_strength=-0.5,
            vol_percentile=0.5,
            reference_universe=("BTC/USDT",),
        )

        fused = fusion.fuse([intent], regime)
        # Weight should be quartered (multiplier 0.25 for range_low_vol with override)
        assert abs(fused.intents[0].target_weight - 0.125) < 1e-6

    def test_range_low_vol_no_gating_for_strategies_without_overrides(self):
        """Strategies without overrides get full exposure in range_low_vol."""
        fusion = RegimeGatedFusion()
        intent = _make_intent("LONG", 0.5, strategy_name="cross_sectional_momentum_v0")

        from crypto_bot.portfolio.models import RegimeSnapshot
        regime = RegimeSnapshot(
            as_of_ms=1_700_000_000_000,
            regime="range_low_vol",
            trend_strength=-0.5,
            vol_percentile=0.5,
            reference_universe=("BTC/USDT",),
        )

        fused = fusion.fuse([intent], regime)
        # Weight should be unchanged (no regime gating for strategies without overrides)
        assert abs(fused.intents[0].target_weight - 0.5) < 1e-6

    def test_range_high_vol_zero_exposure(self):
        """range_high_vol should zero exposure (multiplier 0.0) -> position excluded for strategies with overrides."""
        fusion = RegimeGatedFusion(
            strategy_overrides={
                "mean_reversion_v0": {
                    "range_high_vol": 0.0,
                }
            }
        )
        intent = _make_intent("LONG", 0.5, strategy_name="mean_reversion_v0")

        from crypto_bot.portfolio.models import RegimeSnapshot
        regime = RegimeSnapshot(
            as_of_ms=1_700_000_000_000,
            regime="range_high_vol",
            trend_strength=-0.5,
            vol_percentile=0.9,
            reference_universe=("BTC/USDT",),
        )

        fused = fusion.fuse([intent], regime)
        # With multiplier 0.0, the position should be excluded entirely
        assert len(fused.intents) == 0

    def test_range_high_vol_no_gating_for_strategies_without_overrides(self):
        """Strategies without overrides get full exposure in range_high_vol."""
        fusion = RegimeGatedFusion()
        intent = _make_intent("LONG", 0.5, strategy_name="cross_sectional_momentum_v0")

        from crypto_bot.portfolio.models import RegimeSnapshot
        regime = RegimeSnapshot(
            as_of_ms=1_700_000_000_000,
            regime="range_high_vol",
            trend_strength=-0.5,
            vol_percentile=0.9,
            reference_universe=("BTC/USDT",),
        )

        fused = fusion.fuse([intent], regime)
        # Weight should be unchanged (no regime gating for strategies without overrides)
        assert abs(fused.intents[0].target_weight - 0.5) < 1e-6

    def test_short_positions_scaled_same_as_long(self):
        """Short positions should be scaled with same multipliers as longs."""
        fusion = RegimeGatedFusion(
            strategy_overrides={
                "mean_reversion_v0": {
                    "trend_high_vol": 0.5,
                }
            }
        )

        # Test long in trend_high_vol with override
        intent_long = _make_intent("LONG", 0.5, strategy_name="mean_reversion_v0")
        from crypto_bot.portfolio.models import RegimeSnapshot
        regime_high = RegimeSnapshot(
            as_of_ms=1_700_000_000_000,
            regime="trend_high_vol",
            trend_strength=0.5,
            vol_percentile=0.9,
            reference_universe=("BTC/USDT",),
        )
        fused_long = RegimeGatedFusion(
            strategy_overrides={
                "mean_reversion_v0": {
                    "trend_high_vol": 0.5,
                }
            }
        ).fuse([intent_long], regime_high)

        # Test short in trend_high_vol with override
        intent_short = _make_intent("SHORT", 0.5, strategy_name="mean_reversion_v0")
        fused_short = RegimeGatedFusion(
            strategy_overrides={
                "mean_reversion_v0": {
                    "trend_high_vol": 0.5,
                }
            }
        ).fuse([intent_short], regime_high)

        # Both should be scaled by same multiplier (0.5 for MR with override)
        assert abs(fused_long.intents[0].target_weight - 0.25) < 1e-6
        assert abs(fused_short.intents[0].target_weight - 0.25) < 1e-6

        # Test short in trend_high_vol without override (should be 1.0 for CSM)
        intent_short_no_override = _make_intent("SHORT", 0.5, strategy_name="cross_sectional_momentum_v0")
        fused_short_no_override = RegimeGatedFusion().fuse([intent_short_no_override], regime_high)
        assert abs(fused_short_no_override.intents[0].target_weight - 0.5) < 1e-6


    def test_per_strategy_override(self):
        """Per-strategy overrides should be used instead of defaults."""
        # Default: trend_low_vol = 1.0, range_low_vol = 0.25
        # Override for mean_reversion_v0: trend_low_vol = 0.25, range_low_vol = 1.0
        fusion = RegimeGatedFusion(
            strategy_overrides={
                "mean_reversion_v0": {
                    "trend_low_vol": 0.25,
                    "trend_high_vol": 0.0,
                    "range_low_vol": 1.0,
                    "range_high_vol": 0.5,
                }
            }
        )
        intent = _make_intent("LONG", 0.5, strategy_name="mean_reversion_v0")

        # In trend_low_vol, mean_reversion should get 0.25 (not 1.0)
        from crypto_bot.portfolio.models import RegimeSnapshot
        regime_trend = RegimeSnapshot(
            as_of_ms=1_700_000_000_000,
            regime="trend_low_vol",
            trend_strength=0.5,
            vol_percentile=0.5,
            reference_universe=("BTC/USDT",),
        )
        fused = fusion.fuse([intent], regime_trend)
        assert abs(fused.intents[0].target_weight - 0.125) < 1e-6  # 0.5 * 0.25

        # In range_low_vol, mean_reversion should get 1.0 (not 0.25)
        regime_range = RegimeSnapshot(
            as_of_ms=1_700_000_000_000,
            regime="range_low_vol",
            trend_strength=-0.5,
            vol_percentile=0.5,
            reference_universe=("BTC/USDT",),
        )
        fused2 = fusion.fuse([intent], regime_range)
        assert abs(fused2.intents[0].target_weight - 0.5) < 1e-6  # 0.5 * 1.0

    def test_fallback_to_defaults_when_no_override(self):
        """Strategies without overrides should get 1.0 multiplier (no regime gating)."""
        fusion = RegimeGatedFusion(
            strategy_overrides={
                "mean_reversion_v0": {
                    "trend_low_vol": 0.25,
                }
            }
        )
        intent = _make_intent("LONG", 0.5, strategy_name="cross_sectional_momentum_v0")

        from crypto_bot.portfolio.models import RegimeSnapshot
        regime_trend = RegimeSnapshot(
            as_of_ms=1_700_000_000_000,
            regime="trend_low_vol",
            trend_strength=0.5,
            vol_percentile=0.5,
            reference_universe=("BTC/USDT",),
        )
        fused = fusion.fuse([intent], regime_trend)
        assert abs(fused.intents[0].target_weight - 0.5) < 1e-6  # 0.5 * 1.0 (no override for CSM)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])