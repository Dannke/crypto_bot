"""Paper position: virtual position for paper trading.

Simulates a trading position without real order execution.
Tracks entry, exit, size, and current state for P&L calculation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from ..core.enums import Side, TradeStatus


@dataclass(slots=True)
class PaperPosition:
    """A virtual trading position for paper trading simulation."""

    symbol: str
    timeframe: str
    side: Side
    size: float  # in base currency
    entry_price: float
    stop_loss: float
    take_profit: float
    entry_time: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    status: TradeStatus = TradeStatus.OPEN
    exit_price: float | None = None
    exit_time: datetime | None = None
    pnl_pct: float | None = None
    pnl_abs: float | None = None
    closed_by: str | None = None  # stop_loss | take_profit | manual | signal

    @property
    def is_open(self) -> bool:
        """Whether the position is currently open."""
        return self.status == TradeStatus.OPEN

    @property
    def is_closed(self) -> bool:
        """Whether the position has been closed."""
        return self.status == TradeStatus.CLOSED

    def unrealized_pnl_pct(self, current_price: float) -> float:
        """Calculate unrealized P&L as percentage of entry."""
        if not self.is_open:
            return 0.0

        if self.side == Side.LONG:
            return ((current_price - self.entry_price) / self.entry_price) * 100.0
        return ((self.entry_price - current_price) / self.entry_price) * 100.0

    def unrealized_pnl_abs(self, current_price: float) -> float:
        """Calculate unrealized P&L in quote currency."""
        if not self.is_open:
            return 0.0

        if self.side == Side.LONG:
            return (current_price - self.entry_price) * self.size
        return (self.entry_price - current_price) * self.size

    def close(self, exit_price: float, exit_time: datetime | None = None, *,
              closed_by: str | None = None) -> None:
        """Close the position at the given price."""
        if not self.is_open:
            raise ValueError("Position is already closed")

        self.exit_price = exit_price
        self.exit_time = exit_time or datetime.now(tz=UTC)
        self.status = TradeStatus.CLOSED
        self.closed_by = closed_by

        # Calculate realized P&L
        if self.side == Side.LONG:
            self.pnl_abs = (self.exit_price - self.entry_price) * self.size
        else:  # SHORT
            self.pnl_abs = (self.entry_price - self.exit_price) * self.size

        self.pnl_pct = (self.pnl_abs / (self.entry_price * self.size)) * 100.0

    def check_stop_loss(self, current_price: float) -> bool:
        """Check if stop-loss should be triggered."""
        if not self.is_open:
            return False

        if self.side == Side.LONG:
            return current_price <= self.stop_loss
        else:  # SHORT
            return current_price >= self.stop_loss

    def check_take_profit(self, current_price: float) -> bool:
        """Check if take-profit should be triggered."""
        if not self.is_open:
            return False

        if self.side == Side.LONG:
            return current_price >= self.take_profit
        else:  # SHORT
            return current_price <= self.take_profit

    def to_dict(self) -> dict:
        """Convert position to dictionary for logging/serialization."""
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "side": self.side.value,
            "size": self.size,
            "entry_price": self.entry_price,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "entry_time": self.entry_time.isoformat(),
            "status": self.status.value,
            "exit_price": self.exit_price,
            "exit_time": self.exit_time.isoformat() if self.exit_time else None,
            "closed_by": self.closed_by,
            "pnl_pct": self.pnl_pct,
            "pnl_abs": self.pnl_abs,
        }
