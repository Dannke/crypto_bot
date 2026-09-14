"""Domain enumerations.

These are the only vocabulary the rest of the system uses to describe modes,
signals, trade sides, order types and lifecycle states. Keeping them in one
place avoids stringly-typed comparisons scattered across modules.

Using ``StrEnum`` (Python 3.11+) instead of the legacy ``class X(str, Enum)``
idiom: values compare and serialise as plain strings without the
``str.__str__`` quirks of the dual-inheritance form.
"""
from __future__ import annotations

from enum import StrEnum


class Mode(StrEnum):
    """Operating mode of the bot."""

    SIGNAL_ONLY = "signal_only"   # compute & log signals, no order simulation
    PAPER = "paper"               # simulate orders against market data
    LIVE = "live"                 # place real orders (gated by env + readiness)


class Signal(StrEnum):
    """User-facing recommendation for a symbol."""

    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class Side(StrEnum):
    """Direction of a position or order."""

    LONG = "LONG"
    SHORT = "SHORT"


class OrderType(StrEnum):
    """Order placement type."""

    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(StrEnum):
    """Lifecycle of an order/fill record."""

    PENDING = "pending"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class TradeStatus(StrEnum):
    """Lifecycle of a position/trade record."""

    PROPOSED = "proposed"     # candidate produced, not yet acted upon
    OPEN = "open"             # accepted & active (paper or live)
    CLOSED = "closed"         # exited (stop / take / manual)
    REJECTED = "rejected"     # candidate was filtered out
    CANCELLED = "cancelled"   # proposed but then withdrawn before fill


class StrategyType(StrEnum):
    """The decision layer at which a strategy operates."""

    CANDIDATE = "candidate"
    PORTFOLIO = "portfolio"


class MarketRegime(StrEnum):
    """Market regime classification for regime-aware strategies.

    Two-axis classification:
    - Axis 1 (trend vs range): ADX-based trend strength
    - Axis 2 (vol regime): Rolling percentile of realized volatility/ATR%

    Values are lower_snake_case strings.
    """

    TREND_LOW_VOL = "trend_low_vol"
    TREND_HIGH_VOL = "trend_high_vol"
    RANGE_LOW_VOL = "range_low_vol"
    RANGE_HIGH_VOL = "range_high_vol"


class PortfolioRejectReason(StrEnum):
    """Why a portfolio position intent was rejected by the risk engine.

    Kept distinct from :class:`RejectReason` (candidate-level vocabulary);
    a portfolio-level rejection maps onto one of these values when a
    ``DecisionReport`` is produced downstream.
    """

    REJECT_MAX_POSITIONS = "REJECT_MAX_POSITIONS"
    REJECT_MAX_POSITION_WEIGHT = "REJECT_MAX_POSITION_WEIGHT"
    REJECT_MAX_GROSS_EXPOSURE = "REJECT_MAX_GROSS_EXPOSURE"
    REJECT_MAX_NET_EXPOSURE = "REJECT_MAX_NET_EXPOSURE"
    REJECT_MAX_LEVERAGE = "REJECT_MAX_LEVERAGE"
    REJECT_MIN_NOTIONAL = "REJECT_MIN_NOTIONAL"
    REJECT_CORRELATION = "REJECT_CORRELATION"


class RejectReason(StrEnum):
    """Why a candidate was excluded. Recorded in the decision journal."""

    INSUFFICIENT_LIQUIDITY = "insufficient_liquidity"
    SPREAD_TOO_WIDE = "spread_too_wide"
    VOLATILITY_OUT_OF_RANGE = "volatility_out_of_range"
    LOW_SCORE = "low_score"
    LOW_CONFIDENCE = "low_confidence"
    CONFLICTING_TIMEFRAMES = "conflicting_timeframes"
    IN_COOLDOWN = "in_cooldown"
    POSITION_EXISTS = "position_exists"
    BLACKLISTED = "blacklisted"
    INSUFFICIENT_DATA = "insufficient_data"
    RISK_BUDGET_EXHAUSTED = "risk_budget_exhausted"
    MAX_POSITIONS_REACHED = "max_positions_reached"
    DRAWDOWN_HALT = "drawdown_halt"
    NO_DIRECTION = "no_direction"


class ExecutorOutcome(StrEnum):
    """Final verdict of SignalExecutor on a pipeline-selected candidate."""

    POSITION_OPENED = "position_opened"
    DRAWDOWN_HALT = "drawdown_halt"
    SLOT_TAKEN = "slot_taken"
    MAX_POSITIONS_REACHED = "max_positions_reached"
    OPEN_UNREALIZED_DRAWDOWN = "open_unrealized_drawdown"
    NO_POSITION = "no_position"  # signal_only mode
