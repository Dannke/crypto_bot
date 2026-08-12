"""Stop-loss and take-profit calculator.

Computes optimal stop-loss and take-profit levels based on volatility,
risk parameters, and reward-to-risk ratios.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SLTPLevels:
    """Calculated stop-loss and take-profit levels."""

    stop_loss: float
    take_profit: float
    stop_distance_pct: float
    take_distance_pct: float
    reward_risk_ratio: float


class SLTPCalculator:
    """Calculator for stop-loss and take-profit levels.

    Uses ATR (Average True Range) to set dynamic levels that adapt to
    market volatility while respecting configured risk limits.
    """

    def __init__(
        self,
        max_stop_distance_pct: float = 3.0,  # maximum 3% stop distance
        atr_multiplier: float = 1.5,  # stop at 1.5x ATR
        reward_risk_ratio: float = 2.0,  # TP at 2x the stop distance
    ) -> None:
        self._max_stop_distance_pct = max_stop_distance_pct
        self._atr_multiplier = atr_multiplier
        self._reward_risk_ratio = reward_risk_ratio

    def calculate(
        self,
        entry_price: float,
        side,
        atr_pct: float,
        reward_risk_ratio: float | None = None,
    ) -> SLTPLevels:
        """Calculate SL/TP levels for a position.

        Args:
            entry_price: The entry price of the position.
            side: LONG or SHORT.
            atr_pct: ATR as a percentage of price.
            reward_risk_ratio: Optional per-call override of the instance default.

        Returns:
            SLTPLevels with calculated stop-loss and take-profit.
        """
        # Calculate stop distance (tighter of max cap or ATR-based)
        atr_distance_pct = min(self._max_stop_distance_pct, self._atr_multiplier * atr_pct)
        stop_distance_pct = max(0.1, atr_distance_pct)  # minimum 0.1%

        # Calculate take-profit distance based on reward:risk ratio
        ratio = reward_risk_ratio if reward_risk_ratio is not None else self._reward_risk_ratio
        take_distance_pct = stop_distance_pct * ratio

        # Calculate absolute levels
        if side.value == "LONG":
            stop_loss = entry_price * (1 - stop_distance_pct / 100.0)
            take_profit = entry_price * (1 + take_distance_pct / 100.0)
        else:  # SHORT
            stop_loss = entry_price * (1 + stop_distance_pct / 100.0)
            take_profit = entry_price * (1 - take_distance_pct / 100.0)

        return SLTPLevels(
            stop_loss=round(stop_loss, 8),
            take_profit=round(take_profit, 8),
            stop_distance_pct=round(stop_distance_pct, 4),
            take_distance_pct=round(take_distance_pct, 4),
            reward_risk_ratio=ratio,
        )

    def calculate_fixed(
        self,
        entry_price: float,
        side,
        stop_distance_pct: float,
        reward_risk_ratio: float | None = None,
    ) -> SLTPLevels:
        """Calculate SL/TP levels with fixed stop distance.

        Args:
            entry_price: The entry price of the position.
            side: LONG or SHORT.
            stop_distance_pct: Fixed stop distance as percentage.
            reward_risk_ratio: Optional override for reward:risk ratio.

        Returns:
            SLTPLevels with calculated stop-loss and take-profit.
        """
        ratio = reward_risk_ratio or self._reward_risk_ratio
        take_distance_pct = stop_distance_pct * ratio

        if side.value == "LONG":
            stop_loss = entry_price * (1 - stop_distance_pct / 100.0)
            take_profit = entry_price * (1 + take_distance_pct / 100.0)
        else:  # SHORT
            stop_loss = entry_price * (1 + stop_distance_pct / 100.0)
            take_profit = entry_price * (1 - take_distance_pct / 100.0)

        return SLTPLevels(
            stop_loss=round(stop_loss, 8),
            take_profit=round(take_profit, 8),
            stop_distance_pct=round(stop_distance_pct, 4),
            take_distance_pct=round(take_distance_pct, 4),
            reward_risk_ratio=ratio,
        )
