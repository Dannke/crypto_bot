"""Value objects (dataclasses) passed between layers.

These are intentionally lightweight and dependency-light (only stdlib + the
local enums). Infrastructure layers (data/execution/storage) populate them;
strategy layers (indicators/signal/scorer/risk) consume and produce them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .enums import Mode, OrderStatus, OrderType, RejectReason, Side, Signal, TradeStatus


# --------------------------------------------------------------------------- #
# Market data
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class Candle:
    """A single OHLCV bar. `timestamp` is millisecond epoch (ccxt convention)."""

    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(slots=True)
class Quote:
    """Top-of-book / ticker snapshot used for spread & liquidity filters."""

    symbol: str
    bid: float
    ask: float
    last: float
    spread: float          # absolute: ask - bid
    spread_pct: float      # spread / mid * 100
    quote_volume_24h: float
    timestamp: int


@dataclass(slots=True)
class BookSnapshot:
    """Order-book snapshot for depth / slippage estimation."""

    symbol: str
    bids: list[list[float]]   # [[price, qty], ...] sorted desc by price
    asks: list[list[float]]   # [[price, qty], ...] sorted asc by price
    timestamp: int


# --------------------------------------------------------------------------- #
# Strategy outputs
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class FeatureSet:
    """Normalized per-symbol, per-timeframe indicator snapshot.

    Extended to support ML-ready features including technical indicators,
    market microstructure, and future ML inputs (news, on-chain, sentiment).
    """

    symbol: str
    timeframe: str
    trend_score: float        # 0..1
    momentum_score: float     # 0..1
    volatility_score: float   # 0..1
    volume_score: float       # 0..1
    adx: float
    rsi: float
    atr_pct: float            # ATR / close * 100
    ema_fast: float
    ema_mid: float
    ema_slow: float

    # Extended technical indicators
    macd: float = 0.0
    macd_signal: float = 0.0
    macd_histogram: float = 0.0
    bb_upper: float = 0.0
    bb_mid: float = 0.0
    bb_lower: float = 0.0
    bb_position: float = 0.0  # 0..1 position within bands
    vwap: float = 0.0

    # Market microstructure
    liquidity_score: float = 0.0  # 0..1 based on volume/depth
    spread_pct: float = 0.0
    bid_ask_imbalance: float = 0.0  # -1..1

    # Correlation features
    correlation_btc: float = 0.0  # correlation with BTC
    correlation_eth: float = 0.0  # correlation with ETH

    # Market regime
    market_regime: str = "neutral"  # "bull", "bear", "neutral", "choppy"

    # Future ML inputs (placeholders for now)
    news_score: float = 0.0
    on_chain_score: float = 0.0
    sentiment_score: float = 0.0

    # Raw OHLCV for reference
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: float = 0.0

    # Additional metadata
    extras: dict[str, float] = field(default_factory=dict)


@dataclass(slots=True)
class SignalResult:
    """Directional decision from the signal engine for one symbol."""

    symbol: str
    signal: Signal
    side: Side | None          # LONG/SHORT, or None when HOLD
    confidence: float             # 0..1, cross-timeframe agreement
    by_timeframe: dict[str, Signal]
    reason: str


@dataclass(slots=True)
class ScoredCandidate:
    """A ranked, risk-annotated trade idea ready for the risk manager."""

    symbol: str
    signal: Signal
    side: Side | None
    score: float                  # 0..100
    confidence: float             # 0..1
    entry: float
    stop: float
    take: float
    risk_pct: float               # stop distance as % of entry
    features: dict[str, float] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Position & journaling
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class Position:
    """An open or historical position (paper or live)."""

    id: int = 0
    symbol: str = ""
    timeframe: str = ""
    side: Side = Side.LONG
    size: float = 0.0            # in base currency
    entry_price: float = 0.0
    stop: float = 0.0
    take: float = 0.0
    opened_at: datetime | None = None
    closed_by: str | None = None  # stop_loss | take_profit | manual | signal
    status: TradeStatus = TradeStatus.OPEN
    closed_at: datetime | None = None
    exit_price: float | None = None
    pnl_pct: float | None = None


@dataclass(slots=True)
class Trade:
    """A recorded fill/execution attached to a position."""

    position_id: int
    symbol: str
    side: Side
    order_type: OrderType
    size: float
    price: float
    status: OrderStatus
    ts_ms: int
    mode: Mode
    external_id: str | None = None
    id: int | None = None


@dataclass(slots=True)
class DecisionRecord:
    """An audit entry for the decision journal (accepted or rejected)."""

    timestamp: datetime
    symbol: str
    accepted: bool
    reason: RejectReason | None   # None when accepted
    detail: str
    score: float | None = None
    signal: Signal | None = None
