"""Tests for funding cost model (R0.2)."""
from __future__ import annotations

from crypto_bot.core.enums import Side
from crypto_bot.data.funding import FundingEvent
from crypto_bot.execution.costs import (
    CompositeCostModel,
    CostResult,
    FundingCostModel,
)


def assert_approx(actual: float, expected: float, tol: float = 1e-9) -> None:
    assert abs(actual - expected) < tol, f"{actual} != {expected} (tol={tol})"


def make_events():
    base = 1_700_000_000_000
    return [
        FundingEvent("BTCUSDT", base, 0.0001, 35000.0),
        FundingEvent("BTCUSDT", base + 8 * 3_600_000, -0.00005, 35100.0),
        FundingEvent("BTCUSDT", base + 16 * 3_600_000, 0.0002, 35200.0),
    ]


def test_funding_cost_long_positive_rate():
    """Long position pays when funding rate is positive."""
    model = FundingCostModel()
    events = [
        FundingEvent("BTCUSDT", 1_000_000, 0.0001, 35000.0),  # 0.01%
    ]
    result = model.accrue(Side.LONG, 0.1, 35000.0, events)
    # notional = 0.1 * 35000 = 3500
    # cost = 3500 * 0.0001 = 0.35
    assert_approx(result.fee_abs, 0.35)
    assert_approx(result.net_amount, -0.35)  # negative = cost to P&L


def test_funding_cost_short_positive_rate():
    """Short position receives when funding rate is positive."""
    model = FundingCostModel()
    events = [
        FundingEvent("BTCUSDT", 1_000_000, 0.0001, 35000.0),
    ]
    result = model.accrue(Side.SHORT, 0.1, 35000.0, events)
    # notional = 0.1 * 35000 = 3500
    # cost = -3500 * 0.0001 = -0.35 (receives)
    assert_approx(result.fee_abs, 0.35)
    assert_approx(result.net_amount, 0.35)  # positive = income to P&L


def test_funding_cost_long_negative_rate():
    """Long position receives when funding rate is negative."""
    model = FundingCostModel()
    events = [
        FundingEvent("BTCUSDT", 1_000_000, -0.0001, 35000.0),
    ]
    result = model.accrue(Side.LONG, 0.1, 35000.0, events)
    # notional = 0.1 * 35000 = 3500
    # cost = 3500 * -0.0001 = -0.35 (receives)
    assert_approx(result.fee_abs, 0.35)
    assert_approx(result.net_amount, 0.35)


def test_funding_cost_short_negative_rate():
    """Short position pays when funding rate is negative."""
    model = FundingCostModel()
    events = [
        FundingEvent("BTCUSDT", 1_000_000, -0.0001, 35000.0),
    ]
    result = model.accrue(Side.SHORT, 0.1, 35000.0, events)
    # notional = 0.1 * 35000 = 3500
    # cost = -3500 * -0.0001 = 0.35 (pays)
    assert_approx(result.fee_abs, 0.35)
    assert_approx(result.net_amount, -0.35)


def test_funding_no_events():
    """No funding events returns zero cost."""
    model = FundingCostModel()
    result = model.accrue(Side.LONG, 0.1, 35000.0, [])
    assert_approx(result.fee_abs, 0.0)
    assert_approx(result.net_amount, 0.0)


def test_funding_disabled():
    """Disabled model returns zero cost."""
    model = FundingCostModel(enabled=False)
    events = [FundingEvent("BTCUSDT", 1_000_000, 0.0001, 35000.0)]
    result = model.accrue(Side.LONG, 0.1, 35000.0, events)
    assert_approx(result.fee_abs, 0.0)
    assert_approx(result.net_amount, 0.0)


def test_funding_multiple_events():
    """Multiple funding events accumulate correctly."""
    model = FundingCostModel()
    events = [
        FundingEvent("BTCUSDT", 1_000_000, 0.0001, 35000.0),      # pays 0.35
        FundingEvent("BTCUSDT", 2_000_000, -0.00005, 35100.0),    # receives 0.1755
        FundingEvent("BTCUSDT", 3_000_000, 0.0002, 35200.0),      # pays 0.704
    ]
    result = model.accrue(Side.LONG, 0.1, 35000.0, events)
    # Total = 0.35 - 0.1755 + 0.704 = 0.8785
    assert_approx(result.fee_abs, 0.8785, tol=1e-6)
    assert_approx(result.net_amount, -0.8785, tol=1e-6)


