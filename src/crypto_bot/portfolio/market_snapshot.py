"""Cross-sectional market snapshot API.

Two functions serve as the read facade between a candle source and the
portfolio decision layer::

    get_universe_snapshot(...)  -> UniverseSnapshot
    get_market_snapshot(...)    -> MarketSnapshot

Every snapshot carries three structural guarantees, enforced by slicing and
validated by construction:

1. **Same timestamp** — the whole cross-section is anchored at one
   ``as_of_ms`` instant.  For every symbol only bars whose full period has
   elapsed by that instant are visible; no symbol can see a bar that is
   newer than the anchor, so look-ahead across the cross-section is
   impossible.
2. **Same timeframe** — a snapshot is built for exactly one timeframe; a
   ``MarketSnapshot`` never mixes bars from different timeframes.
3. **Closed bars only** — a bar is included iff
   ``timestamp + period_ms <= as_of_ms``.  Partially-formed bars are
   excluded by construction, and any source that violates this rule is
   rejected with an error.

``CandleSource`` is a structural protocol; ``HistoricalCandleSource``
implements it as-is.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol

from ..core.exceptions import InsufficientDataError
from ..core.policy import timeframe_to_seconds
from ..core.types import Candle
from .models import UniverseSnapshot


class CandleSource(Protocol):
    """Anything that can serve closed candles for one (symbol, timeframe).

    Structurally identical to ``HistoricalCandleSource.slice``: only bars
    with ``open + period <= as_of_ms`` may be returned.
    """

    def slice(
        self, as_of_ms: int, symbol: str, timeframe: str, limit: int = 400
    ) -> list[Candle]: ...


def _closed(bars: list[Candle], as_of_ms: int, period_ms: int) -> bool:
    return all(c.timestamp + period_ms <= as_of_ms for c in bars)


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    """Closed candles for every symbol of a universe at one instant."""

    as_of_ms: int
    timeframe: str
    candles_by_symbol: Mapping[str, tuple[Candle, ...]]

    def __post_init__(self) -> None:
        if isinstance(self.as_of_ms, bool) or not isinstance(self.as_of_ms, int) or self.as_of_ms < 0:
            raise ValueError("as_of_ms must be a non-negative integer timestamp in milliseconds")
        if not isinstance(self.timeframe, str) or not self.timeframe.strip():
            raise ValueError("timeframe must be a non-empty string")
        if not self.candles_by_symbol:
            raise ValueError("candles_by_symbol must not be empty")

        period_ms = timeframe_to_seconds(self.timeframe) * 1000
        candles = dict(self.candles_by_symbol)
        for symbol, bars in candles.items():
            if not isinstance(bars, (list, tuple)) or not bars:
                raise ValueError(f"candles_by_symbol[{symbol!r}] must be a non-empty sequence")
            if not all(isinstance(c, Candle) for c in bars):
                raise ValueError(f"candles_by_symbol[{symbol!r}] must contain Candle instances")
            if not _closed(bars, self.as_of_ms, period_ms):
                raise ValueError(
                    f"future bars are inaccessible: {symbol} contains a bar not closed by as_of_ms"
                )
            times = [c.timestamp for c in bars]
            if times != sorted(times):
                raise ValueError(f"candles_by_symbol[{symbol!r}] must be sorted by timestamp")
            if len(set(times)) != len(times):
                raise ValueError(f"candles_by_symbol[{symbol!r}] contains duplicate timestamps")
        object.__setattr__(self, "candles_by_symbol", MappingProxyType(candles))


def get_universe_snapshot(
    source: CandleSource,
    symbols: list[str] | tuple[str, ...],
    timeframe: str,
    as_of_ms: int,
    *,
    quote_currency: str = "USDT",
    min_closed_bars: int = 1,
    source_name: str | None = None,
) -> UniverseSnapshot:
    """Resolve which of ``symbols`` have at least ``min_closed_bars`` closed
    bars by ``as_of_ms``.

    The returned ``UniverseSnapshot`` is anchored at exactly ``as_of_ms`` —
    the same timestamp that ``get_market_snapshot`` will use — so the
    universe and its market data can never disagree about the observation
    instant.
    """
    if not isinstance(symbols, (list, tuple)) or not symbols:
        raise ValueError("symbols must be a non-empty sequence")
    if not isinstance(timeframe, str) or not timeframe.strip():
        raise ValueError("timeframe must be a non-empty string")
    if isinstance(as_of_ms, bool) or not isinstance(as_of_ms, int) or as_of_ms < 0:
        raise ValueError("as_of_ms must be a non-negative integer timestamp in milliseconds")
    if isinstance(min_closed_bars, bool) or not isinstance(min_closed_bars, int) or min_closed_bars < 1:
        raise ValueError("min_closed_bars must be a positive integer")

    kept: list[str] = []
    for symbol in symbols:
        bars = source.slice(as_of_ms, symbol, timeframe, limit=min_closed_bars)
        if len(bars) >= min_closed_bars:
            kept.append(symbol)
    if not kept:
        raise InsufficientDataError(
            f"no symbol has {min_closed_bars} closed bars at as_of_ms={as_of_ms} for {timeframe}"
        )
    return UniverseSnapshot(
        as_of_ms=as_of_ms,
        symbols=tuple(kept),
        quote_currency=quote_currency,
        source=source_name,
    )


def get_market_snapshot(
    source: CandleSource,
    universe: UniverseSnapshot,
    timeframe: str,
    *,
    as_of_ms: int | None = None,
    limit: int = 400,
    min_closed_bars: int = 1,
) -> MarketSnapshot:
    """Build the cross-sectional market snapshot for ``universe``.

    The snapshot instant is ``universe.as_of_ms`` (an explicit ``as_of_ms``
    must match it — one timestamp for the whole pipeline).  Every symbol of
    the universe must yield at least ``min_closed_bars`` bars closed by the
    anchor; anything less means the source and the universe disagree and
    raises ``InsufficientDataError``.
    """
    if not isinstance(universe, UniverseSnapshot):
        raise ValueError("universe must be a UniverseSnapshot")
    if not isinstance(timeframe, str) or not timeframe.strip():
        raise ValueError("timeframe must be a non-empty string")
    if as_of_ms is not None and as_of_ms != universe.as_of_ms:
        raise ValueError("as_of_ms must match universe.as_of_ms")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer")
    if isinstance(min_closed_bars, bool) or not isinstance(min_closed_bars, int) or min_closed_bars < 1:
        raise ValueError("min_closed_bars must be a positive integer")

    anchor_ms = universe.as_of_ms
    period_ms = timeframe_to_seconds(timeframe) * 1000
    candles_by_symbol: dict[str, tuple[Candle, ...]] = {}
    for symbol in universe.symbols:
        bars = source.slice(anchor_ms, symbol, timeframe, limit=limit)
        if len(bars) < min_closed_bars:
            raise InsufficientDataError(
                f"symbol {symbol} has {len(bars)} closed bars but {min_closed_bars} required "
                f"at as_of_ms={anchor_ms}"
            )
        if not _closed(bars, anchor_ms, period_ms):
            raise ValueError(
                f"future bars are inaccessible: source returned an unclosed bar for {symbol} "
                f"at as_of_ms={anchor_ms}"
            )
        candles_by_symbol[symbol] = tuple(bars)

    return MarketSnapshot(
        as_of_ms=anchor_ms,
        timeframe=timeframe,
        candles_by_symbol=candles_by_symbol,
    )
