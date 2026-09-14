"""CSM research: universe snapshot semantics (task 11).

The universe is the symbol set the strategy may rank.  These tests pin the
contract: only symbols with enough closed history qualify, the universe and
the market snapshot share one anchor, and a symbol whose history disappears
breaks the snapshot instead of silently ranking from partial data.
"""
from __future__ import annotations

import pytest
from csm_helpers import (
    BASE_TS,
    FIVE_COIN_RETS,
    FIVE_COIN_SYMBOLS,
    LOOKBACK,
    MIN_BARS,
    PERIOD_MS,
    closes_snapshot,
    momentum,
    state,
)

from crypto_bot.core.exceptions import InsufficientDataError
from crypto_bot.portfolio import (
    get_market_snapshot,
    get_universe_snapshot,
)
from crypto_bot.simulation.historical_source import HistoricalCandleSource


def _source_with(
    rets: dict[str, float],
    n_bars_by_symbol: dict[str, int] | None = None,
) -> HistoricalCandleSource:
    from crypto_bot.core.types import Candle

    source = HistoricalCandleSource()
    for symbol, ret in rets.items():
        n = (n_bars_by_symbol or {}).get(symbol, MIN_BARS)
        closes = [100.0] * (n - 1) + [100.0 * (1.0 + ret)]
        source.load_all(
            symbol, "1h",
            [
                Candle(
                    timestamp=BASE_TS + i * PERIOD_MS,
                    open=100.0, high=101.0, low=99.0, close=c, volume=1000.0,
                )
                for i, c in enumerate(closes)
            ],
        )
    return source


class TestUniverseQualification:
    def test_short_history_symbol_is_dropped_from_the_universe(self) -> None:
        n_by_symbol = {s: MIN_BARS for s in FIVE_COIN_SYMBOLS}
        n_by_symbol["F/USDT"] = LOOKBACK  # not enough bars for any lookback+1
        source = _source_with({**FIVE_COIN_RETS, "F/USDT": 0.5}, n_by_symbol)
        universe = get_universe_snapshot(
            source, FIVE_COIN_SYMBOLS + ["F/USDT"], "1h",
            BASE_TS + MIN_BARS * PERIOD_MS, min_closed_bars=LOOKBACK + 1,
        )
        assert "F/USDT" not in universe.symbols
        assert universe.symbols == tuple(FIVE_COIN_SYMBOLS)

    def test_min_closed_bars_can_be_raised(self) -> None:
        source = _source_with(FIVE_COIN_RETS)
        anchor = BASE_TS + MIN_BARS * PERIOD_MS
        strict = get_universe_snapshot(source, FIVE_COIN_SYMBOLS, "1h", anchor, min_closed_bars=MIN_BARS)
        assert strict.symbols == tuple(FIVE_COIN_SYMBOLS)
        with pytest.raises(InsufficientDataError):
            get_universe_snapshot(source, FIVE_COIN_SYMBOLS, "1h", anchor, min_closed_bars=MIN_BARS + 1)

    def test_universe_with_no_qualified_symbol_raises(self) -> None:
        source = HistoricalCandleSource()
        with pytest.raises(InsufficientDataError):
            get_universe_snapshot(source, ["A/USDT"], "1h", BASE_TS, min_closed_bars=1)

    def test_universe_and_market_snapshot_share_one_anchor(self) -> None:
        snapshot_mkt = closes_snapshot({s: [100.0] * MIN_BARS for s in FIVE_COIN_SYMBOLS})
        assert snapshot_mkt.as_of_ms == BASE_TS + MIN_BARS * PERIOD_MS
        # The anchor must equal the close of the last bar, not the last bar's open.
        assert snapshot_mkt.as_of_ms > snapshot_mkt.candles_by_symbol["A/USDT"][-1].timestamp


class TestSnapshotConsistency:
    def test_market_snapshot_fails_when_a_universe_symbol_has_no_bars(self) -> None:
        source = _source_with(FIVE_COIN_RETS)
        anchor = BASE_TS + MIN_BARS * PERIOD_MS
        universe = get_universe_snapshot(source, FIVE_COIN_SYMBOLS, "1h", anchor)
        source.load_all("E/USDT", "1h", [])  # data for E disappears
        with pytest.raises(InsufficientDataError):
            get_market_snapshot(source, universe, "1h")

    def test_duplicate_timestamps_are_rejected(self) -> None:
        from crypto_bot.core.types import Candle
        from crypto_bot.portfolio import MarketSnapshot

        bar = Candle(timestamp=BASE_TS, open=100.0, high=101.0, low=99.0, close=100.0, volume=1.0)
        with pytest.raises(ValueError, match="duplicate timestamps"):
            MarketSnapshot(
                as_of_ms=BASE_TS + PERIOD_MS,
                timeframe="1h",
                candles_by_symbol={"A/USDT": (bar, bar)},
            )

    def test_unsorted_bars_are_rejected(self) -> None:
        from crypto_bot.core.types import Candle
        from crypto_bot.portfolio import MarketSnapshot

        bars = (
            Candle(timestamp=BASE_TS + PERIOD_MS, open=100.0, high=101.0, low=99.0, close=100.0, volume=1.0),
            Candle(timestamp=BASE_TS, open=100.0, high=101.0, low=99.0, close=100.0, volume=1.0),
        )
        with pytest.raises(ValueError, match="sorted by timestamp"):
            MarketSnapshot(
                as_of_ms=BASE_TS + 2 * PERIOD_MS,
                timeframe="1h",
                candles_by_symbol={"A/USDT": bars},
            )

    def test_strategy_never_ranks_a_symbol_outside_the_snapshot(self) -> None:
        # F is in the universe (enough bars) but its candles are missing from
        # the snapshot mapping; the strategy ranks only what it was given.
        snapshot_mkt = closes_snapshot({s: [100.0] * (MIN_BARS - 1) + [100.0 * (1 + ret)] for s, ret in FIVE_COIN_RETS.items()})
        intent = momentum(top_fraction=0.4, short_fraction=0.4).evaluate_market(snapshot_mkt, state())
        assert {i.symbol for i in intent.intents} == {"A/USDT", "B/USDT", "D/USDT", "E/USDT"}
