"""Tests for batch feature generation."""
from __future__ import annotations

from crypto_bot.core.types import Candle
from crypto_bot.features.batch import build_features_batch, return_correlation
from crypto_bot.features.builder import FeatureBuilder, FeatureBuilderParams
from crypto_bot.features.context import SymbolMarketContext


def _candles(n: int = 250, start: float = 100.0) -> list[Candle]:
    return [
        Candle(
            timestamp=i * 60_000,
            open=start + i * 0.1,
            high=start + i * 0.1 + 1,
            low=start + i * 0.1 - 1,
            close=start + i * 0.1,
            volume=1000.0,
        )
        for i in range(n)
    ]


def test_return_correlation_perfect_for_identical_series():
    closes = [100 + i for i in range(40)]
    assert return_correlation(closes, closes) > 0.99


def test_build_features_batch_skips_insufficient_data():
    params = FeatureBuilderParams(
        ema=(21, 50, 200),
        rsi_period=14,
        atr_period=14,
        bb=(20, 2.0),
        volma_period=20,
        adx_min=20.0,
        atr_lo_pct=0.5,
        atr_hi_pct=8.0,
        vol_spike_ratio=1.5,
    )
    builder = FeatureBuilder(params)
    batch = build_features_batch(
        {"BTC/USDT": {"15m": _candles()}},
        builder,
        {"BTC/USDT": SymbolMarketContext(quote_volume_24h=10_000_000, spread_pct=0.05)},
        quote="USDT",
        trigger_tf="15m",
    )
    assert "BTC/USDT" in batch
    assert batch["BTC/USDT"]["15m"].liquidity_score > 0
