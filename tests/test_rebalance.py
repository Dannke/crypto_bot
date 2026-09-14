"""CSM research: rebalance cadence (task 11).

The strategy is stateless: the caller decides when to re-evaluate (the
config contract is ``csm.rebalance_hours``, default 24).  Between
rebalances the executor holds positions — a symbol still in the new intent
is not re-opened (no churn), a symbol that drops out stays open, and a new
entrant opens at the next tick.
"""
from __future__ import annotations

import pytest
from csm_helpers import (
    BASE_TS,
    MIN_BARS,
    PERIOD_MS,
    closes_snapshot,
    five_coin_closes,
    momentum,
    state,
)

from crypto_bot.config.env import Config, EnvConfig
from crypto_bot.config.schemas import CsmConfig, Settings
from crypto_bot.core import policy
from crypto_bot.core.enums import Side
from crypto_bot.simulation.portfolio_executor import PortfolioExecutor
from crypto_bot.storage.db import Database, Repositories


class TestCadence:
    def test_24h_rebalance_is_24_bars_on_1h(self) -> None:
        assert policy.parse_duration_seconds("24h") // 3_600 == 24
        assert policy.parse_duration_seconds("72h") // 3_600 == 72
        assert policy.parse_duration_seconds("168h") // 3_600 == 168

    def test_rebalance_hours_config_defaults_to_24(self) -> None:
        assert CsmConfig().rebalance_hours == 24
        with pytest.raises(ValueError):
            CsmConfig(rebalance_hours=0)

    def test_strategy_is_stateless_across_rebalance_ticks(self) -> None:
        # Identical closes, observation instant moved 24 bars later: the
        # strategy must emit the same selection (the caller sets the cadence).
        strategy = momentum(top_fraction=0.4, short_fraction=0.4)
        tick_0 = closes_snapshot(five_coin_closes(), anchor_offset_bars=0)
        tick_24 = closes_snapshot(five_coin_closes(), anchor_offset_bars=24)
        intent_0 = strategy.evaluate_market(tick_0, state(tick_0.as_of_ms))
        intent_24 = strategy.evaluate_market(tick_24, state(tick_24.as_of_ms))

        assert intent_24.as_of_ms - intent_0.as_of_ms == 24 * PERIOD_MS
        assert [(i.symbol, i.side, i.target_weight) for i in intent_0.intents] == [
            (i.symbol, i.side, i.target_weight) for i in intent_24.intents
        ]


def _config(**overrides) -> Config:
    settings = Settings.model_validate({**Settings().model_dump(), **overrides})
    return Config(settings=settings, env=EnvConfig())


class TestHoldBetweenRebalances:
    def test_re_evaluating_the_same_intent_does_not_churn(self, tmp_path) -> None:
        db = Database(tmp_path / "rebalance.db")
        ex = PortfolioExecutor(_config(), Repositories(db))
        intent = momentum(top_fraction=0.4, short_fraction=0.4).evaluate_market(
            closes_snapshot(five_coin_closes()), state()
        )
        t0 = BASE_TS + MIN_BARS * PERIOD_MS
        t1 = t0 + 24 * PERIOD_MS

        for pi in intent.intents:
            assert ex.open_position(pi, entry_price=100.0, atr_pct=1.0, timestamp_ms=t0).handled
        assert len([p for p in ex.tracker.positions if p.is_open]) == 4

        for pi in intent.intents:  # same decision at the next rebalance tick
            result = ex.open_position(pi, entry_price=100.0, atr_pct=1.0, timestamp_ms=t1)
            assert not result.handled
            assert "already open" in result.message
        assert len([p for p in ex.tracker.positions if p.is_open]) == 4
        db.close()

    def test_rank_flip_opens_the_entrant_but_holds_incumbents(self, tmp_path) -> None:
        # At t0: A/B long, D/E short (canonical).  At t1 the returns flip and
        # F enters at +30%: F is the new top.  The executor must open F but
        # must NOT re-open or flip the incumbents (A stays long even though
        # the new intent would short it).
        db = Database(tmp_path / "rebalance_flip.db")
        ex = PortfolioExecutor(_config(), Repositories(db))

        n = MIN_BARS
        closes_t0 = five_coin_closes(n)
        closes_t1 = {s: list(closes) for s, closes in closes_t0.items()}
        for s, ret in {"A/USDT": -0.30, "B/USDT": -0.10, "C/USDT": 0.0, "D/USDT": 0.10, "E/USDT": 0.20}.items():
            closes_t1[s][-1] = 100.0 * (1.0 + ret)
        closes_t1["F/USDT"] = [100.0] * (n - 1) + [130.0]  # +30% entrant

        strategy = momentum(top_fraction=0.33, short_fraction=0.33)
        tick_0 = closes_snapshot(closes_t0)
        tick_1 = closes_snapshot(closes_t1, anchor_offset_bars=24)
        intent_0 = strategy.evaluate_market(tick_0, state(tick_0.as_of_ms))
        intent_1 = strategy.evaluate_market(tick_1, state(tick_1.as_of_ms))

        assert [(i.symbol, i.side) for i in intent_0.intents] == [
            ("A/USDT", Side.LONG), ("B/USDT", Side.LONG),
            ("D/USDT", Side.SHORT), ("E/USDT", Side.SHORT),
        ]
        # Reversed + entrant: 6 symbols, top/bottom 33% -> ceil(1.98)=2 each.
        assert [(i.symbol, i.side) for i in intent_1.intents] == [
            ("F/USDT", Side.LONG), ("E/USDT", Side.LONG),
            ("B/USDT", Side.SHORT), ("A/USDT", Side.SHORT),
        ]

        t0 = tick_0.as_of_ms
        t1 = tick_1.as_of_ms
        for pi in intent_0.intents:
            ex.open_position(pi, entry_price=100.0, atr_pct=1.0, timestamp_ms=t0)

        opened_at_t1 = []
        for pi in intent_1.intents:
            result = ex.open_position(pi, entry_price=100.0, atr_pct=1.0, timestamp_ms=t1)
            if result.handled:
                opened_at_t1.append(pi.symbol)

        assert opened_at_t1 == ["F/USDT"]
        open_positions = {p.symbol: p for p in ex.tracker.positions if p.is_open}
        assert set(open_positions) == {"A/USDT", "B/USDT", "D/USDT", "E/USDT", "F/USDT"}
        # Incumbent A is untouched: still LONG, still the old entry price.
        assert open_positions["A/USDT"].side == Side.LONG
        assert open_positions["A/USDT"].entry_price == pytest.approx(100.0)
        db.close()
