"""Fee calculator: estimates trading fees for paper trading.

Provides realistic fee estimates based on exchange fee structures,
trading volume, and order types.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class FeeTier(StrEnum):
    """Fee tiers based on trading volume."""
    TIER_1 = "tier_1"  # < 10M volume
    TIER_2 = "tier_2"  # 10M-50M volume
    TIER_3 = "tier_3"  # 50M-500M volume
    TIER_4 = "tier_4"  # > 500M volume


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    """Fee schedule for an exchange."""

    maker_fee_pct: float = 0.1  # 0.1% maker fee
    taker_fee_pct: float = 0.1  # 0.1% taker fee
    base_discount: float = 0.0  # base discount percentage


@dataclass(frozen=True, slots=True)
class FeeResult:
    """Result of fee calculation."""

    fee_pct: float
    fee_abs: float
    net_amount: float


class FeeCalculator:
    """Calculator for trading fees.

    Supports different fee structures based on exchange, order type,
    and trading volume tiers.
    """

    def __init__(
        self,
        schedule: FeeSchedule | None = None,
        tier: FeeTier = FeeTier.TIER_1,
    ) -> None:
        self._schedule = schedule or FeeSchedule()
        self._tier = tier

    def calculate(
        self,
        amount: float,
        price: float,
        is_maker: bool = True,
    ) -> FeeResult:
        """Calculate fee for a trade.

        Args:
            amount: The amount being traded (in base currency).
            price: The price of the trade (in quote currency).
            is_maker: Whether the order is a maker (limit) or taker (market).

        Returns:
            FeeResult with fee percentage and absolute amount.
        """
        notional = amount * price

        # Select fee based on order type
        base_fee_pct = self._schedule.maker_fee_pct if is_maker else self._schedule.taker_fee_pct

        # Apply tier-based discount
        discount = self._get_tier_discount()
        fee_pct = base_fee_pct * (1 - discount)

        # Calculate absolute fee
        fee_abs = notional * (fee_pct / 100.0)
        net_amount = notional - fee_abs

        return FeeResult(
            fee_pct=round(fee_pct, 4),
            fee_abs=round(fee_abs, 8),
            net_amount=round(net_amount, 8),
        )

    def calculate_round_trip(
        self,
        amount: float,
        entry_price: float,
        exit_price: float,
        is_maker_entry: bool = True,
        is_maker_exit: bool = True,
    ) -> tuple[FeeResult, FeeResult]:
        """Calculate fees for a complete round-trip trade.

        Args:
            amount: The amount being traded.
            entry_price: Entry price.
            exit_price: Exit price.
            is_maker_entry: Whether entry order is maker.
            is_maker_exit: Whether exit order is maker.

        Returns:
            Tuple of (entry_fee_result, exit_fee_result).
        """
        entry_fee = self.calculate(amount, entry_price, is_maker_entry)
        exit_fee = self.calculate(amount, exit_price, is_maker_exit)
        return entry_fee, exit_fee

    def _get_tier_discount(self) -> float:
        """Get discount based on volume tier."""
        tier_discounts = {
            FeeTier.TIER_1: 0.0,
            FeeTier.TIER_2: 0.1,  # 10% discount
            FeeTier.TIER_3: 0.2,  # 20% discount
            FeeTier.TIER_4: 0.3,  # 30% discount
        }
        return tier_discounts.get(self._tier, 0.0) + self._schedule.base_discount

    def set_tier(self, tier: FeeTier) -> None:
        """Update the fee tier."""
        self._tier = tier
