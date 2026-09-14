"""Tests for immutable, serializable portfolio-domain DTOs."""
from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from crypto_bot.core.enums import Mode, OrderType, Side
from crypto_bot.core.types import FeatureSet, Position
from crypto_bot.portfolio import (
    CrossSectionalFeatureSnapshot,
    Fill,
    OrderRequest,
    PortfolioIntent,
    PortfolioState,
    PositionIntent,
    UniverseSnapshot,
)


def _feature(symbol: str = "BTC/USDT") -> FeatureSet:
    return FeatureSet(
        symbol=symbol,
        timeframe="1h",
        trend_score=0.8,
        momentum_score=0.6,
        volatility_score=0.7,
        volume_score=0.5,
        adx=30.0,
        rsi=55.0,
        atr_pct=1.2,
        ema_fast=101.0,
        ema_mid=100.0,
        ema_slow=99.0,
    )


def _universe() -> UniverseSnapshot:
    return UniverseSnapshot(as_of_ms=1_700_000_000_000, symbols=("BTC/USDT", "ETH/USDT"))


def test_models_round_trip_through_json_serialization():
    universe = _universe()
    features = CrossSectionalFeatureSnapshot(
        as_of_ms=universe.as_of_ms,
        universe=universe,
        features_by_symbol={"BTC/USDT": _feature()},
    )
    intent = PortfolioIntent(
        as_of_ms=universe.as_of_ms,
        intents=(PositionIntent("BTC/USDT", Side.LONG, 0.25, timeframe="1h"),),
        universe=universe,
        strategy_name="confluence",
    )
    state = PortfolioState(
        as_of_ms=universe.as_of_ms,
        mode=Mode.PAPER,
        equity=10_000.0,
        cash=7_500.0,
        positions=(
            Position(
                symbol="BTC/USDT",
                timeframe="1h",
                side=Side.LONG,
                size=0.1,
                opened_at=datetime(2026, 1, 1, tzinfo=UTC),
            ),
        ),
    )
    order = OrderRequest(
        client_order_id="paper-1",
        symbol="BTC/USDT",
        side=Side.LONG,
        order_type=OrderType.LIMIT,
        quantity=0.1,
        requested_at_ms=universe.as_of_ms,
        mode=Mode.PAPER,
        limit_price=50_000.0,
    )
    fill = Fill(
        fill_id="fill-1",
        order_id="paper-1",
        symbol="BTC/USDT",
        side=Side.LONG,
        quantity=0.1,
        price=50_000.0,
        filled_at_ms=universe.as_of_ms,
        mode=Mode.PAPER,
        fee=5.0,
        fee_currency="USDT",
    )

    for model in (universe, features, intent, state, order, fill):
        payload = json.loads(json.dumps(model.to_dict()))
        assert type(model).from_dict(payload) == model


def test_models_are_value_equal_and_immutable_where_possible():
    first = PositionIntent("BTC/USDT", Side.LONG, 0.25, timeframe="1h")
    second = PositionIntent("BTC/USDT", Side.LONG, 0.25, timeframe="1h")
    assert first == second

    universe = _universe()
    with pytest.raises(FrozenInstanceError):
        universe.quote_currency = "USD"  # type: ignore[misc]

    snapshot = CrossSectionalFeatureSnapshot(
        as_of_ms=universe.as_of_ms,
        universe=universe,
        features_by_symbol={"BTC/USDT": _feature()},
    )
    with pytest.raises(TypeError):
        snapshot.features_by_symbol["ETH/USDT"] = _feature("ETH/USDT")  # type: ignore[index]


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        (lambda: UniverseSnapshot(as_of_ms=-1, symbols=("BTC/USDT",)), "timestamp"),
        (lambda: UniverseSnapshot(as_of_ms=1, symbols=("BTC-USDT",)), "BASE/QUOTE"),
        (lambda: UniverseSnapshot(as_of_ms=1, symbols=("BTC/USDT", "BTC/USDT")), "unique"),
        (lambda: PositionIntent("BTC/USDT", Side.LONG, 0.0), "target_weight"),
        (lambda: OrderRequest("id", "BTC/USDT", Side.LONG, OrderType.LIMIT, 1.0, 1, Mode.PAPER), "limit_price"),
        (lambda: Fill("fill", "order", "BTC/USDT", Side.LONG, 1.0, 1.0, 1, Mode.PAPER, fee=-0.1), "fee"),
    ],
)
def test_models_reject_invalid_values(factory, message):
    with pytest.raises(ValueError, match=message):
        factory()


def test_cross_section_and_portfolio_validate_membership_and_duplicates():
    universe = _universe()
    with pytest.raises(ValueError, match="outside the universe"):
        CrossSectionalFeatureSnapshot(
            as_of_ms=universe.as_of_ms,
            universe=universe,
            features_by_symbol={"SOL/USDT": _feature("SOL/USDT")},
        )

    duplicate = PositionIntent("BTC/USDT", Side.LONG, 0.25, timeframe="1h")
    with pytest.raises(ValueError, match="unique"):
        PortfolioIntent(as_of_ms=universe.as_of_ms, intents=(duplicate, duplicate))


def test_deserialization_rejects_invalid_collection_items():
    universe = _universe()
    with pytest.raises(ValueError, match="feature mappings"):
        CrossSectionalFeatureSnapshot.from_dict(
            {
                "as_of_ms": universe.as_of_ms,
                "universe": universe.to_dict(),
                "features_by_symbol": {"BTC/USDT": "not-a-feature"},
            }
        )
    with pytest.raises(ValueError, match="contain mappings"):
        PortfolioIntent.from_dict({"as_of_ms": universe.as_of_ms, "intents": ["not-an-intent"]})
