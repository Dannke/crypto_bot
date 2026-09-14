"""Tests for execution cost models and legacy-behavior parity."""
from __future__ import annotations

import pytest

from crypto_bot.core.enums import Side
from crypto_bot.execution import (
    CompositeCostModel,
    CostResult,
    ExecutionCostModel,
    SimpleFeeModel,
    SimpleSlippageModel,
)
from crypto_bot.simulation.executor import SignalExecutor
from crypto_bot.simulation.fees import FeeCalculator, FeeSchedule, FeeTier


class TestSimpleFeeModel:
    def test_matches_legacy_fee_calculator_defaults(self) -> None:
        legacy = FeeCalculator()
        model = SimpleFeeModel()
        for is_maker, side in ((True, Side.LONG), (False, Side.SHORT)):
            expected = legacy.calculate(2.5, 100.0, is_maker=is_maker)
            result = model.calculate(2.5, 100.0, side, is_maker=is_maker)
            assert result.fee_pct == expected.fee_pct
            assert result.fee_abs == expected.fee_abs
            assert result.net_amount == expected.net_amount
            assert result.adjusted_price == 100.0
            assert result.slippage_pct == 0.0

    def test_matches_legacy_fee_calculator_all_tiers(self) -> None:
        schedule = FeeSchedule(maker_fee_pct=0.05, taker_fee_pct=0.12, base_discount=0.02)
        for tier in FeeTier:
            legacy = FeeCalculator(schedule=schedule, tier=tier)
            model = SimpleFeeModel.from_fee_calculator(legacy)
            for is_maker in (True, False):
                expected = legacy.calculate(1.0, 300.0, is_maker=is_maker)
                result = model.calculate(1.0, 300.0, Side.LONG, is_maker=is_maker)
                assert result.fee_pct == expected.fee_pct
                assert result.fee_abs == expected.fee_abs
                assert result.net_amount == expected.net_amount

    def test_ignores_spread_and_side(self) -> None:
        model = SimpleFeeModel()
        long_result = model.calculate(1.0, 100.0, Side.LONG, spread_pct=50.0)
        short_result = model.calculate(1.0, 100.0, Side.SHORT, spread_pct=50.0)
        assert long_result == short_result

    def test_zero_fee_no_cost(self) -> None:
        result = SimpleFeeModel(0.0, 0.0).calculate(4.0, 50.0, Side.LONG)
        assert result.fee_abs == 0.0
        assert result.net_amount == 200.0

    @pytest.mark.parametrize(
        ("amount", "price", "spread_pct", "side"),
        [
            (0.0, 100.0, 0.0, Side.LONG),
            (-1.0, 100.0, 0.0, Side.LONG),
            (1.0, 0.0, 0.0, Side.LONG),
            (1.0, -5.0, 0.0, Side.LONG),
            (1.0, 100.0, -0.1, Side.LONG),
            (1.0, 100.0, 0.0, "LONG"),  # type: ignore[arg-type]
        ],
    )
    def test_rejects_invalid_inputs(self, amount: float, price: float, spread_pct: float, side: Side) -> None:
        model = SimpleFeeModel()
        with pytest.raises(ValueError):
            model.calculate(amount, price, side, spread_pct=spread_pct)

    @pytest.mark.parametrize("discount", [-0.1, 1.0, 2.0])
    def test_rejects_invalid_discount(self, discount: float) -> None:
        with pytest.raises(ValueError):
            SimpleFeeModel(discount=discount)


class TestSimpleSlippageModel:
    def test_matches_legacy_executor_slippage(self) -> None:
        model = SimpleSlippageModel()
        for side in (Side.LONG, Side.SHORT):
            for price in (50.0, 100.0, 1_234.5):
                for spread_pct in (0.0, 0.1, 0.42, 2.34, 9.0):
                    expected = SignalExecutor._apply_slippage(
                        price, side, spread_pct, SignalExecutor._SLIPPAGE_FACTOR
                    )
                    result = model.calculate(1.0, price, side, spread_pct=spread_pct)
                    assert result.adjusted_price == pytest.approx(expected)
                    assert result.slippage_pct == pytest.approx(
                        spread_pct * 0.5 * SignalExecutor._SLIPPAGE_FACTOR
                    )

    def test_direction(self) -> None:
        model = SimpleSlippageModel()
        long_result = model.calculate(1.0, 100.0, Side.LONG, spread_pct=0.4)
        short_result = model.calculate(1.0, 100.0, Side.SHORT, spread_pct=0.4)
        assert long_result.adjusted_price > 100.0
        assert short_result.adjusted_price < 100.0
        assert long_result.slippage_abs == short_result.slippage_abs

    def test_zero_spread_and_zero_factor(self) -> None:
        assert SimpleSlippageModel().calculate(1.0, 100.0, Side.LONG, spread_pct=0.0).adjusted_price == 100.0
        assert SimpleSlippageModel(0.0).calculate(1.0, 100.0, Side.LONG, spread_pct=5.0).adjusted_price == 100.0

    @pytest.mark.parametrize("factor", [-0.1, 1.01, 2.0])
    def test_rejects_invalid_factor(self, factor: float) -> None:
        with pytest.raises(ValueError):
            SimpleSlippageModel(factor)


