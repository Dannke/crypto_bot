"""Execution cost models: fees, slippage, and funding as composable, testable units.

The composite model is the entry-point contract for backtests and paper
trading.  ``CompositeCostModel.legacy_default()`` reproduces the exact
fee + slippage numbers the simulation pipeline produced before the cost
model migration, so switching a backtest to the new interface does not
change its results.

``CompositeCostModel.bybit_perp_default()`` adds funding accrual for
Bybit USDT-margined perpetual futures (R0.2).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from math import isfinite

from ..core.enums import Side
from ..data.funding import FundingEvent

# Tier discounts mirror ``FeeCalculator._get_tier_discount`` so that
# ``SimpleFeeModel.from_fee_calculator`` stays numerically identical to the
# legacy calculator.  Parity is enforced by tests across all tiers.
# Defined locally to avoid circular import with simulation.fees
_TIER_DISCOUNTS = {
    "tier_1": 0.0,
    "tier_2": 0.1,
    "tier_3": 0.2,
    "tier_4": 0.3,
}

# Legacy entry slippage factor of ``SignalExecutor._SLIPPAGE_FACTOR``.
_LEGACY_SLIPPAGE_FACTOR = 0.5


def _finite(value: float, field_name: str, *, non_negative: bool = False, positive: bool = False) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(float(value)):
        raise ValueError(f"{field_name} must be a finite number")
    if non_negative and value < 0:
        raise ValueError(f"{field_name} must be non-negative")
    if positive and value <= 0:
        raise ValueError(f"{field_name} must be greater than zero")


@dataclass(frozen=True, slots=True)
class CostResult:
    """Immutable report of one execution cost calculation."""

    fee_pct: float
    fee_abs: float
    slippage_pct: float
    slippage_abs: float
    adjusted_price: float
    net_amount: float

    def __post_init__(self) -> None:
        _finite(self.fee_pct, "fee_pct", non_negative=True)
        _finite(self.fee_abs, "fee_abs", non_negative=True)
        _finite(self.slippage_pct, "slippage_pct", non_negative=True)
        _finite(self.slippage_abs, "slippage_abs", non_negative=True)
        _finite(self.adjusted_price, "adjusted_price", positive=True)
        _finite(self.net_amount, "net_amount")  # can be negative for funding costs


class ExecutionCostModel(ABC):
    """Uniform contract for any cost component.

    A model consumes the trade inputs and returns an immutable
    :class:`CostResult`; it has no side effects and no business logic
    beyond the arithmetic of its own cost type.
    """

    @abstractmethod
    def calculate(
        self,
        amount: float,
        price: float,
        side: Side,
        *,
        is_maker: bool = True,
        spread_pct: float = 0.0,
    ) -> CostResult:
        """Compute costs for a single trade.

        Args:
            amount: Quantity traded in base currency.
            price: Reference (pre-adjustment) price in quote currency.
            side: Direction of the trade (slippage is directional).
            is_maker: Whether the order is maker or taker (fee models).
            spread_pct: Bid/ask spread in percent (slippage models).
        """


class SimpleFeeModel(ExecutionCostModel):
    """Flat-percentage fee model compatible with the legacy ``FeeCalculator``.

    ``fee_pct = (maker or taker) * (1 - discount)``, applied to the notional
    value.  Rounding matches ``FeeCalculator`` (4/8/8 decimals) so results
    are byte-identical to the legacy implementation.
    """

    def __init__(
        self,
        maker_fee_pct: float = 0.1,
        taker_fee_pct: float = 0.1,
        discount: float = 0.0,
    ) -> None:
        _finite(maker_fee_pct, "maker_fee_pct", non_negative=True)
        _finite(taker_fee_pct, "taker_fee_pct", non_negative=True)
        _finite(discount, "discount", non_negative=True)
        if discount >= 1.0:
            raise ValueError("discount must be less than 1.0")
        self._maker_fee_pct = maker_fee_pct
        self._taker_fee_pct = taker_fee_pct
        self._discount = discount

    @classmethod
    def from_fee_calculator(cls, calculator) -> SimpleFeeModel:
        """Rebuild the legacy fee behavior from an existing ``FeeCalculator``."""
        # Lazy import to avoid circular dependency
        tier_key = calculator._tier.value if hasattr(calculator._tier, 'value') else str(calculator._tier).lower()
        return cls(
            maker_fee_pct=calculator._schedule.maker_fee_pct,
            taker_fee_pct=calculator._schedule.taker_fee_pct,
            discount=_TIER_DISCOUNTS.get(tier_key, 0.0) + calculator._schedule.base_discount,
        )

    def calculate(
        self,
        amount: float,
        price: float,
        side: Side,
        *,
        is_maker: bool = True,
        spread_pct: float = 0.0,
    ) -> CostResult:
        _finite(amount, "amount", positive=True)
        _finite(price, "price", positive=True)
        _finite(spread_pct, "spread_pct", non_negative=True)
        if not isinstance(side, Side):
            raise ValueError("side must be a Side")

        notional = amount * price
        base_fee_pct = self._maker_fee_pct if is_maker else self._taker_fee_pct
        fee_pct = round(base_fee_pct * (1 - self._discount), 4)
        fee_abs = round(notional * (fee_pct / 100.0), 8)
        return CostResult(
            fee_pct=fee_pct,
            fee_abs=fee_abs,
            slippage_pct=0.0,
            slippage_abs=0.0,
            adjusted_price=price,
            net_amount=round(notional - fee_abs, 8),
        )


class SimpleSlippageModel(ExecutionCostModel):
    """Directional price-adjustment model reproducing legacy slippage.

    With the default ``half_spread_factor=0.5`` the model is identical to
    ``SignalExecutor._apply_slippage`` with ``_SLIPPAGE_FACTOR = 0.5``:
    entry moves by half the spread, further scaled by the factor, against
    the trade direction.
    """

    def __init__(self, half_spread_factor: float = _LEGACY_SLIPPAGE_FACTOR) -> None:
        _finite(half_spread_factor, "half_spread_factor", non_negative=True)
        if half_spread_factor > 1.0:
            raise ValueError("half_spread_factor must not exceed 1.0")
        self._half_spread_factor = half_spread_factor

    def calculate(
        self,
        amount: float,
        price: float,
        side: Side,
        *,
        is_maker: bool = True,
        spread_pct: float = 0.0,
    ) -> CostResult:
        _finite(amount, "amount", positive=True)
        _finite(price, "price", positive=True)
        _finite(spread_pct, "spread_pct", non_negative=True)
        if not isinstance(side, Side):
            raise ValueError("side must be a Side")

        slippage_pct = spread_pct * 0.5 * self._half_spread_factor
        direction = 1.0 if side == Side.LONG else -1.0
        adjusted_price = price * (1.0 + direction * slippage_pct / 100.0)
        notional = amount * price
        return CostResult(
            fee_pct=0.0,
            fee_abs=0.0,
            slippage_pct=slippage_pct,
            slippage_abs=abs(adjusted_price - price) * amount,
            adjusted_price=adjusted_price,
            net_amount=notional,
        )


class CompositeCostModel(ExecutionCostModel):
    """Combines fee, slippage, and funding models.

    Order is significant: the price is first adjusted for slippage, then
    the fee is charged on the adjusted notional — exactly the sequence the
    simulation executor used before the cost-model migration.
    Funding is accrued separately during position holding via accrue_funding().
    """

    def __init__(
        self,
        fee_model: ExecutionCostModel,
        slippage_model: ExecutionCostModel,
        funding_model: ExecutionCostModel | None = None,
    ) -> None:
        if not isinstance(fee_model, ExecutionCostModel):
            raise ValueError("fee_model must be an ExecutionCostModel")
        if not isinstance(slippage_model, ExecutionCostModel):
            raise ValueError("slippage_model must be an ExecutionCostModel")
        if funding_model is not None and not isinstance(funding_model, ExecutionCostModel):
            raise ValueError("funding_model must be an ExecutionCostModel")
        self._fee_model = fee_model
        self._slippage_model = slippage_model
        self._funding_model = funding_model

    @classmethod
    def legacy_default(cls) -> CompositeCostModel:
        """Build the composite that reproduces the pre-migration pipeline (no funding)."""
        return cls(fee_model=SimpleFeeModel(), slippage_model=SimpleSlippageModel())

    @classmethod
    def bybit_perp_default(cls) -> CompositeCostModel:
        """Build composite with fee + slippage + funding for Bybit perpetuals."""
        return cls(
            fee_model=SimpleFeeModel(),
            slippage_model=SimpleSlippageModel(),
            funding_model=FundingCostModel(),
        )

    @classmethod
    def bybit_perp_maker_only(cls) -> CompositeCostModel:
        """Build composite with maker-only fees for Bybit perpetuals (post-only execution).

        Uses maker fees (2 bps) + slippage (1 bps) for both entry and exit.
        Post-only orders that don't fill within timeout fall back to taker fees.
        """
        return cls(
            fee_model=SimpleFeeModel(maker_fee_pct=0.02, taker_fee_pct=0.10),
            slippage_model=SimpleSlippageModel(half_spread_factor=0.01),
            funding_model=FundingCostModel(),
        )

    @property
    def funding_model(self) -> ExecutionCostModel | None:
        return self._funding_model

    def accrue_funding(
        self,
        side: Side,
        weight: float,
        entry_price: float,
        funding_events: Sequence[FundingEvent],
    ) -> CostResult:
        """Accrue funding for a position using the embedded funding model."""
        if self._funding_model is None:
            return CostResult(
                fee_pct=0.0, fee_abs=0.0, slippage_pct=0.0, slippage_abs=0.0,
                adjusted_price=entry_price, net_amount=0.0
            )
        # FundingCostModel has accrue method
        if hasattr(self._funding_model, 'accrue'):
            return self._funding_model.accrue(side, weight, entry_price, funding_events)  # type: ignore[attr-defined]
        return CostResult(
            fee_pct=0.0, fee_abs=0.0, slippage_pct=0.0, slippage_abs=0.0,
            adjusted_price=entry_price, net_amount=0.0
        )

    def calculate(
        self,
        amount: float,
        price: float,
        side: Side,
        *,
        is_maker: bool = True,
        spread_pct: float = 0.0,
    ) -> CostResult:
        slippage_result = self._slippage_model.calculate(
            amount,
            price,
            side,
            is_maker=is_maker,
            spread_pct=spread_pct,
        )
        fee_result = self._fee_model.calculate(
            amount,
            slippage_result.adjusted_price,
            side,
            is_maker=is_maker,
            spread_pct=0.0,
        )
        return CostResult(
            fee_pct=fee_result.fee_pct,
            fee_abs=fee_result.fee_abs,
            slippage_pct=slippage_result.slippage_pct,
            slippage_abs=slippage_result.slippage_abs,
            adjusted_price=slippage_result.adjusted_price,
            net_amount=fee_result.net_amount,
        )


class FundingCostModel(ExecutionCostModel):
    """Accrues funding fees for positions held across funding timestamps.

    Bybit funding mechanics:
    - Funding fee = Position Value (mark price) × funding rate
    - Positive rate: longs pay, shorts receive
    - Negative rate: shorts pay, longs receive
    - Only charged if position is open at exact funding timestamp
    - Settlement uses mark price, not last traded price

    This model is applied during position holding (not at entry/exit),
    typically on each bar close in backtest or periodically in live.
    """

    def __init__(self, enabled: bool = True) -> None:
        self._enabled = enabled

    def calculate(
        self,
        amount: float,
        price: float,
        side: Side,
        *,
        is_maker: bool = True,
        spread_pct: float = 0.0,
    ) -> CostResult:
        """Not used for funding — funding is accrued via accrue(), not per-trade."""
        return CostResult(
            fee_pct=0.0,
            fee_abs=0.0,
            slippage_pct=0.0,
            slippage_abs=0.0,
            adjusted_price=price,
            net_amount=amount * price,
        )

    def accrue(
        self,
        side: Side,
        weight: float,
        entry_price: float,
        funding_events: Sequence[FundingEvent],
    ) -> CostResult:
        """Calculate funding cost for a position across funding events.

        Args:
            side: Position side (LONG or SHORT)
            weight: Portfolio weight (e.g., 0.1 for 10%)
            entry_price: Reference price for notional calculation
            funding_events: Funding events that occurred while position was open

        Returns:
            CostResult with fee_abs = total funding paid/received (negative = received)
        """
        if not self._enabled or not funding_events:
            return CostResult(
                fee_pct=0.0,
                fee_abs=0.0,
                slippage_pct=0.0,
                slippage_abs=0.0,
                adjusted_price=entry_price,
                net_amount=0.0,
            )

        total_funding = 0.0
        for event in funding_events:
            # Position notional at mark price (or fall back to entry_price)
            mark = event.mark_price if event.mark_price is not None else entry_price
            notional = abs(weight) * mark

            # Funding cost: positive rate -> long pays, short receives
            # cost = notional * rate for long, -notional * rate for short
            if side == Side.LONG:
                cost = notional * event.funding_rate
            else:
                cost = -notional * event.funding_rate

            total_funding += cost

        # fee_abs is positive = cost (paid), negative = income (received)
        return CostResult(
            fee_pct=0.0,
            fee_abs=abs(total_funding),
            slippage_pct=0.0,
            slippage_abs=0.0,
            adjusted_price=entry_price,
            net_amount=-total_funding,  # negative = net cost to P&L
        )

    def accrue_simple(
        self,
        side: Side,
        notional: float,
        funding_events: Sequence[FundingEvent],
    ) -> float:
        """Simplified funding accrual for SignalExecutor (legacy positions).

        Returns net funding amount (negative = cost to P&L).
        """
        if not self._enabled or not funding_events:
            return 0.0

        total = 0.0
        for event in funding_events:
            if side == Side.LONG:
                total += notional * event.funding_rate
            else:
                total -= notional * event.funding_rate
        return -total  # negative = cost to P&L
