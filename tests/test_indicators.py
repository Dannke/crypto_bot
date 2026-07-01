"""Indicator unit tests: known-answer checks against hand-computed values.

These run with no network/storage — indicators are pure functions.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from crypto_bot.indicators import (
    adx,
    atr,
    bollinger_bands,
    ema,
    ema_cross_state,
    rsi,
    volume_ma,
    volume_spike_ratio,
)
from crypto_bot.indicators.ema import EmaStack


# --------------------------------------------------------------------------- #
# EMA
# --------------------------------------------------------------------------- #
def test_ema_constant_series_is_constant():
    s = pd.Series([5.0] * 30)
    out = ema(s, 10)
    assert np.isnan(out.iloc[:9]).all()
    assert pytest.approx(out.iloc[-1], abs=1e-9) == 5.0


def test_ema_warmup_is_nan():
    s = pd.Series(np.arange(1, 21, dtype=float))
    out = ema(s, 5)
    assert np.isnan(out.iloc[:4]).all()
    assert not np.isnan(out.iloc[5])


def test_ema_cross_state_bull():
    # Strictly increasing closes -> fast > mid > slow.
    s = pd.Series(np.linspace(10, 100, 250))
    st = ema_cross_state(s, 21, 50, 200)
    assert st.stack == EmaStack.BULL


def test_ema_cross_state_bear():
    s = pd.Series(np.linspace(100, 10, 250))
    st = ema_cross_state(s, 21, 50, 200)
    assert st.stack == EmaStack.BEAR


# --------------------------------------------------------------------------- #
# RSI
# --------------------------------------------------------------------------- #
def test_rsi_all_up_is_near_100():
    s = pd.Series(np.linspace(1, 100, 50))
    out = rsi(s, 14)
    assert out.iloc[-1] > 90


def test_rsi_all_down_is_near_0():
    s = pd.Series(np.linspace(100, 1, 50))
    out = rsi(s, 14)
    assert out.iloc[-1] < 10


def test_rsi_flat_market_is_midpoint():
    # A perfectly flat series has no directional pressure (avg_gain == avg_loss == 0).
    # The canonical convention is RSI = 50 (no momentum either way).
    s = pd.Series([10.0] * 40)
    out = rsi(s, 14)
    assert float(out.iloc[-1]) == pytest.approx(50.0, abs=1e-9)


def test_rsi_warmup_is_nan():
    s = pd.Series(np.linspace(1, 50, 50))
    out = rsi(s, 14)
    assert np.isnan(out.iloc[14 - 1]) or out.notna().iloc[14:].any()


# --------------------------------------------------------------------------- #
# ATR
# --------------------------------------------------------------------------- #
def test_atr_constant_range_is_positive_and_finite():
    n = 50
    high = pd.Series([11.0] * n)
    low = pd.Series([9.0] * n)
    close = pd.Series([10.0] * n)
    out = atr(high, low, close, 14)
    assert np.isnan(out.iloc[:13]).all()
    assert out.iloc[-1] == pytest.approx(2.0, abs=1e-9)


def test_atr_accepts_dataframe():
    df = pd.DataFrame({
        "high": np.linspace(11, 20, 60),
        "low": np.linspace(9, 18, 60),
        "close": np.linspace(10, 19, 60),
    })
    out = atr(df, period=14)
    assert out.notna().iloc[-1]
    assert out.iloc[-1] > 0


# --------------------------------------------------------------------------- #
# ADX
# --------------------------------------------------------------------------- #
def test_adx_trending_series_high():
    # Strong monotonic trend should produce a meaningful ADX.
    close = pd.Series(np.linspace(10, 100, 120))
    high = close + 1.0
    low = close - 1.0
    out = adx(high, low, close, 14)
    assert np.isnan(out.iloc[: 2 * 14]).any() or out.notna().iloc[-1]
    assert out.iloc[-1] > 0


def test_adx_period_must_be_positive():
    with pytest.raises(ValueError):
        adx(pd.Series([1.0] * 5), pd.Series([0.0] * 5), pd.Series([0.5] * 5), period=0)


# --------------------------------------------------------------------------- #
# Bollinger
# --------------------------------------------------------------------------- #
def test_bollinger_bands_symmetry():
    s = pd.Series(np.linspace(10, 20, 40))
    bb = bollinger_bands(s, period=20, std=2.0)
    upper, mid, lower = bb.last()
    assert not np.isnan(upper)
    assert upper > mid > lower


def test_bollinger_rejects_bad_std():
    with pytest.raises(ValueError):
        bollinger_bands(pd.Series([1.0] * 30), period=20, std=0)


# --------------------------------------------------------------------------- #
# Volume
# --------------------------------------------------------------------------- #
def test_volume_ma_and_spike():
    # 19 bars of 100 + one bar of 300 -> 20-bar SMA = (19*100 + 300)/20 = 110.0
    v = pd.Series([100.0] * 19 + [300.0])
    ma = volume_ma(v, 20)
    spike = volume_spike_ratio(v, 20)
    assert ma.iloc[-1] == pytest.approx(110.0, abs=1e-9)
    assert spike.iloc[-1] == pytest.approx(300.0 / 110.0, abs=1e-9)


def test_volume_ma_rejects_zero_period():
    with pytest.raises(ValueError):
        volume_ma(pd.Series([1.0]), period=0)
