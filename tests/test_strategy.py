"""Strategy scaffold tests."""
from __future__ import annotations

import pytest

from crypto_bot.core.enums import Mode, Side, Signal, StrategyType
from crypto_bot.core.types import FeatureSet
from crypto_bot.portfolio import (
    CrossSectionalFeatureSnapshot,
    PortfolioIntent,
    PortfolioState,
    UniverseSnapshot,
)
from crypto_bot.strategy import (
    CandidateStrategy,
    PortfolioStrategy,
    SignalEngine,
    SingleTfEngine,
    StrategyContext,
)
from crypto_bot.strategy.registry import StrategyRegistry


def _ctx(min_score: float = 65.0, min_confidence: float = 0.6) -> StrategyContext:
    return StrategyContext(
        scoring_weights={
            "trend": 0.25,
            "momentum": 0.15,
            "volume": 0.15,
            "spread": 0.05,
            "risk": 0.20,
            "liquidity": 0.10,
        },
        min_score=min_score,
        min_confidence=min_confidence,
        adx_min=20.0,
        rsi_long=(50.0, 70.0),
        rsi_short=(30.0, 50.0),
        atr_min_pct=0.5,
        atr_max_pct=8.0,
        take_profit_risk_multiple=2.0,
        max_stop_distance_pct=3.0,
    )


def _feature(tf: str, ema_sign: float, rsi: float = 55.0, adx: float = 30.0) -> FeatureSet:
    return FeatureSet(
        symbol="BTC/USDT",
        timeframe=tf,
        trend_score=1.0,
        momentum_score=0.8,
        volatility_score=0.7,
        volume_score=0.9,
        adx=adx,
        rsi=rsi,
        atr_pct=1.0,
        ema_fast=100.0,
        ema_mid=99.0,
        ema_slow=98.0,
        extras={"ema_state": ema_sign, "last_close": 100.0},
    )


def test_signal_engine_requires_full_timeframe_agreement():
    engine = SignalEngine(_ctx(), ["15m", "1h", "4h"])
    result = engine.evaluate(
        "BTC/USDT",
        {
            "15m": _feature("15m", 1.0),
            "1h": _feature("1h", 1.0),
            "4h": _feature("4h", 0.0),
        },
    )
    assert result.signal == Signal.HOLD
    assert result.reason == "partial timeframe agreement"


def test_signal_engine_full_long_confluence():
    engine = SignalEngine(_ctx(), ["15m", "1h", "4h"])
    result = engine.evaluate(
        "BTC/USDT",
        {
            "15m": _feature("15m", 1.0),
            "1h": _feature("1h", 1.0),
            "4h": _feature("4h", 1.0),
        },
    )
    assert result.signal == Signal.BUY
    assert result.side == Side.LONG
    assert result.confidence == 1.0


def test_single_tf_engine_remains_a_candidate_strategy_with_unchanged_signal():
    engine = SingleTfEngine(_ctx(), ["1h"])
    result = engine.evaluate("BTC/USDT", {"1h": _feature("1h", 1.0)})

    assert isinstance(engine, CandidateStrategy)
    assert result.signal == Signal.BUY
    assert result.side == Side.LONG
    assert result.confidence == 1.0
    assert result.by_timeframe == {"1h": Signal.BUY}
    assert result.reason == "1h: LONG bias"


class _PortfolioStub(PortfolioStrategy):
    def __init__(self, context: StrategyContext, timeframes: list[str]) -> None:
        self.context = context
        self.timeframes = timeframes

    def evaluate(
        self,
        features: CrossSectionalFeatureSnapshot,
        state: PortfolioState,
    ) -> PortfolioIntent:
        return PortfolioIntent(as_of_ms=features.as_of_ms, intents=(), universe=features.universe)


