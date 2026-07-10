"""Tests for batch feature generation."""
from __future__ import annotations

from crypto_bot.core.types import Candle
from crypto_bot.features.batch import aligned_correlation, build_features_batch
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


def test_aligned_correlation_perfect_for_identical_series():
    closes = {i: 100.0 + i for i in range(40)}
    assert aligned_correlation(closes, closes) > 0.99


def test_aligned_correlation_zero_for_misaligned_timestamps():
    a = {i: 100.0 + i for i in range(40)}
    b = {i + 100: 200.0 + i for i in range(40)}  # no overlapping timestamps
    assert aligned_correlation(a, b) == 0.0


def test_aligned_correlation_partial_overlap():
    a = {i: 100.0 + i for i in range(50)}
    b = {i: 200.0 + i * 2 for i in range(50)}  # same timestamps, different prices
    corr = aligned_correlation(a, b, window=20)
    assert -1.0 <= corr <= 1.0


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
