"""Batch feature generation for multi-symbol scans."""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..core.exceptions import InsufficientDataError
from ..core.types import Candle, FeatureSet
from .builder import FeatureBuilder
from .context import SymbolMarketContext


def closes_by_timestamp(
    symbol_candles: dict[str, dict[str, list[Candle]]],
    symbol: str,
    timeframe: str,
) -> dict[int, float]:
    """Map {timestamp_ms: close} for a (symbol, timeframe) pair."""
    by_tf = symbol_candles.get(symbol, {})
    candles = by_tf.get(timeframe, [])
    return {c.timestamp: c.close for c in candles}


def aligned_correlation(
    closes_a: dict[int, float],
    closes_b: dict[int, float],
    window: int = 30,
) -> float:
    """Pearson correlation on timestamp-aligned close returns.

    Builds a DataFrame from the two timestamp->close dicts, aligns on common
    timestamps (inner join), and computes return correlation over the last
    ``window`` aligned observations.

    This fixes the index-alignment bug in the original ``return_correlation``:
    when BTC has 400 candles and an altcoin has 80, the previous code took the
    last N elements by list index position — which compared BTC closes from a
    completely different time window to the altcoin's closes.
    """
    df = pd.DataFrame({"a": pd.Series(closes_a), "b": pd.Series(closes_b)})
    df = df.dropna()  # keep only timestamps present in BOTH series
    if len(df) < 6:  # need at least 5 returns after diff
        return 0.0

    n = min(window, len(df))
    a = np.asarray(df["a"].tail(n), dtype="float64")
    b = np.asarray(df["b"].tail(n), dtype="float64")
    ret_a = np.diff(a) / np.maximum(a[:-1], 1e-12)
    ret_b = np.diff(b) / np.maximum(b[:-1], 1e-12)
    if len(ret_a) < 2:
        return 0.0

    with np.errstate(invalid="ignore", divide="ignore"):
        corr = np.corrcoef(ret_a, ret_b)[0, 1]
    return 0.0 if np.isnan(corr) else float(corr)


def build_features_batch(
    symbol_candles: dict[str, dict[str, list[Candle]]],
    builder: FeatureBuilder,
    market_by_symbol: dict[str, SymbolMarketContext],
    *,
    quote: str = "USDT",
    trigger_tf: str | None = None,
) -> dict[str, dict[str, FeatureSet]]:
    """Build feature sets for many symbols, including cross-asset correlations."""
    if not symbol_candles:
        return {}

    first_symbol = next(iter(symbol_candles))
    tf = trigger_tf or next(iter(symbol_candles[first_symbol]))
    btc_sym = f"BTC/{quote}"
    eth_sym = f"ETH/{quote}"
    btc_closes = closes_by_timestamp(symbol_candles, btc_sym, tf)
    eth_closes = closes_by_timestamp(symbol_candles, eth_sym, tf)

    out: dict[str, dict[str, FeatureSet]] = {}
    for symbol, by_timeframe in symbol_candles.items():
        candles = by_timeframe.get(tf, [])
        if not candles:
            continue
        symbol_closes = closes_by_timestamp(symbol_candles, symbol, tf)
        corr_btc = 1.0 if symbol == btc_sym else aligned_correlation(symbol_closes, btc_closes)
        corr_eth = 1.0 if symbol == eth_sym else aligned_correlation(symbol_closes, eth_closes)
        market = market_by_symbol.get(symbol, SymbolMarketContext())
        try:
            out[symbol] = builder.build_all(
                symbol,
                by_timeframe,
                market=market,
                correlation_btc=corr_btc,
                correlation_eth=corr_eth,
            )
        except InsufficientDataError:
            continue
    return out