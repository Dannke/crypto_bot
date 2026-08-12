"""Features layer: raw candles -> normalised FeatureSet.

This is the bridge between raw market data and the strategy layer. It:
  * computes raw indicators per timeframe,
  * reduces them to per-timeframe sub-scores in [0, 1],
  * packs them into a ``FeatureSet`` the strategy layer consumes.

Important: this module imports indicators (pure) and value types only. It must
not touch I/O (exchange, storage, logging) so it can be unit-tested instantly
and reused by the backtester.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from ..core.exceptions import InsufficientDataError
from ..core.types import Candle, FeatureSet
from ..indicators import adx, atr_pct, bollinger_position, ema_cross_state, rsi, volume_spike_ratio
from ..indicators.bollinger import bollinger_bands
from .context import SymbolMarketContext, liquidity_score


# --------------------------------------------------------------------------- #
# Raw metrics (one per timeframe)
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class RawMetrics:
    """Snapshot of raw indicator values for one timeframe."""

    timeframe: str
    adx: float
    rsi: float
    atr_pct: float
    ema_state: str        # "bull" | "bear" | "mixed"
    ema_fast: float
    ema_mid: float
    ema_slow: float
    bb_pos: float         # Bollinger position [~0..1]
    vol_spike: float      # volume / volume_ma


def _arrays(candles: list[Candle]) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Extract OHLCV columns as float64 arrays in one pass (backtest hot path)."""
    n = len(candles)
    open_a = np.empty(n, dtype="float64")
    high_a = np.empty(n, dtype="float64")
    low_a = np.empty(n, dtype="float64")
    close_a = np.empty(n, dtype="float64")
    vol_a = np.empty(n, dtype="float64")
    for i, c in enumerate(candles):
        open_a[i] = c.open
        high_a[i] = c.high
        low_a[i] = c.low
        close_a[i] = c.close
        vol_a[i] = c.volume
    return open_a, high_a, low_a, close_a, vol_a


def compute_raw_metrics(
    candles: list[Candle],
    timeframe: str,
    p_trend_ema: tuple[int, int, int],
    p_mom_rsi: int,
    p_vol_atr: int,
    p_vol_bb: tuple[int, float],
    p_volma: int,
) -> RawMetrics:
    """Compute raw indicators for a single timeframe's candle slice.

    Raises ``InsufficientDataError`` if there aren't enough bars even for the
    shortest indicator — callers treat that as "skip symbol".
    """
    n = len(candles)
    min_needed = max(
        p_trend_ema[2],
        p_mom_rsi + 1,
        (2 * p_vol_atr) + 1,
        p_vol_bb[0],
        p_volma,
    )
    if n < min_needed:
        raise InsufficientDataError(
            f"need >= {min_needed} candles for tf={timeframe}, got {n}"
        )

    _, high, low, close, volume = _arrays(candles)
    ema_st = ema_cross_state(close, *p_trend_ema)
    adx_series = adx(high, low, close, period=p_vol_atr)
    rsi_series = rsi(close, period=p_mom_rsi)
    atr_series = atr_pct(high, low, close, period=p_vol_atr)
    bb_pos_series = bollinger_position(close, period=p_vol_bb[0], std=p_vol_bb[1])
    vol_series = volume_spike_ratio(volume, period=p_volma)

    def _last(s: pd.Series) -> float:
        return float(s.iloc[-1]) if len(s) and not np.isnan(s.iloc[-1]) else float("nan")

    return RawMetrics(
        timeframe=timeframe,
        adx=_last(adx_series),
        rsi=_last(rsi_series),
        atr_pct=_last(atr_series),
        ema_state=ema_st.stack.value,
        ema_fast=ema_st.fast,
        ema_mid=ema_st.mid,
        ema_slow=ema_st.slow,
        bb_pos=_last(bb_pos_series),
        vol_spike=_last(vol_series),
    )


