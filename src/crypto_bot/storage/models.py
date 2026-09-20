"""Core domain models (dataclasses) for the storage layer.

These are the canonical domain types used throughout the application.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum

from ..core.enums import Mode, OrderStatus, RejectReason, Side, TradeStatus
from ..core.enums import Signal as SignalValue
from ..core.types import Candle as CoreCandle

# Re-export core types for backward compatibility
Candle = CoreCandle


@dataclass(frozen=True, slots=True)
class Signal:
    """A trading signal from a strategy."""

    ts_ms: int
    symbol: str
    signal: SignalValue
    side: Side | None = None
    confidence: float = 0.0
    score: float | None = None
    timeframe: str | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class Decision:
    """A decision to accept or reject a candidate signal."""

    ts_ms: int
    symbol: str
    timeframe: str
    accepted: bool
    reject_reason: RejectReason | None = None
    detail: str = ""
    score: float | None = None
    signal: SignalValue | None = None
    outcome: str | None = None


@dataclass(frozen=True, slots=True)
class Position:
    """A trading position."""

    id: int = 0
    symbol: str = ""
    timeframe: str = ""
    side: Side = Side.LONG
    size: float = 0.0
    entry_price: float = 0.0
    stop: float = 0.0
    take: float = 0.0
    status: TradeStatus = TradeStatus.OPEN
    closed_by: str | None = None
    opened_at_ms: int = 0
    closed_at_ms: int | None = None
    exit_price: float | None = None
    pnl_pct: float | None = None
    mode: Mode = Mode.PAPER
    created_at: str = ""
    updated_at: str = ""

    @property
    def opened_at(self):
        """Convert opened_at_ms to datetime (UTC)."""
        if self.opened_at_ms:
            return datetime.fromtimestamp(self.opened_at_ms / 1000, tz=UTC)
        return None

    @property
    def closed_at(self):
        """Convert closed_at_ms to datetime (UTC)."""
        if self.closed_at_ms:
            return datetime.fromtimestamp(self.closed_at_ms / 1000, tz=UTC)
        return None


@dataclass(frozen=True, slots=True)
class Order:
    """An order for execution."""

    id: int = 0
    position_id: int = 0
    symbol: str = ""
    side: Side = Side.LONG
    order_type: str = "market"
    size: float = 0.0
    price: float = 0.0
    status: OrderStatus = OrderStatus.PENDING
    ts_ms: int = 0
    mode: Mode = Mode.PAPER


class OrderStatus(Enum):
    PENDING = "pending"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class Trade:
    """A trading position."""

    position_id: int
    symbol: str
    side: Side
    order_type: str = "market"
    size: float = 0.0
    price: float = 0.0
    status: OrderStatus = OrderStatus.FILLED
    ts_ms: int = 0
    mode: Mode = Mode.PAPER
    id: int = 0
    external_id: str | None = None
    created_at: str = ""


@dataclass(frozen=True, slots=True)
class TradeRecord:
    """A record of a trade for the trade history."""

    id: int = 0
    position_id: int = 0
    symbol: str = ""
    side: Side = Side.LONG
    order_type: str = "market"
    size: float = 0.0
    price: float = 0.0
    status: OrderStatus = OrderStatus.FILLED
    ts_ms: int = 0
    mode: Mode = Mode.PAPER


@dataclass(frozen=True, slots=True)
class EquityPoint:
    """A point on the equity curve."""

    ts_ms: int
    currency: str
    equity: float
    drawdown_pct: float
    mode: Mode = Mode.PAPER


@dataclass(frozen=True, slots=True)
class SignalRecord:
    """A signal record for storage."""

    ts_ms: int
    symbol: str
    signal: str
    side: str | None = None
    confidence: float = 0.0
    score: float | None = None
    timeframe: str = ""
    reason: str = ""
    created_at: str = ""


class OrderStatus(Enum):
    PENDING = "pending"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"