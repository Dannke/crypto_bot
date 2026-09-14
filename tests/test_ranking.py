"""CSM research: ranking on the synthetic 5-coin universe (task 11).

A = +20%, B = +10%, C = 0%, D = -10%, E = -20%.  The canonical assertions:
A/B are the top two (LONG), D/E are the bottom two (SHORT), C is never
selected by the 40%/40% cut, and the rank order is strictly descending.
"""
from __future__ import annotations

from csm_helpers import (
    FIVE_COIN_RETS,
    FIVE_COIN_SYMBOLS,
    SHORT_FRACTION,
    TOP_FRACTION,
    closes_snapshot,
    momentum,
    snapshot,
    state,
)

from crypto_bot.core.enums import Side


def _long_short(intent):
    return [(i.symbol, i.side) for i in intent.intents]


class TestSyntheticFiveCoins:
    def test_top_two_gainers_are_long_in_rank_order(self) -> None:
        intent = momentum(
            top_fraction=TOP_FRACTION, short_fraction=None
        ).evaluate_market(snapshot(FIVE_COIN_RETS), state())
        assert _long_short(intent) == [
            ("A/USDT", Side.LONG),
            ("B/USDT", Side.LONG),
        ]

    def test_bottom_two_losers_are_short(self) -> None:
        intent = momentum(
            top_fraction=TOP_FRACTION, short_fraction=SHORT_FRACTION
        ).evaluate_market(snapshot(FIVE_COIN_RETS), state())
        assert _long_short(intent) == [
            ("A/USDT", Side.LONG),
            ("B/USDT", Side.LONG),
            ("D/USDT", Side.SHORT),
            ("E/USDT", Side.SHORT),
        ]

    def test_shorts_follow_the_rank_order_after_the_longs(self) -> None:
        intent = momentum(
            top_fraction=TOP_FRACTION, short_fraction=SHORT_FRACTION
        ).evaluate_market(snapshot(FIVE_COIN_RETS), state())
        shorts = [i.symbol for i in intent.intents if i.side == Side.SHORT]
        assert shorts == ["D/USDT", "E/USDT"]  # -10% ranks above -20%

    def test_neutral_coin_is_never_selected_by_the_cut(self) -> None:
        intent = momentum(
            top_fraction=TOP_FRACTION, short_fraction=SHORT_FRACTION
        ).evaluate_market(snapshot(FIVE_COIN_RETS), state())
        assert "C/USDT" not in [i.symbol for i in intent.intents]

    def test_neutral_coin_ranks_third_when_the_cut_widens(self) -> None:
        # Top 80% of five -> ceil(4.0) = 4: A, B, C, D; E excluded.
        intent = momentum(top_fraction=0.8).evaluate_market(
            snapshot(FIVE_COIN_RETS), state()
        )
        assert [i.symbol for i in intent.intents] == [
            "A/USDT", "B/USDT", "C/USDT", "D/USDT",
        ]

    def test_wider_short_cut_absorbs_the_neutral_coin(self) -> None:
        # Top 20% (A) long, bottom 80% (B/C/D/E) short.
        intent = momentum(top_fraction=0.2, short_fraction=0.8).evaluate_market(
            snapshot(FIVE_COIN_RETS), state()
        )
        assert _long_short(intent) == [
            ("A/USDT", Side.LONG),
            ("B/USDT", Side.SHORT),
            ("C/USDT", Side.SHORT),
            ("D/USDT", Side.SHORT),
            ("E/USDT", Side.SHORT),
        ]


class TestCompositeRanking:
    def test_composite_preserves_the_canonical_order(self) -> None:
        # Both horizons carry the same per-symbol return, so the composite
        # mean keeps the single-horizon ranking exactly.
        n = 8
        closes = {s: [100.0] * n for s in FIVE_COIN_SYMBOLS}
        for s, ret in FIVE_COIN_RETS.items():
            closes[s][-1] = 100.0 * (1.0 + ret)
        intent = momentum(
            lookbacks_bars=(2, 5), top_fraction=TOP_FRACTION, short_fraction=SHORT_FRACTION
        ).evaluate_market(closes_snapshot(closes), state())
        assert _long_short(intent) == [
            ("A/USDT", Side.LONG),
            ("B/USDT", Side.LONG),
            ("D/USDT", Side.SHORT),
            ("E/USDT", Side.SHORT),
        ]

    def test_reversal_flips_the_ranking(self) -> None:
        # The exact mirror of the canonical universe: E is now the winner.
        reversed_rets = {s: -r for s, r in FIVE_COIN_RETS.items()}
        intent = momentum(
            top_fraction=TOP_FRACTION, short_fraction=SHORT_FRACTION
        ).evaluate_market(snapshot(reversed_rets), state())
        assert _long_short(intent) == [
            ("E/USDT", Side.LONG),
            ("D/USDT", Side.LONG),
            ("B/USDT", Side.SHORT),
            ("A/USDT", Side.SHORT),
        ]


class TestStableOrdering:
    def test_tied_returns_keep_input_order(self) -> None:
        intent = momentum(top_fraction=1.0).evaluate_market(
            snapshot({s: 0.0 for s in FIVE_COIN_SYMBOLS}), state()
        )
        assert [i.symbol for i in intent.intents] == FIVE_COIN_SYMBOLS

    def test_symbols_with_no_computable_return_rank_never(self) -> None:
        # F has exactly `lookback` bars: no return, so it is dropped before
        # ranking and the canonical five keep their exact order.
        from csm_helpers import LOOKBACK, closes_snapshot

        n = LOOKBACK + 2
        closes = {s: [100.0] * (n - 1) + [100.0 * (1 + ret)] for s, ret in FIVE_COIN_RETS.items()}
        closes["F/USDT"] = [100.0] * (n - 3) + [130.0]  # 5 bars: 1 short of max(lookback)+1
        intent = momentum(top_fraction=1.0).evaluate_market(closes_snapshot(closes), state())
        assert [i.symbol for i in intent.intents] == FIVE_COIN_SYMBOLS
        assert "F/USDT" not in [i.symbol for i in intent.intents]
