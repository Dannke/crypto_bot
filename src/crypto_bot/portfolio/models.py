"""Immutable portfolio-domain DTOs.

These objects model snapshots and hand-offs between portfolio construction and
execution.  They intentionally validate only data shape and value domains;
portfolio selection, sizing, and order-routing policies belong in services.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from datetime import datetime
from math import isfinite
from types import MappingProxyType

from ..core.enums import Mode, OrderType, Side, TradeStatus
from ..core.types import FeatureSet, Position

Scalar = str | int | float | bool | None


def _non_empty(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")


def _timestamp(value: int, field_name: str = "as_of_ms") -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer timestamp in milliseconds")


def _finite(value: float, field_name: str, *, positive: bool = False, non_negative: bool = False) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(float(value)):
        raise ValueError(f"{field_name} must be a finite number")
    if positive and value <= 0:
        raise ValueError(f"{field_name} must be greater than zero")
    if non_negative and value < 0:
        raise ValueError(f"{field_name} must be non-negative")


def _symbol(value: str, field_name: str = "symbol") -> None:
    _non_empty(value, field_name)
    base_quote = value.split("/")
    if len(base_quote) != 2 or not all(part.strip() for part in base_quote):
        raise ValueError(f"{field_name} must use BASE/QUOTE format")


def _string_value(data: Mapping[str, object], key: str) -> str:
    """Read a required string from a JSON-compatible mapping."""
    value = data[key]
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


def _datetime_to_value(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _datetime_from_value(value: object, field_name: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be an ISO-8601 string or null")
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 string or null") from exc


def _feature_to_dict(feature: FeatureSet) -> dict[str, Scalar | dict[str, float]]:
    """Serialize the existing FeatureSet without changing its contract."""
    return {field.name: getattr(feature, field.name) for field in fields(feature)}


def _feature_from_dict(data: Mapping[str, object]) -> FeatureSet:
    """Restore a FeatureSet from a JSON-compatible representation."""
    return FeatureSet(**dict(data))  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class UniverseSnapshot:
    """The resolved tradable universe at one instant in time."""

    as_of_ms: int
    symbols: tuple[str, ...]
    quote_currency: str = "USDT"
    source: str | None = None

    def __post_init__(self) -> None:
        _timestamp(self.as_of_ms)
        _non_empty(self.quote_currency, "quote_currency")
        if self.source is not None:
            _non_empty(self.source, "source")
        if not isinstance(self.symbols, tuple):
            object.__setattr__(self, "symbols", tuple(self.symbols))
        if not self.symbols:
            raise ValueError("symbols must not be empty")
        for symbol in self.symbols:
            _symbol(symbol, "symbols item")
        if len(set(self.symbols)) != len(self.symbols):
            raise ValueError("symbols must be unique")

    def to_dict(self) -> dict[str, Scalar | list[str]]:
        return {
            "as_of_ms": self.as_of_ms,
            "symbols": list(self.symbols),
            "quote_currency": self.quote_currency,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> UniverseSnapshot:
        return cls(
            as_of_ms=data["as_of_ms"],  # type: ignore[arg-type]
            symbols=tuple(data["symbols"]),  # type: ignore[arg-type]
            quote_currency=data.get("quote_currency", "USDT"),  # type: ignore[arg-type]
            source=data.get("source"),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class CrossSectionalFeatureSnapshot:
    """Feature snapshots for the symbols available in one universe snapshot."""

    as_of_ms: int
    universe: UniverseSnapshot
    features_by_symbol: Mapping[str, FeatureSet]

    def __post_init__(self) -> None:
        _timestamp(self.as_of_ms)
        if not isinstance(self.universe, UniverseSnapshot):
            raise ValueError("universe must be a UniverseSnapshot")
        if self.as_of_ms != self.universe.as_of_ms:
            raise ValueError("as_of_ms must match universe.as_of_ms")
        features = dict(self.features_by_symbol)
        if not features:
            raise ValueError("features_by_symbol must not be empty")
        for symbol, feature in features.items():
            _symbol(symbol, "features_by_symbol key")
            if symbol not in self.universe.symbols:
                raise ValueError("features_by_symbol contains a symbol outside the universe")
            if not isinstance(feature, FeatureSet):
                raise ValueError("features_by_symbol values must be FeatureSet instances")
            if feature.symbol != symbol:
                raise ValueError("FeatureSet.symbol must match its mapping key")
        object.__setattr__(self, "features_by_symbol", MappingProxyType(features))

    def to_dict(self) -> dict[str, object]:
        return {
            "as_of_ms": self.as_of_ms,
            "universe": self.universe.to_dict(),
            "features_by_symbol": {
                symbol: _feature_to_dict(feature)
                for symbol, feature in self.features_by_symbol.items()
            },
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> CrossSectionalFeatureSnapshot:
        universe_raw = data["universe"]
        features_raw = data["features_by_symbol"]
        if not isinstance(universe_raw, Mapping) or not isinstance(features_raw, Mapping):
            raise ValueError("universe and features_by_symbol must be mappings")
        features: dict[str, FeatureSet] = {}
        for symbol, feature in features_raw.items():
            if not isinstance(symbol, str) or not isinstance(feature, Mapping):
                raise ValueError("features_by_symbol must map strings to feature mappings")
            features[symbol] = _feature_from_dict(feature)
        return cls(
            as_of_ms=data["as_of_ms"],  # type: ignore[arg-type]
            universe=UniverseSnapshot.from_dict(universe_raw),
            features_by_symbol=features,
        )


@dataclass(frozen=True, slots=True)
class PositionIntent:
    """A target portfolio allocation for one symbol and direction."""

    symbol: str
    side: Side
    target_weight: float
    timeframe: str | None = None
    reference_price: float | None = None

    def __post_init__(self) -> None:
        _symbol(self.symbol)
        if not isinstance(self.side, Side):
            raise ValueError("side must be a Side")
        _finite(self.target_weight, "target_weight", positive=True)
        if self.timeframe is not None:
            _non_empty(self.timeframe, "timeframe")
        if self.reference_price is not None:
            _finite(self.reference_price, "reference_price", positive=True)

    def to_dict(self) -> dict[str, Scalar]:
        return {
            "symbol": self.symbol,
            "side": self.side.value,
            "target_weight": self.target_weight,
            "timeframe": self.timeframe,
            "reference_price": self.reference_price,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> PositionIntent:
        return cls(
            symbol=_string_value(data, "symbol"),
            side=Side(_string_value(data, "side")),
            target_weight=data["target_weight"],  # type: ignore[arg-type]
            timeframe=data.get("timeframe"),  # type: ignore[arg-type]
            reference_price=data.get("reference_price"),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class PortfolioIntent:
    """A complete portfolio target at one instant, including an all-cash intent."""

    as_of_ms: int
    intents: tuple[PositionIntent, ...]
    universe: UniverseSnapshot | None = None
    strategy_name: str | None = None

    def __post_init__(self) -> None:
        _timestamp(self.as_of_ms)
        if not isinstance(self.intents, tuple):
            object.__setattr__(self, "intents", tuple(self.intents))
        for intent in self.intents:
            if not isinstance(intent, PositionIntent):
                raise ValueError("intents must contain PositionIntent instances")
        keys = [(intent.symbol, intent.timeframe) for intent in self.intents]
        if len(set(keys)) != len(keys):
            raise ValueError("intents must be unique per symbol and timeframe")
        if self.universe is not None:
            if not isinstance(self.universe, UniverseSnapshot):
                raise ValueError("universe must be a UniverseSnapshot")
            if self.as_of_ms != self.universe.as_of_ms:
                raise ValueError("as_of_ms must match universe.as_of_ms")
            unknown = {intent.symbol for intent in self.intents} - set(self.universe.symbols)
            if unknown:
                raise ValueError("intents contain symbols outside the universe")
        if self.strategy_name is not None:
            _non_empty(self.strategy_name, "strategy_name")

    def to_dict(self) -> dict[str, object]:
        return {
            "as_of_ms": self.as_of_ms,
            "intents": [intent.to_dict() for intent in self.intents],
            "universe": self.universe.to_dict() if self.universe else None,
            "strategy_name": self.strategy_name,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> PortfolioIntent:
        universe_raw = data.get("universe")
        intents_raw = data["intents"]
        if not isinstance(intents_raw, (list, tuple)):
            raise ValueError("intents must be a sequence")
        if universe_raw is not None and not isinstance(universe_raw, Mapping):
            raise ValueError("universe must be a mapping or null")
        intents: list[PositionIntent] = []
        for intent in intents_raw:
            if not isinstance(intent, Mapping):
                raise ValueError("intents must contain mappings")
            intents.append(PositionIntent.from_dict(intent))
        return cls(
            as_of_ms=data["as_of_ms"],  # type: ignore[arg-type]
            intents=tuple(intents),
            universe=UniverseSnapshot.from_dict(universe_raw) if universe_raw else None,
            strategy_name=data.get("strategy_name"),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class PortfolioState:
    """Observed portfolio balances and positions at one timestamp."""

    as_of_ms: int
    mode: Mode
    equity: float
    cash: float
    positions: tuple[Position, ...] = ()

    def __post_init__(self) -> None:
        _timestamp(self.as_of_ms)
        if not isinstance(self.mode, Mode):
            raise ValueError("mode must be a Mode")
        _finite(self.equity, "equity", non_negative=True)
        _finite(self.cash, "cash", non_negative=True)
        if not isinstance(self.positions, tuple):
            object.__setattr__(self, "positions", tuple(self.positions))
        if not all(isinstance(position, Position) for position in self.positions):
            raise ValueError("positions must contain Position instances")

    def to_dict(self) -> dict[str, object]:
        return {
            "as_of_ms": self.as_of_ms,
            "mode": self.mode.value,
            "equity": self.equity,
            "cash": self.cash,
            "positions": [
                {
                    **{field.name: getattr(position, field.name) for field in fields(position)},
                    "side": position.side.value,
                    "status": position.status.value,
                    "opened_at": _datetime_to_value(position.opened_at),
                    "closed_at": _datetime_to_value(position.closed_at),
                }
                for position in self.positions
            ],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> PortfolioState:
        positions_raw = data.get("positions", [])
        if not isinstance(positions_raw, (list, tuple)):
            raise ValueError("positions must be a sequence")
        positions = []
        for raw_position in positions_raw:
            if not isinstance(raw_position, Mapping):
                raise ValueError("positions must contain mappings")
            position_data = dict(raw_position)
            side_value = position_data["side"]
            status_value = position_data["status"]
            if not isinstance(side_value, str) or not isinstance(status_value, str):
                raise ValueError("position side and status must be strings")
            position_data["side"] = Side(side_value)
            position_data["status"] = TradeStatus(status_value)
            position_data["opened_at"] = _datetime_from_value(
                position_data.get("opened_at"), "opened_at"
            )
            position_data["closed_at"] = _datetime_from_value(
                position_data.get("closed_at"), "closed_at"
            )
            positions.append(Position(**position_data))
        return cls(
            as_of_ms=data["as_of_ms"],  # type: ignore[arg-type]
            mode=Mode(_string_value(data, "mode")),
            equity=data["equity"],  # type: ignore[arg-type]
            cash=data["cash"],  # type: ignore[arg-type]
            positions=tuple(positions),
        )


@dataclass(frozen=True, slots=True)
class OrderRequest:
    """An order command ready for an execution adapter."""

    client_order_id: str
    symbol: str
    side: Side
    order_type: OrderType
    quantity: float
    requested_at_ms: int
    mode: Mode
    limit_price: float | None = None

    def __post_init__(self) -> None:
        _non_empty(self.client_order_id, "client_order_id")
        _symbol(self.symbol)
        if not isinstance(self.side, Side):
            raise ValueError("side must be a Side")
        if not isinstance(self.order_type, OrderType):
            raise ValueError("order_type must be an OrderType")
        _finite(self.quantity, "quantity", positive=True)
        _timestamp(self.requested_at_ms, "requested_at_ms")
        if not isinstance(self.mode, Mode):
            raise ValueError("mode must be a Mode")
        if self.limit_price is not None:
            _finite(self.limit_price, "limit_price", positive=True)
        if self.order_type == OrderType.LIMIT and self.limit_price is None:
            raise ValueError("limit_price is required for limit orders")

    def to_dict(self) -> dict[str, Scalar]:
        return {
            "client_order_id": self.client_order_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "order_type": self.order_type.value,
            "quantity": self.quantity,
            "requested_at_ms": self.requested_at_ms,
            "mode": self.mode.value,
            "limit_price": self.limit_price,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> OrderRequest:
        return cls(
            client_order_id=_string_value(data, "client_order_id"),
            symbol=_string_value(data, "symbol"),
            side=Side(_string_value(data, "side")),
            order_type=OrderType(_string_value(data, "order_type")),
            quantity=data["quantity"],  # type: ignore[arg-type]
            requested_at_ms=data["requested_at_ms"],  # type: ignore[arg-type]
            mode=Mode(_string_value(data, "mode")),
            limit_price=data.get("limit_price"),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class Fill:
    """An immutable execution report for all or part of an order."""

    fill_id: str
    order_id: str
    symbol: str
    side: Side
    quantity: float
    price: float
    filled_at_ms: int
    mode: Mode
    fee: float = 0.0
    fee_currency: str | None = None

    def __post_init__(self) -> None:
        _non_empty(self.fill_id, "fill_id")
        _non_empty(self.order_id, "order_id")
        _symbol(self.symbol)
        if not isinstance(self.side, Side):
            raise ValueError("side must be a Side")
        _finite(self.quantity, "quantity", positive=True)
        _finite(self.price, "price", positive=True)
        _timestamp(self.filled_at_ms, "filled_at_ms")
        if not isinstance(self.mode, Mode):
            raise ValueError("mode must be a Mode")
        _finite(self.fee, "fee", non_negative=True)
        if self.fee_currency is not None:
            _non_empty(self.fee_currency, "fee_currency")

    def to_dict(self) -> dict[str, Scalar]:
        return {
            "fill_id": self.fill_id,
            "order_id": self.order_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "quantity": self.quantity,
            "price": self.price,
            "filled_at_ms": self.filled_at_ms,
            "mode": self.mode.value,
            "fee": self.fee,
            "fee_currency": self.fee_currency,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> Fill:
        return cls(
            fill_id=_string_value(data, "fill_id"),
            order_id=_string_value(data, "order_id"),
            symbol=_string_value(data, "symbol"),
            side=Side(_string_value(data, "side")),
            quantity=data["quantity"],  # type: ignore[arg-type]
            price=data["price"],  # type: ignore[arg-type]
            filled_at_ms=data["filled_at_ms"],  # type: ignore[arg-type]
            mode=Mode(_string_value(data, "mode")),
            fee=data.get("fee", 0.0),  # type: ignore[arg-type]
            fee_currency=data.get("fee_currency"),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class WeightedPosition:
    """A position with a portfolio weight (for funding accrual).

    Represents a position sized by portfolio weight rather than absolute size.
    Used by the funding cost model to compute notional value.
    """

    symbol: str
    side: Side
    weight: float          # portfolio weight (e.g., 0.1 for 10%)
    entry_price: float     # reference price for notional calculation
    timeframe: str | None = None

    def __post_init__(self) -> None:
        _symbol(self.symbol)
        if not isinstance(self.side, Side):
            raise ValueError("side must be a Side")
        _finite(self.weight, "weight", positive=True)
        _finite(self.entry_price, "entry_price", positive=True)
        if self.timeframe is not None:
            _non_empty(self.timeframe, "timeframe")

    def to_dict(self) -> dict[str, Scalar]:
        return {
            "symbol": self.symbol,
            "side": self.side.value,
            "weight": self.weight,
            "entry_price": self.entry_price,
            "timeframe": self.timeframe,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> WeightedPosition:
        return cls(
            symbol=_string_value(data, "symbol"),
            side=Side(_string_value(data, "side")),
            weight=data["weight"],  # type: ignore[arg-type]
            entry_price=data["entry_price"],  # type: ignore[arg-type]
            timeframe=data.get("timeframe"),  # type: ignore[arg-type]
        )

    @classmethod
    def from_position_intent(cls, intent: PositionIntent) -> WeightedPosition:
        """Create WeightedPosition from PositionIntent."""
        return cls(
            symbol=intent.symbol,
            side=intent.side,
            weight=intent.target_weight,
            entry_price=intent.reference_price or 0.0,
            timeframe=intent.timeframe,
        )


@dataclass(frozen=True, slots=True)
class RegimeSnapshot:
    """Market regime classification snapshot at a point in time.

    Produced by RegimeClassifier and consumed by PortfolioFusionEngine
    for regime-aware exposure scaling.
    """

    as_of_ms: int
    regime: str
    trend_strength: float
    vol_percentile: float
    reference_universe: tuple[str, ...]

    def __post_init__(self) -> None:
        _timestamp(self.as_of_ms)
        if not isinstance(self.regime, str) or not self.regime:
            raise ValueError("regime must be a non-empty string")
        _finite(self.trend_strength, "trend_strength")
        _finite(self.vol_percentile, "vol_percentile", non_negative=True)
        if self.vol_percentile > 1.0:
            raise ValueError("vol_percentile must be <= 1.0")
        if not isinstance(self.reference_universe, tuple):
            object.__setattr__(self, "reference_universe", tuple(self.reference_universe))

    def to_dict(self) -> dict[str, object]:
        return {
            "as_of_ms": self.as_of_ms,
            "regime": self.regime,
            "trend_strength": self.trend_strength,
            "vol_percentile": self.vol_percentile,
            "reference_universe": list(self.reference_universe),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> RegimeSnapshot:
        return cls(
            as_of_ms=data["as_of_ms"],  # type: ignore[arg-type]
            regime=_string_value(data, "regime"),
            trend_strength=data["trend_strength"],  # type: ignore[arg-type]
            vol_percentile=data["vol_percentile"],  # type: ignore[arg-type]
            reference_universe=tuple(data["reference_universe"]),  # type: ignore[arg-type]
        )


__all__ = [
    "UniverseSnapshot",
    "CrossSectionalFeatureSnapshot",
    "PositionIntent",
    "PortfolioIntent",
    "PortfolioState",
    "OrderRequest",
    "Fill",
    "WeightedPosition",
    "RegimeSnapshot",
]
