"""Cross-sectional market snapshot API (task 8).

Guarantees under test:
  * same timestamp  — one ``as_of_ms`` anchor for the whole cross-section
  * same timeframe  — a snapshot never mixes timeframes
  * closed bars only — ``open + period <= as_of_ms`` is a hard invariant
  * future bars are inaccessible — both by slicing and by validation
"""
from __future__ import annotations

import pytest

from crypto_bot.core.exceptions import InsufficientDataError
from crypto_bot.core.types import Candle
from crypto_bot.portfolio import (
    MarketSnapshot,
    UniverseSnapshot,
    get_market_snapshot,
    get_universe_snapshot,
)
from crypto_bot.simulation.historical_source import HistoricalCandleSource

PERIOD_MS = 3_600_000
BASE_TS = 1_700_000_000_000


def _candles(n: int, *, start_ms: int = BASE_TS) -> list[Candle]:
    price = 100.0
    out: list[Candle] = []
    for i in range(n):
        ts = start_ms + i * PERIOD_MS
        out.append(Candle(
            timestamp=ts,
            open=price,
            high=price * 1.01,
            low=price * 0.99,
            close=price * 1.001,
            volume=1000.0,
        ))
        price *= 1.001
    return out


def _source(symbol_candles: dict[str, list[Candle]]) -> HistoricalCandleSource:
    source = HistoricalCandleSource()
    for symbol, candles in symbol_candles.items():
        source.load_all(symbol, "1h", candles)
    return source


class TestUniverseSnapshot:
    def test_keeps_only_symbols_with_enough_closed_bars(self) -> None:
        source = _source({
            "BTC/USDT": _candles(50),
            "ETH/USDT": _candles(2),
        })
        as_of = BASE_TS + 50 * PERIOD_MS

        universe = get_universe_snapshot(
            source, ["BTC/USDT", "ETH/USDT"], "1h",
            as_of, min_closed_bars=5,
        )

        assert universe.symbols == ("BTC/USDT",)
        assert universe.as_of_ms == as_of
        assert universe.quote_currency == "USDT"

    def test_as_of_anchors_the_universe(self) -> None:
        source = _source({"BTC/USDT": _candles(30)})
        universe = get_universe_snapshot(
            source, ["BTC/USDT"], "1h", BASE_TS + 30 * PERIOD_MS,
        )
        assert universe.as_of_ms == BASE_TS + 30 * PERIOD_MS

    def test_empty_universe_raises(self) -> None:
        source = _source({"BTC/USDT": _candles(2)})
        with pytest.raises(InsufficientDataError):
            get_universe_snapshot(
                source, ["BTC/USDT"], "1h",
                BASE_TS + 10 * PERIOD_MS, min_closed_bars=5,
            )

    def test_rejects_bad_arguments(self) -> None:
        source = _source({"BTC/USDT": _candles(10)})
        as_of = BASE_TS + 10 * PERIOD_MS
        with pytest.raises(ValueError):
            get_universe_snapshot(source, [], "1h", as_of)
        with pytest.raises(ValueError):
            get_universe_snapshot(source, ["BTC/USDT"], "", as_of)
        with pytest.raises(ValueError):
            get_universe_snapshot(source, ["BTC/USDT"], "1h", -1)


