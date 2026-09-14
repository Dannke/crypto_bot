"""CSM research: transaction costs on portfolio positions (task 11).

The executor charges fees and slippage through the injectable cost model:
entry price is slippage-adjusted, the entry fee is deducted on close, and
a round trip at the entry price must lose exactly the round-trip fees.
"""
from __future__ import annotations

import pytest

from crypto_bot.config.env import Config, EnvConfig
from crypto_bot.config.schemas import Settings
from crypto_bot.core.enums import Side
from crypto_bot.execution.costs import (
    CompositeCostModel,
    CostResult,
    ExecutionCostModel,
    SimpleSlippageModel,
)
from crypto_bot.portfolio import PositionIntent
from crypto_bot.simulation.portfolio_executor import PortfolioExecutor
from crypto_bot.storage.db import Database, Repositories

EQUITY = 10_000.0
FEE_PCT = 0.1  # SimpleFeeModel default: 0.1%


class ZeroCostModel(ExecutionCostModel):
    """Execution cost model that charges nothing at all."""

    def calculate(self, amount, price, side, *, is_maker=True, spread_pct=0.0):
        return CostResult(
            fee_pct=0.0, fee_abs=0.0, slippage_pct=0.0, slippage_abs=0.0,
            adjusted_price=price, net_amount=amount * price,
        )


def _config(**overrides) -> Config:
    settings = Settings.model_validate({**Settings().model_dump(), **overrides})
    return Config(settings=settings, env=EnvConfig())


def _intent(symbol: str = "BTC/USDT", weight: float = 0.5, side: Side = Side.LONG) -> PositionIntent:
    return PositionIntent(symbol=symbol, side=side, target_weight=weight, timeframe="1h")


@pytest.fixture()
def executor(tmp_path):
    db = Database(tmp_path / "costs.db")
    ex = PortfolioExecutor(_config(), Repositories(db), cost_model=ZeroCostModel())
    yield ex, db
    db.close()


class TestZeroCostModel:
    def test_entry_is_the_reference_price_and_equity_is_untouched(self, executor) -> None:
        ex, _ = executor
        result = ex.open_position(
            _intent(weight=0.5), entry_price=100.0, atr_pct=1.0,
            timestamp_ms=1_700_000_000_000,
        )
        assert result.entry_price == pytest.approx(100.0)
        assert result.size == pytest.approx(0.5 * EQUITY / 100.0)
        position = ex.tracker.positions[0]
        assert position.entry_fee_abs == 0.0
        assert ex.tracker.current_equity == pytest.approx(EQUITY)

    def test_round_trip_at_entry_price_is_lossless(self, executor) -> None:
        ex, _ = executor
        ex.open_position(
            _intent(weight=0.5), entry_price=100.0, atr_pct=1.0,
            timestamp_ms=1_700_000_000_000,
        )
        ex.close_all_positions("manual", {"BTC/USDT": {"1h": 100.0}})
        position = ex.tracker.positions[0]
        assert position.pnl_abs == pytest.approx(0.0)
        assert ex.tracker.current_equity == pytest.approx(EQUITY)


class TestLegacyCosts:
    def test_entry_fee_is_charged_and_round_trip_loses_fees(self, tmp_path) -> None:
        db = Database(tmp_path / "costs_legacy.db")
        ex = PortfolioExecutor(
            _config(), Repositories(db),
            cost_model=CompositeCostModel.legacy_default(),
        )
        ex.open_position(
            _intent(weight=0.5), entry_price=100.0, atr_pct=1.0,
            timestamp_ms=1_700_000_000_000,
        )
        position = ex.tracker.positions[0]
        assert position.entry_fee_abs == pytest.approx(0.5 * EQUITY * FEE_PCT / 100.0)  # 5.0

        ex.close_all_positions("manual", {"BTC/USDT": {"1h": 100.0}})
        exit_fee = position.size * 100.0 * FEE_PCT / 100.0
        assert position.pnl_abs == pytest.approx(-position.entry_fee_abs - exit_fee)
        assert ex.tracker.current_equity == pytest.approx(EQUITY + position.pnl_abs)
        db.close()

    def test_gross_pnl_net_of_both_fees(self, tmp_path) -> None:
        db = Database(tmp_path / "costs_pnl.db")
        model = CompositeCostModel.legacy_default()
        ex = PortfolioExecutor(_config(), Repositories(db), cost_model=model)
        ex.open_position(
            _intent(weight=0.5), entry_price=100.0, atr_pct=1.0,
            timestamp_ms=1_700_000_000_000,
        )
        position = ex.tracker.positions[0]
        exit_price = 110.0
        ex.close_all_positions("manual", {"BTC/USDT": {"1h": exit_price}})

        exit_fee = model.calculate(position.size, exit_price, Side.LONG).fee_abs
        gross = (exit_price - position.entry_price) * position.size
        expected_pnl = gross - position.entry_fee_abs - exit_fee
        assert position.pnl_abs == pytest.approx(expected_pnl)
        assert position.pnl_abs < gross  # fees strictly reduce the P&L
        db.close()

    def test_fees_scale_with_position_size(self, tmp_path) -> None:
        db = Database(tmp_path / "costs_scale.db")
        ex = PortfolioExecutor(
            _config(), Repositories(db),
            cost_model=CompositeCostModel.legacy_default(),
        )
        ex.open_position(
            _intent("BTC/USDT", weight=0.5), entry_price=100.0, atr_pct=1.0,
            timestamp_ms=1_700_000_000_000,
        )
        ex.open_position(
            _intent("ETH/USDT", weight=1.0), entry_price=100.0, atr_pct=1.0,
            timestamp_ms=1_700_000_000_000,
        )
        fees = {p.symbol: p.entry_fee_abs for p in ex.tracker.positions}
        assert fees["BTC/USDT"] == pytest.approx(5.0)
        assert fees["ETH/USDT"] == pytest.approx(10.0)
        db.close()


class TestSlippageDirection:
    @pytest.mark.parametrize(
        ("side", "expected_entry"),
        [(Side.LONG, 100.1), (Side.SHORT, 99.9)],
    )
    def test_slippage_is_directional(self, tmp_path, side, expected_entry) -> None:
        db = Database(tmp_path / "costs_slip.db")
        ex = PortfolioExecutor(
            _config(), Repositories(db),
            cost_model=SimpleSlippageModel(),  # no fees, slippage only
        )
        result = ex.open_position(
            _intent(side=side), entry_price=100.0, atr_pct=1.0,
            spread_pct=0.4, timestamp_ms=1_700_000_000_000,
        )
        assert result.entry_price == pytest.approx(expected_entry)
        assert result.size == pytest.approx(0.5 * EQUITY / expected_entry)
        db.close()

    def test_entry_fee_is_charged_on_the_slippage_adjusted_price(self, tmp_path) -> None:
        # Fee notional = size * adjusted entry; with 0.1% this cancels the
        # adjusted price, so the fee is exactly weight * equity * 0.1%.
        db = Database(tmp_path / "costs_fee_on_slip.db")
        ex = PortfolioExecutor(
            _config(), Repositories(db),
            cost_model=CompositeCostModel.legacy_default(),
        )
        ex.open_position(
            _intent(weight=0.5), entry_price=100.0, atr_pct=1.0,
            spread_pct=0.4, timestamp_ms=1_700_000_000_000,
        )
        position = ex.tracker.positions[0]
        assert position.entry_price == pytest.approx(100.1)
        assert position.entry_fee_abs == pytest.approx(0.5 * EQUITY * FEE_PCT / 100.0)
        db.close()