# --------------------------------------------------------------------------- #
# Normalisation to [0, 1] sub-scores
# --------------------------------------------------------------------------- #
def _norm_trend(m: RawMetrics, adx_min: float) -> float:
    """Strong, aligned trend -> high score. Choppy/mixed -> low."""
    if m.adx != m.adx or m.ema_state == "mixed":
        return 0.0
    # ADX strength: linear up to adx_min..(adx_min+25); cap at 1.
    strength = max(0.0, min(1.0, (m.adx - adx_min) / 25.0))
    return strength if m.ema_state in ("bull", "bear") else 0.0


def _norm_momentum(m: RawMetrics) -> float:
    """RSI distance from the 50 midline, mapped to [0, 1].

    RSI=50 -> 0 (no momentum); RSI at the extremes (0 or 100) -> ~1. The sign
    of the deviation (overbought vs oversold) is interpreted by the signal
    engine, not here.
    """
    if m.rsi != m.rsi:
        return 0.0
    return max(0.0, min(1.0, abs(m.rsi - 50.0) / 50.0))


def _norm_volatility(m: RawMetrics, lo: float, hi: float) -> float:
    """Reward ATR% inside [lo, hi]; penalise outside (dead or explosive)."""
    if m.atr_pct != m.atr_pct:
        return 0.0
    if m.atr_pct < lo or m.atr_pct > hi:
        return 0.0
    # Peak near the middle of the band.
    mid = (lo + hi) / 2.0
    half = max(1e-9, (hi - lo) / 2.0)
    return max(0.0, 1.0 - abs(m.atr_pct - mid) / half)


def _norm_volume(m: RawMetrics, spike_ratio: float) -> float:
    """Volume confirmation: spike_ratio..2x -> 0.5..1; below spike_ratio -> low."""
    if m.vol_spike != m.vol_spike:
        return 0.0
    if m.vol_spike < spike_ratio:
        # Still map smoothly: 1.0 baseline -> 0.2 floor so flat volume isn't zero.
        return max(0.0, 0.2 * (m.vol_spike / spike_ratio))
    return min(1.0, 0.5 + (m.vol_spike - spike_ratio) / spike_ratio)


def _infer_market_regime(m: RawMetrics, adx_min: float) -> str:
    if m.adx < adx_min * 0.75 or m.ema_state == "mixed":
        return "choppy"
    if m.ema_state == "bull":
        return "bull"
    if m.ema_state == "bear":
        return "bear"
    return "neutral"


def _bb_levels(candles: list[Candle], period: int, std: float) -> tuple[float, float, float]:
    close = _arrays(candles)[3]
    bb = bollinger_bands(close, period, std)
    return bb.last()


# --------------------------------------------------------------------------- #
# Builder
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class FeatureBuilderParams:
    ema: tuple[int, int, int]
    rsi_period: int
    atr_period: int
    bb: tuple[int, float]
    volma_period: int
    adx_min: float
    atr_lo_pct: float | dict[str, float]
    atr_hi_pct: float | dict[str, float]
    vol_spike_ratio: float
    min_quote_volume: float = 5_000_000.0