class TestCompositeCostModel:
    def test_reproduces_legacy_entry_flow(self) -> None:
        composite = CompositeCostModel.legacy_default()
        amount, price, spread_pct = 2.0, 100.0, 0.4
        result = composite.calculate(amount, price, Side.LONG, is_maker=False, spread_pct=spread_pct)

        adjusted = price * (1.0 + spread_pct / 100.0 * 0.5 * SignalExecutor._SLIPPAGE_FACTOR)
        legacy_fee = FeeCalculator().calculate(amount, adjusted, is_maker=False)
        assert result.adjusted_price == pytest.approx(adjusted)
        assert result.fee_abs == legacy_fee.fee_abs
        assert result.net_amount == legacy_fee.net_amount
        assert result.fee_pct == 0.1
        assert result.slippage_pct == pytest.approx(spread_pct * 0.25)

    def test_legacy_default_equals_explicit_models(self) -> None:
        composite = CompositeCostModel.legacy_default()
        explicit = CompositeCostModel(SimpleFeeModel(), SimpleSlippageModel())
        for side in (Side.LONG, Side.SHORT):
            assert composite.calculate(1.5, 88.0, side, spread_pct=0.7) == explicit.calculate(
                1.5, 88.0, side, spread_pct=0.7
            )

    def test_zero_cost_composite(self) -> None:
        composite = CompositeCostModel(SimpleFeeModel(0.0, 0.0), SimpleSlippageModel(0.0))
        result = composite.calculate(3.0, 200.0, Side.SHORT, spread_pct=1.0)
        assert result.adjusted_price == 200.0
        assert result.fee_abs == 0.0
        assert result.net_amount == 600.0

    def test_custom_components(self) -> None:
        composite = CompositeCostModel(SimpleFeeModel(0.2, 0.5), SimpleSlippageModel(1.0))
        result = composite.calculate(1.0, 100.0, Side.LONG, is_maker=True, spread_pct=1.0)
        assert result.adjusted_price == pytest.approx(100.5)
        assert result.fee_pct == 0.2
        assert result.fee_abs == pytest.approx(0.201)

    @pytest.mark.parametrize("fee_model", [None, object()])
    def test_rejects_invalid_fee_model(self, fee_model) -> None:
        with pytest.raises(ValueError, match="fee_model"):
            CompositeCostModel(fee_model, SimpleSlippageModel())  # type: ignore[arg-type]

    @pytest.mark.parametrize("slippage_model", [None, object()])
    def test_rejects_invalid_slippage_model(self, slippage_model) -> None:
        with pytest.raises(ValueError, match="slippage_model"):
            CompositeCostModel(SimpleFeeModel(), slippage_model)  # type: ignore[arg-type]


class TestInterfaceAndImmutability:
    def test_models_are_execution_cost_models(self) -> None:
        assert isinstance(SimpleFeeModel(), ExecutionCostModel)
        assert isinstance(SimpleSlippageModel(), ExecutionCostModel)
        assert isinstance(CompositeCostModel.legacy_default(), ExecutionCostModel)

    def test_cost_result_is_immutable_and_hashable(self) -> None:
        result = CostResult(0.1, 0.2, 0.0, 0.0, 100.0, 200.0)
        with pytest.raises(AttributeError):
            result.fee_abs = 1.0  # type: ignore[misc]
        assert result == CostResult(0.1, 0.2, 0.0, 0.0, 100.0, 200.0)
        assert len({result, CostResult(0.1, 0.2, 0.0, 0.0, 100.0, 200.0)}) == 1

    def test_cost_result_validation(self) -> None:
        with pytest.raises(ValueError):
            CostResult(-0.1, 0.0, 0.0, 0.0, 100.0, 200.0)
        with pytest.raises(ValueError):
            CostResult(0.1, 0.0, 0.0, 0.0, 0.0, 200.0)