def test_registry_tracks_candidate_and_portfolio_strategy_types():
    registry = StrategyRegistry()
    registry.register("candidate", SignalEngine, strategy_type=StrategyType.CANDIDATE)
    registry.register("portfolio", _PortfolioStub, strategy_type=StrategyType.PORTFOLIO)

    assert registry.get_type("candidate") == StrategyType.CANDIDATE
    assert registry.get_type("portfolio") == StrategyType.PORTFOLIO
    assert registry.is_registered("portfolio", strategy_type=StrategyType.PORTFOLIO)
    assert not registry.is_registered("portfolio", strategy_type=StrategyType.CANDIDATE)

    with pytest.raises(ValueError, match="not 'candidate'"):
        registry.create("portfolio", _ctx(), ["1h"], strategy_type=StrategyType.CANDIDATE)


def test_portfolio_strategy_contract_returns_portfolio_intent():
    universe = UniverseSnapshot(as_of_ms=1, symbols=("BTC/USDT",))
    features = CrossSectionalFeatureSnapshot(
        as_of_ms=1,
        universe=universe,
        features_by_symbol={"BTC/USDT": _feature("1h", 1.0)},
    )
    state = PortfolioState(as_of_ms=1, mode=Mode.PAPER, equity=100.0, cash=100.0)
    strategy = _PortfolioStub(_ctx(), ["1h"])

    assert strategy.build_intent(features, state) == PortfolioIntent(
        as_of_ms=1, intents=(), universe=universe
    )


def _trend_feature(symbol: str, trend_score: float) -> FeatureSet:
    return FeatureSet(
        symbol=symbol,
        timeframe="1h",
        trend_score=trend_score,
        momentum_score=0.5,
        volatility_score=0.5,
        volume_score=0.5,
        adx=30.0,
        rsi=55.0,
        atr_pct=1.0,
        ema_fast=1.0,
        ema_mid=1.0,
        ema_slow=1.0,
    )


def test_long_only_trend_strategy_selects_above_threshold_with_equal_weights():
    from crypto_bot.strategy import LongOnlyTrendPortfolioStrategy

    universe = UniverseSnapshot(as_of_ms=1, symbols=("BTC/USDT", "ETH/USDT", "SOL/USDT"))
    features = CrossSectionalFeatureSnapshot(
        as_of_ms=1,
        universe=universe,
        features_by_symbol={
            "BTC/USDT": _trend_feature("BTC/USDT", 0.9),
            "ETH/USDT": _trend_feature("ETH/USDT", 0.4),
            "SOL/USDT": _trend_feature("SOL/USDT", 0.6),
        },
    )
    state = PortfolioState(as_of_ms=1, mode=Mode.PAPER, equity=100.0, cash=100.0)
    strategy = LongOnlyTrendPortfolioStrategy(_ctx(), ["1h"], min_trend_score=0.5)

    intent = strategy.evaluate(features, state)

    assert intent.as_of_ms == 1
    assert intent.universe is universe
    assert intent.strategy_name == "long_only_trend"
    assert [i.symbol for i in intent.intents] == ["BTC/USDT", "SOL/USDT"]
    assert all(i.side == Side.LONG for i in intent.intents)
    assert all(i.target_weight == pytest.approx(0.5) for i in intent.intents)
    assert intent.intents[0].timeframe == "1h"


def test_long_only_trend_strategy_empty_cross_section_returns_all_cash_intent():
    from crypto_bot.strategy import LongOnlyTrendPortfolioStrategy

    universe = UniverseSnapshot(as_of_ms=1, symbols=("BTC/USDT",))
    features = CrossSectionalFeatureSnapshot(
        as_of_ms=1,
        universe=universe,
        features_by_symbol={"BTC/USDT": _trend_feature("BTC/USDT", 0.1)},
    )
    state = PortfolioState(as_of_ms=1, mode=Mode.PAPER, equity=100.0, cash=100.0)
    intent = LongOnlyTrendPortfolioStrategy(_ctx(), ["1h"]).evaluate(features, state)

    assert intent.intents == ()


def test_long_only_trend_is_registered_as_portfolio_strategy():
    from crypto_bot.pipeline.factory import build_strategy_registry

    registry = build_strategy_registry()
    assert registry.get_type("long_only_trend") == StrategyType.PORTFOLIO
    assert registry.is_registered("long_only_trend", strategy_type=StrategyType.PORTFOLIO)
