"""CSM research: equal weighting of the synthetic 5-coin universe (task 11).

Selected symbols share equity equally: longs and shorts are symmetric, the
gross exposure is exactly 1.0, the long/short book is dollar-neutral, and
each intent carries the last close as its reference price.
"""
from __future__ import annotations

import pytest
from csm_helpers import (
    FIVE_COIN_RETS,
    SHORT_FRACTION,
    TOP_FRACTION,
    momentum,
    snapshot,
    state,
)

from crypto_bot.core.enums import Mode, Side
from crypto_bot.portfolio import PortfolioState


class TestEqualWeights:
    def test_long_only_weights_sum_to_one(self) -> None:
        intent = momentum(top_fraction=TOP_FRACTION).evaluate_market(
            snapshot(FIVE_COIN_RETS), state()
        )
        assert [i.symbol for i in intent.intents] == ["A/USDT", "B/USDT"]
        assert [i.target_weight for i in intent.intents] == [0.5, 0.5]
        assert sum(i.target_weight for i in intent.intents) == pytest.approx(1.0)

    def test_long_short_book_is_dollar_neutral(self) -> None:
        intent = momentum(
            top_fraction=TOP_FRACTION, short_fraction=SHORT_FRACTION
        ).evaluate_market(snapshot(FIVE_COIN_RETS), state())
        assert len(intent.intents) == 4
        assert all(i.target_weight == pytest.approx(0.25) for i in intent.intents)

        longs = [i for i in intent.intents if i.side == Side.LONG]
        shorts = [i for i in intent.intents if i.side == Side.SHORT]
        assert sum(i.target_weight for i in longs) == pytest.approx(0.5)
        assert sum(i.target_weight for i in shorts) == pytest.approx(0.5)  # absolute
        net = sum(
            i.target_weight if i.side == Side.LONG else -i.target_weight
            for i in intent.intents
        )
        assert net == pytest.approx(0.0)
        gross = sum(abs(i.target_weight) for i in intent.intents)
        assert gross == pytest.approx(1.0)

    def test_weights_are_independent_of_equity(self) -> None:
        intent = momentum(
            top_fraction=TOP_FRACTION, short_fraction=SHORT_FRACTION
        ).evaluate_market(snapshot(FIVE_COIN_RETS), state())
        rich_intent = momentum(
            top_fraction=TOP_FRACTION, short_fraction=SHORT_FRACTION
        ).evaluate_market(
            snapshot(FIVE_COIN_RETS),
            PortfolioState(as_of_ms=state().as_of_ms, mode=Mode.PAPER, equity=1_000_000.0, cash=1_000_000.0),
        )
        assert [i.target_weight for i in intent.intents] == [
            i.target_weight for i in rich_intent.intents
        ]

    def test_reference_price_is_the_last_close(self) -> None:
        intent = momentum(
            top_fraction=TOP_FRACTION, short_fraction=SHORT_FRACTION
        ).evaluate_market(snapshot(FIVE_COIN_RETS), state())
        by_symbol = {i.symbol: i for i in intent.intents}
        assert by_symbol["A/USDT"].reference_price == pytest.approx(120.0)
        assert by_symbol["B/USDT"].reference_price == pytest.approx(110.0)
        assert by_symbol["D/USDT"].reference_price == pytest.approx(90.0)
        assert by_symbol["E/USDT"].reference_price == pytest.approx(80.0)

    def test_the_neutral_coin_carries_no_weight(self) -> None:
        intent = momentum(
            top_fraction=TOP_FRACTION, short_fraction=SHORT_FRACTION
        ).evaluate_market(snapshot(FIVE_COIN_RETS), state())
        assert all(i.symbol != "C/USDT" for i in intent.intents)
