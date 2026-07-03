"""Batch feature generation for multi-symbol scans."""
from __future__ import annotations

import numpy as np

from ..core.exceptions import InsufficientDataError
from ..core.types import Candle
from ..core.types import FeatureSet
from .builder import FeatureBuilder
from .context import SymbolMarketContext


def return_correlation(
    closes_a: list[float],
    closes_b: list[float],
    window: int = 30,
) -> float:
    """Pearson correlation of pct returns over the last ``window`` bars."""
    n = min(window, len(closes_a), len(closes_b))
    if n < 5:
        return 0.0
    a = np.array(closes_a[-n:], dtype="float64")
    b = np.array(closes_b[-n:], dtype="float64")
    ret_a = np.diff(a) / np.maximum(a[:-1], 1e-12)
    ret_b = np.diff(b) / np.maximum(b[:-1], 1e-12)
    if len(ret_a) < 2:
        return 0.0
    # A flat (zero-variance) return series — common on illiquid testnet pairs
    # with a constant/near-constant price — makes np.corrcoef divide by a
    # zero stddev. The result is correctly NaN and we already handle that
    # below; we just don't want numpy's RuntimeWarning spamming the console
    # for what is an expected, handled condition rather than a real bug.
    with np.errstate(invalid="ignore", divide="ignore"):
        corr = np.corrcoef(ret_a, ret_b)[0, 1]
    return 0.0 if np.isnan(corr) else float(corr)


def _reference_closes(
    symbol_candles: dict[str, dict[str, list[Candle]]],
    reference_symbol: str,
    trigger_tf: str,
) -> list[float]:
    by_tf = symbol_candles.get(reference_symbol, {})
    candles = by_tf.get(trigger_tf, [])
    return [c.close for c in candles]


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
    btc_closes = _reference_closes(symbol_candles, btc_sym, tf)
    eth_closes = _reference_closes(symbol_candles, eth_sym, tf)

    out: dict[str, dict[str, FeatureSet]] = {}
    for symbol, by_timeframe in symbol_candles.items():
        candles = by_timeframe.get(tf, [])
        if not candles:
            continue
        symbol_closes = [c.close for c in candles]
        corr_btc = 1.0 if symbol == btc_sym else return_correlation(symbol_closes, btc_closes)
        corr_eth = 1.0 if symbol == eth_sym else return_correlation(symbol_closes, eth_closes)
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