class FeatureBuilder:
    """Builds a normalised ``FeatureSet`` from per-timeframe candles."""

    def __init__(self, params: FeatureBuilderParams) -> None:
        self._p = params

    def _build_one(
        self,
        symbol: str,
        timeframe: str,
        candles: list[Candle],
        *,
        market: SymbolMarketContext | None = None,
        correlation_btc: float = 0.0,
        correlation_eth: float = 0.0,
    ) -> FeatureSet:
        m = compute_raw_metrics(
            candles, timeframe,
            self._p.ema, self._p.rsi_period, self._p.atr_period,
            self._p.bb, self._p.volma_period,
        )
        last = candles[-1]
        bb_upper, bb_mid, bb_lower = _bb_levels(candles, self._p.bb[0], self._p.bb[1])
        ctx = market or SymbolMarketContext()
        liq = liquidity_score(ctx.quote_volume_24h, self._p.min_quote_volume)
        lo = self._p.atr_lo_pct[timeframe] if isinstance(self._p.atr_lo_pct, dict) else self._p.atr_lo_pct
        hi = self._p.atr_hi_pct[timeframe] if isinstance(self._p.atr_hi_pct, dict) else self._p.atr_hi_pct
        return FeatureSet(
            symbol=symbol,
            timeframe=timeframe,
            trend_score=_norm_trend(m, self._p.adx_min),
            momentum_score=_norm_momentum(m),
            volatility_score=_norm_volatility(m, lo, hi),
            volume_score=_norm_volume(m, self._p.vol_spike_ratio),
            adx=m.adx,
            rsi=m.rsi,
            atr_pct=m.atr_pct,
            ema_fast=m.ema_fast,
            ema_mid=m.ema_mid,
            ema_slow=m.ema_slow,
            bb_upper=bb_upper,
            bb_mid=bb_mid,
            bb_lower=bb_lower,
            bb_position=m.bb_pos,
            candle_timestamp_ms=last.timestamp,
            liquidity_score=liq,
            spread_pct=ctx.spread_pct,
            correlation_btc=correlation_btc,
            correlation_eth=correlation_eth,
            market_regime=_infer_market_regime(m, self._p.adx_min),
            open=last.open,
            high=last.high,
            low=last.low,
            close=last.close,
            volume=last.volume,
            extras={
                "last_close": last.close,
                "ema_state": float(m.ema_state == "bull") - float(m.ema_state == "bear"),
                "bb_pos": m.bb_pos,
                "vol_spike": m.vol_spike,
                "quote_volume_24h": ctx.quote_volume_24h,
            },
        )

    def build(
        self,
        symbol: str,
        by_timeframe: dict[str, list[Candle]],
        *,
        market: SymbolMarketContext | None = None,
        correlation_btc: float = 0.0,
        correlation_eth: float = 0.0,
    ) -> FeatureSet:
        if not by_timeframe:
            raise InsufficientDataError("no timeframes provided")
        trigger_tf = next(iter(by_timeframe))
        return self._build_one(
            symbol,
            trigger_tf,
            by_timeframe[trigger_tf],
            market=market,
            correlation_btc=correlation_btc,
            correlation_eth=correlation_eth,
        )

    def build_all(
        self,
        symbol: str,
        by_timeframe: dict[str, list[Candle]],
        *,
        market: SymbolMarketContext | None = None,
        correlation_btc: float = 0.0,
        correlation_eth: float = 0.0,
    ) -> dict[str, FeatureSet]:
        """Build one FeatureSet per timeframe, preserving input order."""
        if not by_timeframe:
            raise InsufficientDataError("no timeframes provided")
        return {
            timeframe: self._build_one(
                symbol,
                timeframe,
                candles,
                market=market,
                correlation_btc=correlation_btc,
                correlation_eth=correlation_eth,
            )
            for timeframe, candles in by_timeframe.items()
        }


def builder_from_settings(settings: Any) -> FeatureBuilder:
    """Factory: build a FeatureBuilder from a ``Settings`` instance.

    ``settings`` is typed as ``object`` to keep this module free of a config
    import (and therefore free of I/O). The orchestrator passes a real Settings.
    """
    s = settings
    strat = s.strategy
    params = FeatureBuilderParams(
        ema=(strat.trend.ema_fast, strat.trend.ema_mid, strat.trend.ema_slow),
        rsi_period=strat.momentum.rsi_period,
        atr_period=strat.volatility.atr_period,
        bb=(strat.volatility.bb_period, strat.volatility.bb_std),
        volma_period=strat.volume.ma_period,
        adx_min=strat.trend.adx_min,
        atr_lo_pct=strat.volatility.atr_min_pct,
        atr_hi_pct=strat.volatility.atr_max_pct,
        vol_spike_ratio=strat.volume.spike_ratio,
        min_quote_volume=s.filters.min_quote_volume_usd,
    )
    return FeatureBuilder(params)