class TestMarketSnapshot:
    def test_same_timestamp_across_all_symbols(self) -> None:
        source = _source({
            "BTC/USDT": _candles(50),
            "ETH/USDT": _candles(10),
        })
        anchor = BASE_TS + 50 * PERIOD_MS
        universe = UniverseSnapshot(as_of_ms=anchor, symbols=("BTC/USDT", "ETH/USDT"))

        snapshot = get_market_snapshot(source, universe, "1h")

        assert snapshot.as_of_ms == anchor
        assert snapshot.timeframe == "1h"
        assert set(snapshot.candles_by_symbol) == {"BTC/USDT", "ETH/USDT"}
        # No symbol may see a bar newer than the anchor, even though BTC has
        # more data than ETH — the visibility horizon is identical.
        for bars in snapshot.candles_by_symbol.values():
            assert all(c.timestamp + PERIOD_MS <= anchor for c in bars)
        assert snapshot.candles_by_symbol["BTC/USDT"][-1].timestamp == BASE_TS + 49 * PERIOD_MS
        assert snapshot.candles_by_symbol["ETH/USDT"][-1].timestamp == BASE_TS + 9 * PERIOD_MS

    def test_as_of_must_match_universe_timestamp(self) -> None:
        source = _source({"BTC/USDT": _candles(10)})
        universe = UniverseSnapshot(
            as_of_ms=BASE_TS + 10 * PERIOD_MS, symbols=("BTC/USDT",)
        )
        with pytest.raises(ValueError, match="as_of_ms"):
            get_market_snapshot(source, universe, "1h", as_of_ms=BASE_TS + 9 * PERIOD_MS)

    def test_closed_bars_only_mid_bar_anchor(self) -> None:
        source = _source({"BTC/USDT": _candles(10)})
        # Anchor lands in the middle of bar 5's period: bar 5 must be excluded.
        anchor = BASE_TS + 5 * PERIOD_MS + PERIOD_MS // 2
        universe = UniverseSnapshot(as_of_ms=anchor, symbols=("BTC/USDT",))

        snapshot = get_market_snapshot(source, universe, "1h")

        assert snapshot.candles_by_symbol["BTC/USDT"][-1].timestamp == BASE_TS + 4 * PERIOD_MS
        assert snapshot.as_of_ms == anchor

    def test_snapshot_dto_rejects_unclosed_bars(self) -> None:
        bars = _candles(3)
        future_bar = bars[-1]
        assert future_bar.timestamp + PERIOD_MS > BASE_TS + 2 * PERIOD_MS + PERIOD_MS // 2
        with pytest.raises(ValueError, match="future bars are inaccessible"):
            MarketSnapshot(
                as_of_ms=BASE_TS + 2 * PERIOD_MS + PERIOD_MS // 2,
                timeframe="1h",
                candles_by_symbol={"BTC/USDT": (bars[0], bars[1], future_bar)},
            )

    def test_snapshot_dto_rejects_mixed_unsorted_bars(self) -> None:
        bars = _candles(3)
        with pytest.raises(ValueError):
            MarketSnapshot(
                as_of_ms=BASE_TS + 3 * PERIOD_MS,
                timeframe="1h",
                candles_by_symbol={"BTC/USDT": (bars[1], bars[0], bars[2])},
            )
        with pytest.raises(ValueError):
            MarketSnapshot(
                as_of_ms=BASE_TS + 3 * PERIOD_MS,
                timeframe="1h",
                candles_by_symbol={"BTC/USDT": (bars[0], bars[0])},
            )

    def test_insufficient_symbol_data_raises(self) -> None:
        source = _source({"BTC/USDT": _candles(10)})
        universe = UniverseSnapshot(as_of_ms=BASE_TS + 10 * PERIOD_MS, symbols=("BTC/USDT",))
        with pytest.raises(InsufficientDataError):
            get_market_snapshot(source, universe, "1h", min_closed_bars=50)


class TestFutureBarsAreInaccessible:
    def test_snapshot_taken_in_the_past_never_sees_later_bars(self) -> None:
        source = _source({"BTC/USDT": _candles(10)})
        anchor = BASE_TS + 3 * PERIOD_MS
        universe = UniverseSnapshot(as_of_ms=anchor, symbols=("BTC/USDT",))

        snapshot = get_market_snapshot(source, universe, "1h")

        assert [c.timestamp for c in snapshot.candles_by_symbol["BTC/USDT"]] == [
            BASE_TS + i * PERIOD_MS for i in range(3)
        ]

    def test_historical_source_slice_never_leaks_a_forming_bar(self) -> None:
        source = _source({"BTC/USDT": _candles(10)})
        mid_bar = BASE_TS + 7 * PERIOD_MS + PERIOD_MS // 3

        bars = source.slice(mid_bar, "BTC/USDT", "1h", limit=400)

        assert bars[-1].timestamp == BASE_TS + 6 * PERIOD_MS

    def test_source_returning_an_unclosed_bar_is_rejected(self) -> None:
        class _LeakySource:
            """A source that violates the closed-bars contract on purpose."""

            def slice(self, as_of_ms, symbol, timeframe, limit=400) -> list[Candle]:
                return _candles(3)[-1:]

        anchor = BASE_TS + 2 * PERIOD_MS + PERIOD_MS // 2
        universe = UniverseSnapshot(as_of_ms=anchor, symbols=("BTC/USDT",))
        with pytest.raises(ValueError, match="future bars are inaccessible"):
            get_market_snapshot(_LeakySource(), universe, "1h")