def test_funding_uses_mark_price():
    """Funding uses mark_price when available."""
    model = FundingCostModel()
    events = [
        FundingEvent("BTCUSDT", 1_000_000, 0.0001, 36000.0),  # mark > entry
    ]
    result = model.accrue(Side.LONG, 0.1, 35000.0, events)
    # notional = 0.1 * 36000 = 3600 (uses mark price)
    # cost = 3600 * 0.0001 = 0.36
    assert_approx(result.fee_abs, 0.36)


def test_funding_fallback_to_entry_price():
    """Funding falls back to entry_price when mark_price is None."""
    model = FundingCostModel()
    events = [
        FundingEvent("BTCUSDT", 1_000_000, 0.0001, None),
    ]
    result = model.accrue(Side.LONG, 0.1, 35000.0, events)
    # notional = 0.1 * 35000 = 3500 (uses entry price)
    # cost = 3500 * 0.0001 = 0.35
    assert_approx(result.fee_abs, 0.35)


def test_funding_accrue_simple():
    """Simplified accrual for SignalExecutor."""
    model = FundingCostModel()
    events = [
        FundingEvent("BTCUSDT", 1_000_000, 0.0001, 35000.0),
    ]
    # notional = 1000 (e.g., position size * entry_price)
    result = model.accrue_simple(Side.LONG, 1000.0, events)
    # cost = 1000 * 0.0001 = 0.1
    # returns -cost = -0.1 (negative = cost to P&L)
    assert result == -0.1

    result_short = model.accrue_simple(Side.SHORT, 1000.0, events)
    # short receives = +0.1
    assert result_short == 0.1


def test_composite_bybit_perp_default():
    """bybit_perp_default includes funding model."""
    composite = CompositeCostModel.bybit_perp_default()
    assert composite.funding_model is not None
    assert isinstance(composite.funding_model, FundingCostModel)


def test_composite_legacy_default_no_funding():
    """legacy_default does not include funding model."""
    composite = CompositeCostModel.legacy_default()
    assert composite.funding_model is None


def test_composite_accrue_funding_delegates():
    """CompositeCostModel.accrue_funding delegates to funding model."""
    composite = CompositeCostModel.bybit_perp_default()
    events = [FundingEvent("BTCUSDT", 1_000_000, 0.0001, 35000.0)]
    result = composite.accrue_funding(Side.LONG, 0.1, 35000.0, events)
    assert_approx(result.fee_abs, 0.35)
    assert_approx(result.net_amount, -0.35)


def test_composite_accrue_funding_no_model():
    """Composite without funding model returns zero."""
    composite = CompositeCostModel.legacy_default()
    events = [FundingEvent("BTCUSDT", 1_000_000, 0.0001, 35000.0)]
    result = composite.accrue_funding(Side.LONG, 0.1, 35000.0, events)
    assert_approx(result.fee_abs, 0.0)
    assert_approx(result.net_amount, 0.0)


def test_cost_result_validation():
    """CostResult validates all fields."""
    # Valid
    CostResult(
        fee_pct=0.1,
        fee_abs=1.0,
        slippage_pct=0.05,
        slippage_abs=0.5,
        adjusted_price=100.0,
        net_amount=99.0,
    )

    # Invalid fee_pct (negative)
    import pytest
    with pytest.raises(ValueError):
        CostResult(
            fee_pct=-0.1,
            fee_abs=1.0,
            slippage_pct=0.05,
            slippage_abs=0.5,
            adjusted_price=100.0,
            net_amount=99.0,
        )

    # Invalid adjusted_price (zero)
    with pytest.raises(ValueError):
        CostResult(
            fee_pct=0.1,
            fee_abs=1.0,
            slippage_pct=0.05,
            slippage_abs=0.5,
            adjusted_price=0.0,
            net_amount=99.0,
        )


def test_funding_cost_model_calculate_not_used():
    """FundingCostModel.calculate returns no-op (funding is accrued, not per-trade)."""
    model = FundingCostModel()
    result = model.calculate(1.0, 100.0, Side.LONG)
    assert result.fee_abs == 0.0
    assert result.slippage_abs == 0.0
    assert result.adjusted_price == 100.0
    assert result.net_amount == 100.0