"""Market context for enriching FeatureSet beyond OHLCV indicators."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class SymbolMarketContext:
    """Exchange microstructure snapshot for one symbol."""

    quote_volume_24h: float = 0.0
    spread_pct: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    last: float = 0.0


def quote_volume_from_ticker(ticker: dict[str, Any]) -> float:
    """Extract 24h quote volume from a ccxt ticker dict."""
    value = ticker.get("quoteVolume")
    if value is None:
        base_volume = ticker.get("baseVolume")
        last = ticker.get("last")
        value = (
            float(base_volume) * float(last)
            if base_volume is not None and last is not None
            else 0.0
        )
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def market_context_from_ticker(ticker: dict[str, Any]) -> SymbolMarketContext:
    """Build ``SymbolMarketContext`` from a ccxt ticker."""
    bid = float(ticker.get("bid") or 0.0)
    ask = float(ticker.get("ask") or 0.0)
    last = float(ticker.get("last") or 0.0)
    mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else last
    spread_pct = ((ask - bid) / mid * 100.0) if mid > 0 and ask >= bid else 0.0
    return SymbolMarketContext(
        quote_volume_24h=quote_volume_from_ticker(ticker),
        spread_pct=spread_pct,
        bid=bid,
        ask=ask,
        last=last,
    )


def liquidity_score(quote_volume_24h: float, min_quote_volume: float) -> float:
    """Map quote volume to [0, 1] relative to the configured minimum."""
    if quote_volume_24h <= 0 or min_quote_volume <= 0:
        return 0.0
    ratio = quote_volume_24h / min_quote_volume
    return min(1.0, ratio / 3.0)
