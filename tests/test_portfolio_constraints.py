"""CSM research: portfolio constraints on the synthetic 5-coin intent (task 11).

The risk engine enforces deterministic limits — max positions, per-position
weight, gross exposure, then net exposure — on top of the strategy intent;
the executor enforces the live ``max_open_positions`` guard on top of that.
The canonical 5-coin intent (A/B long, D/E short, 0.25 each) is the
workload for every constraint.
"""
from __future__ import annotations

import pytest
from csm_helpers import FIVE_COIN_RETS, momentum, snapshot, state

from crypto_bot.config.env import Config, EnvConfig
from crypto_bot.config.schemas import Settings
from crypto_bot.core.enums import PortfolioRejectReason
from crypto_bot.portfolio import (
    PortfolioIntent,
    PortfolioRiskEngine,
    PortfolioRiskLimits,
    PortfolioState,
)
from crypto_bot.simulation.portfolio_executor import PortfolioExecutor
from crypto_bot.storage.db import Database, Repositories


def _canonical_intent() -> PortfolioIntent:
    return momentum(top_fraction=0.4, short_fraction=0.4).evaluate_market(
        snapshot(FIVE_COIN_RETS), state()
    )


def _limits(**overrides) -> PortfolioRiskLimits:
    params = dict(
        max_positions=10,
        max_position_weight=1.0,
        max_gross_exposure=1.0,
        max_net_exposure=1.0,
    )
    params.update(overrides)
    return PortfolioRiskLimits(**params)


class TestRiskEngineConstraints:
    def test_max_positions_drops_the_last_ranked_symbol(self) -> None:
        report = PortfolioRiskEngine(_limits(max_positions=3)).evaluate(
            _canonical_intent(), state()
        )
        assert [r.symbol for r in report.position_results if r.accepted] == [
            "A/USDT", "B/USDT", "D/USDT",
        ]
        rejected = [r for r in report.position_results if not r.accepted]
        assert [r.symbol for r in rejected] == ["E/USDT"]
        assert rejected[0].reason == PortfolioRejectReason.REJECT_MAX_POSITIONS
        assert not report.accepted
        assert report.gross_exposure == pytest.approx(0.75)

    def test_max_position_weight_rejects_every_position(self) -> None:
        report = PortfolioRiskEngine(_limits(max_position_weight=0.24)).evaluate(
            _canonical_intent(), state()
        )
        assert report.accepted is False
        assert all(not r.accepted for r in report.position_results)
        assert all(
            r.reason == PortfolioRejectReason.REJECT_MAX_POSITION_WEIGHT
            for r in report.position_results
        )
        assert report.adjusted_intent.intents == ()
        assert report.gross_exposure == pytest.approx(0.0)

    def test_max_gross_exposure_drops_the_smallest_weights_first(self) -> None:
        # All weights are equal, so the engine drops by rank order until the
        # gross (1.0) fits under 0.5: A and B are dropped, D/E stay.
        report = PortfolioRiskEngine(_limits(max_gross_exposure=0.5)).evaluate(
            _canonical_intent(), state()
        )
        assert [r.symbol for r in report.position_results if r.accepted] == [
            "D/USDT", "E/USDT",
        ]
        assert report.gross_exposure == pytest.approx(0.5)
        assert report.net_exposure == pytest.approx(0.5)
        assert PortfolioRejectReason.REJECT_MAX_GROSS_EXPOSURE in report.rejected_reasons

    def test_dollar_neutral_book_passes_any_net_limit(self) -> None:
        report = PortfolioRiskEngine(_limits(max_net_exposure=0.0)).evaluate(
            _canonical_intent(), state()
        )
        assert report.accepted
        assert len(report.adjusted_intent.intents) == 4
        assert report.net_exposure == pytest.approx(0.0)

    def test_max_net_exposure_trims_the_overloaded_side(self) -> None:
        long_only = momentum(top_fraction=0.4).evaluate_market(snapshot(FIVE_COIN_RETS), state())
        assert [i.target_weight for i in long_only.intents] == [0.5, 0.5]  # net 1.0
        report = PortfolioRiskEngine(_limits(max_net_exposure=0.5)).evaluate(long_only, state())
        assert [i.symbol for i in report.adjusted_intent.intents] == ["B/USDT"]
        assert report.net_exposure == pytest.approx(0.5)
        assert PortfolioRejectReason.REJECT_MAX_NET_EXPOSURE in report.rejected_reasons

    def test_constraint_order_is_positions_weight_gross_net(self) -> None:
        # max_positions=2 drops D/E first, then the gross cap (0.25) drops A.
        report = PortfolioRiskEngine(
            _limits(max_positions=2, max_gross_exposure=0.25)
        ).evaluate(_canonical_intent(), state())
        assert report.rejected_reasons[:1] == (PortfolioRejectReason.REJECT_MAX_POSITIONS,)
        assert PortfolioRejectReason.REJECT_MAX_GROSS_EXPOSURE in report.rejected_reasons
        assert [i.symbol for i in report.adjusted_intent.intents] == ["B/USDT"]

    def test_limits_reject_invalid_values(self) -> None:
        with pytest.raises(ValueError):
            _limits(max_positions=-1)
        with pytest.raises(ValueError):
            _limits(max_gross_exposure=-0.1)
        with pytest.raises(ValueError):
            PortfolioRiskLimits(max_positions=1, max_position_weight=0.5, max_gross_exposure=1.0, max_net_exposure=float("nan"))

    def test_engine_requires_matching_as_of(self) -> None:
        intent = _canonical_intent()
        shifted = PortfolioState(
            as_of_ms=intent.as_of_ms + 3_600_000, mode=state().mode,
            equity=10_000.0, cash=10_000.0,
        )
        with pytest.raises(ValueError, match="must match"):
            PortfolioRiskEngine(_limits()).evaluate(intent, shifted)


class TestExecutorConstraint:
    def test_max_open_positions_caps_the_canonical_book(self, tmp_path) -> None:
        settings = Settings.model_validate(
            {
                **Settings().model_dump(),
                "risk": {**Settings().model_dump()["risk"], "max_open_positions": 2},
            }
        )
        db = Database(tmp_path / "constraints.db")
        ex = PortfolioExecutor(Config(settings=settings, env=EnvConfig()), Repositories(db))

        opened, refused = [], []
        for pi in _canonical_intent().intents:
            result = ex.open_position(pi, entry_price=100.0, atr_pct=1.0, timestamp_ms=1_700_000_000_000)
            (opened if result.handled else refused).append(pi.symbol)

        assert opened == ["A/USDT", "B/USDT"]
        assert refused == ["D/USDT", "E/USDT"]
        assert len([p for p in ex.tracker.positions if p.is_open]) == 2
        db.close()
