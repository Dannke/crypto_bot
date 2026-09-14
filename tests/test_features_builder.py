"""Feature builder tests.

Synthetic candle sequences with known trend/volatility behaviour, fed through
the builder, then assert the FeatureSet sub-scores are in range and behave
sensibly (uptrend -> high trend score; dead market -> low).
"""
from __future__ import annotations

import math

import pytest

from crypto_bot.core.exceptions import InsufficientDataError
from crypto_bot.core.types import Candle
from crypto_bot.features.builder import FeatureBuilder, FeatureBuilderParams

PARAMS = FeatureBuilderParams(
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


def _candles(closes, base_ts=0, step_ms=60_000, spread=0.5, vol=100.0):
    return [
        Candle(
            timestamp=base_ts + i * step_ms,
            open=c - spread,
            high=c + spread,
            low=c - spread,
            close=c,
            volume=vol,
        )
        for i, c in enumerate(closes)
    ]


def test_uptrend_yields_high_trend_score():
    closes = [10 + i * 0.4 for i in range(250)]  # steady uptrend
    candles = _candles(closes)
    fb = FeatureBuilder(PARAMS)
    fs = fb.build("BTC/USDT", {"15m": candles})
    assert fs.symbol == "BTC/USDT"
    assert 0.0 <= fs.trend_score <= 1.0
    assert fs.trend_score > 0.5  # strong aligned trend
    assert fs.extras["ema_state"] > 0  # bull
    assert fs.extras["last_close"] == closes[-1]


def test_build_all_returns_each_timeframe():
    closes = [10 + i * 0.4 for i in range(250)]
    candles = _candles(closes)
    fb = FeatureBuilder(PARAMS)
    built = fb.build_all("BTC/USDT", {"15m": candles, "1h": candles})
    assert list(built) == ["15m", "1h"]
    assert built["1h"].timeframe == "1h"


def test_downtrend_yields_bear_ema_state():
    closes = [100 - i * 0.4 for i in range(250)]
    candles = _candles(closes)
    fb = FeatureBuilder(PARAMS)
    fs = fb.build("ETH/USDT", {"15m": candles})
    assert fs.extras["ema_state"] < 0  # bear


def test_flat_market_low_trend_score():
    closes = [50.0] * 250  # no movement
    candles = _candles(closes)
    fb = FeatureBuilder(PARAMS)
    fs = fb.build("XRP/USDT", {"15m": candles})
    # No trend, low ADX -> trend score ~0
    assert fs.trend_score <= 0.1


def test_all_scores_bounded():
    closes = [math.sin(i / 5.0) * 5 + 50 for i in range(250)]
    candles = _candles(closes)
    fb = FeatureBuilder(PARAMS)
    fs = fb.build("SOL/USDT", {"15m": candles})
    for name in ("trend_score", "momentum_score", "volatility_score", "volume_score"):
        v = getattr(fs, name)
        assert 0.0 <= v <= 1.0, f"{name}={v} out of [0,1]"


def test_empty_candles_raise():
    fb = FeatureBuilder(PARAMS)
    with pytest.raises(InsufficientDataError):
        fb.build("ADA/USDT", {"15m": []})


def test_insufficient_bars_raise():
    fb = FeatureBuilder(PARAMS)
    candles = _candles([10, 11, 12, 13, 14])
    with pytest.raises(InsufficientDataError):
        fb.build("ADA/USDT", {"15m": candles})


def test_builder_populates_microstructure_fields():
    closes = [10 + i * 0.4 for i in range(250)]
    candles = _candles(closes)
    fb = FeatureBuilder(PARAMS)
    from crypto_bot.features.context import SymbolMarketContext

    fs = fb.build(
        "BTC/USDT",
        {"15m": candles},
        market=SymbolMarketContext(quote_volume_24h=15_000_000, spread_pct=0.08),
    )
    assert fs.close == closes[-1]
    assert fs.volume == candles[-1].volume
    assert fs.liquidity_score > 0
    assert fs.spread_pct == 0.08
    # R1: New 4-regime taxonomy
    assert fs.market_regime in ("trend_low_vol", "trend_high_vol", "range_low_vol", "range_high_vol")
    assert fs.bb_upper >= fs.bb_lower


def test_insufficient_slow_ema_warmup_raises():
    fb = FeatureBuilder(PARAMS)
    candles = _candles([10 + i * 0.1 for i in range(199)])
    with pytest.raises(InsufficientDataError, match="need >= 200"):
        fb.build("ADA/USDT", {"15m": candles})
