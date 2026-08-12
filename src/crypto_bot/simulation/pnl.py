"""P&L calculator: tracks and calculates profit and loss for paper trading.

Provides comprehensive P&L tracking including realized and unrealized
gains/losses, win rate, and performance metrics.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

from ..core.enums import Side

if TYPE_CHECKING:
    from .paper_position import PaperPosition


@dataclass(frozen=True, slots=True)
class PnLSummary:
    """Summary of P&L performance over a period."""

    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    total_pnl_abs: float = 0.0
    total_pnl_pct: float = 0.0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    max_profit_pct: float = 0.0
    sharpe_ratio: float = 0.0
    closed_by_sl: int = 0
    closed_by_tp: int = 0
    closed_by_other: int = 0
    pnl_from_sl: float = 0.0
    pnl_from_tp: float = 0.0


@dataclass(slots=True)
class PnLTracker:
    """Tracks P&L across multiple positions for paper trading."""

    positions: list[PaperPosition] = field(default_factory=list)
    initial_equity: float = 10000.0
    current_equity: float = 10000.0
    peak_equity: float = 10000.0
    daily_peak_equity: float = 10000.0
    equity_history: list[tuple[datetime, float]] = field(default_factory=list)
    _last_reset_day: date | None = None

    def add_position(self, position: PaperPosition) -> None:
        """Add a position to the tracker."""
        self.positions.append(position)
        self._update_equity()

    def close_position(self, position: PaperPosition, exit_price: float, *,
                       closed_by: str | None = None, exit_fee_abs: float = 0.0) -> None:
        """Close a position and update P&L.

        Args:
            position: The position to close.
            exit_price: Price at which the position is closed.
            closed_by: Reason for closing.
            exit_fee_abs: Exit commission, subtracted from gross P&L.
        """
        position.close(exit_price, closed_by=closed_by, exit_fee_abs=exit_fee_abs)
        self._update_equity()
        self.equity_history.append((datetime.now(tz=UTC), self.current_equity))

    def _update_equity(self) -> None:
        """Update current equity based on closed positions."""
        realized_pnl = sum(p.pnl_abs or 0.0 for p in self.positions if p.is_closed)
        self.current_equity = self.initial_equity + realized_pnl
        self.peak_equity = max(self.peak_equity, self.current_equity)

    def reset_daily_peak_if_day_changed(self, timestamp: datetime) -> None:
        """Reset daily_peak_equity at the start of a new trading day.

        Called before drawdown checks in _check_risk_limits.  Uses its own
        _last_reset_day so it is not masked by record_equity updating
        _last_tick_day on every tick.
        """
        tick_day: date = timestamp.date()
        if self._last_reset_day is not None and tick_day != self._last_reset_day:
            self.daily_peak_equity = self.current_equity
        self._last_reset_day = tick_day

    def get_summary(self) -> PnLSummary:
        """Calculate P&L summary statistics."""
        closed_positions = [p for p in self.positions if p.is_closed]

        if not closed_positions:
            return PnLSummary()

        total_trades = len(closed_positions)
        winning_trades = sum(1 for p in closed_positions if (p.pnl_pct or 0) > 0)
        losing_trades = total_trades - winning_trades

        win_rate = winning_trades / total_trades if total_trades > 0 else 0.0

        total_pnl_abs = sum(p.pnl_abs or 0.0 for p in closed_positions)
        total_pnl_pct = (total_pnl_abs / self.initial_equity) * 100.0

        wins = [p.pnl_pct or 0.0 for p in closed_positions if (p.pnl_pct or 0) > 0]
        losses = [p.pnl_pct or 0.0 for p in closed_positions if (p.pnl_pct or 0) < 0]

        avg_win_pct = sum(wins) / len(wins) if wins else 0.0
        avg_loss_pct = sum(losses) / len(losses) if losses else 0.0

        # Calculate closed-by breakdown
        closed_by_sl = sum(1 for p in closed_positions if p.closed_by == "stop_loss")
        closed_by_tp = sum(1 for p in closed_positions if p.closed_by == "take_profit")

        pnl_from_sl = sum(p.pnl_abs or 0.0 for p in closed_positions if p.closed_by == "stop_loss")
        pnl_from_tp = sum(p.pnl_abs or 0.0 for p in closed_positions if p.closed_by == "take_profit")

        closed_by_other = total_trades - closed_by_sl - closed_by_tp

        # Calculate max drawdown from equity curve with running peak
        max_drawdown_pct = 0.0
        if len(self.equity_history) > 1:
            peak = self.equity_history[0][1]
            for _, equity in self.equity_history:
                if equity > peak:
                    peak = equity
                drawdown = (peak - equity) / peak * 100.0
                max_drawdown_pct = max(max_drawdown_pct, drawdown)

        # Calculate max profit
        max_profit_pct = (self.peak_equity - self.initial_equity) / self.initial_equity * 100.0

        # Calculate Sharpe ratio from equity history returns
        sharpe_ratio = 0.0
        if len(self.equity_history) > 2:
            equities = [e for _, e in self.equity_history]
            returns = [(equities[i] - equities[i-1]) / equities[i-1]
                       for i in range(1, len(equities))]
            returns = [r for r in returns if abs(r) < 0.5]  # filter outliers
            if len(returns) > 1:
                mean_ret = sum(returns) / len(returns)
                var_ret = sum((r - mean_ret) ** 2 for r in returns) / (len(returns) - 1)
                if var_ret > 0:
                    std_ret = math.sqrt(var_ret)
                    sharpe_ratio = (mean_ret / std_ret) * math.sqrt(365)

        return PnLSummary(
            total_trades=total_trades,
            winning_trades=winning_trades,
            losing_trades=losing_trades,
            win_rate=round(win_rate, 4),
            total_pnl_abs=round(total_pnl_abs, 2),
            total_pnl_pct=round(total_pnl_pct, 2),
            avg_win_pct=round(avg_win_pct, 2),
            avg_loss_pct=round(avg_loss_pct, 2),
            max_drawdown_pct=round(max_drawdown_pct, 2),
            max_profit_pct=round(max_profit_pct, 2),
            sharpe_ratio=round(sharpe_ratio, 2),
            closed_by_sl=closed_by_sl,
            closed_by_tp=closed_by_tp,
            closed_by_other=closed_by_other,
            pnl_from_sl=round(pnl_from_sl, 2),
            pnl_from_tp=round(pnl_from_tp, 2),
        )

    def open_symbols(self) -> set[str]:
        return {p.symbol for p in self.positions if p.is_open}

    def open_symbol_timeframes(self) -> dict[str, set[str]]:
        result: dict[str, set[str]] = {}
        for p in self.positions:
            if p.is_open:
                result.setdefault(p.symbol, set()).add(p.timeframe)
        return result

    def unrealized_pnl(self, current_prices: dict[tuple[str, str], float]) -> float:
        total = 0.0
        for pos in self.positions:
            if not pos.is_open:
                continue
            price = current_prices.get((pos.symbol, pos.timeframe))
            if price is None:
                continue
            if pos.side == Side.LONG:
                total += (price - pos.entry_price) * pos.size
            else:
                total += (pos.entry_price - price) * pos.size
        return total

    def mark_to_market_equity(self, current_prices: dict[tuple[str, str], float]) -> float:
        return self.current_equity + self.unrealized_pnl(current_prices)

    def record_equity(self, timestamp: datetime, equity: float) -> None:
        self.equity_history.append((timestamp, equity))
        self.current_equity = equity
        if equity > self.peak_equity:
            self.peak_equity = equity
        if equity > self.daily_peak_equity:
            self.daily_peak_equity = equity

    def get_open_positions_pnl(self, current_prices: dict[str, float]) -> dict[str, float]:
        """Get unrealized P&L for all open positions.

        Args:
            current_prices: Dictionary of symbol -> current price.

        Returns:
            Dictionary of symbol -> unrealized P&L percentage.
        """
        unrealized = {}
        for position in self.positions:
            if position.is_open and position.symbol in current_prices:
                current_price = current_prices[position.symbol]
                unrealized[position.symbol] = position.unrealized_pnl_pct(current_price)
        return unrealized
