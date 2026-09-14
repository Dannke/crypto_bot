"""Tests for the feature-only portfolio decision pipeline."""
from __future__ import annotations

import pytest

from crypto_bot.core.enums import Mode, Side
from crypto_bot.core.types import Candle
from crypto_bot.features.builder import FeatureBuilder, FeatureBuilderParams
from crypto_bot.features.context import SymbolMarketContext
from crypto_bot.pipeline.portfolio_decision_pipeline import PortfolioDecisionPipeline
from crypto_bot.portfolio import (
    CrossSectionalFeatureSnapshot,
    PortfolioIntent,
    PortfolioState,
    PositionIntent,
    UniverseSnapshot,
)
from crypto_bot.strategy import PortfolioStrategy


def _candles() -> list[Candle]:
    result = []
    price = 100.0
    for index in range(50):  # Need more bars for ADX calculation (period=14 needs ~30+ bars)
        open_price = price
        close = price * 1.01
        result.append(
            Candle(
                timestamp=1_700_000_000_000 + index * 3_600_000,
                open=open_price,
                high=close * 1.002,
                low=open_price * 0.998,
                close=close,
                volume=1_000.0 + index,
            )
        )
        price = close
    return result


def _builder() -> FeatureBuilder:
    return FeatureBuilder(
        FeatureBuilderParams(
            ema=(3, 5, 8),
            rsi_period=4,
            atr_period=3,
            bb=(4, 2.0),
            volma_period=3,
            adx_min=5.0,
            atr_lo_pct=0.01,
            atr_hi_pct=20.0,
            vol_spike_ratio=1.0,
            min_quote_volume=100.0,
        )
    )


class _RecordingPortfolioStrategy(PortfolioStrategy):
    received_features: CrossSectionalFeatureSnapshot | None = None
    received_state: PortfolioState | None = None

    def evaluate(
        self,
        features: CrossSectionalFeatureSnapshot,
        state: PortfolioState,
    ) -> PortfolioIntent:
        self.received_features = features
        self.received_state = state
        return PortfolioIntent(
            as_of_ms=features.as_of_ms,
            universe=features.universe,
            intents=(PositionIntent("BTC/USDT", Side.LONG, 0.25, timeframe="1h"),),
        )


def test_portfolio_pipeline_flows_universe_market_features_to_strategy_intent():
    as_of_ms = 1_700_000_180_000_000
    universe = UniverseSnapshot(
        as_of_ms=as_of_ms,
        symbols=("BTC/USDT", "ETH/USDT"),
    )
    state = PortfolioState(as_of_ms=as_of_ms, mode=Mode.PAPER, equity=10_000.0, cash=10_000.0)
    strategy = _RecordingPortfolioStrategy()
    from crypto_bot.config.schemas import RegimeConfig
    pipeline = PortfolioDecisionPipeline(
        _builder(),
        regime_config=RegimeConfig(
            enabled=True,
            reference="universe_basket",
            trend_period=5,
            trend_threshold=0.1,
            vol_lookback_bars=10,
            vol_percentile_high=0.75,
        ),
    )

    intent = pipeline.process(
        universe,
        {
            "BTC/USDT": {"1h": _candles()},
            "ETH/USDT": {"1h": _candles()},
            "SOL/USDT": {"1h": _candles()},
        },
        {
            "BTC/USDT": SymbolMarketContext(quote_volume_24h=150.0, spread_pct=0.1),
            "ETH/USDT": SymbolMarketContext(quote_volume_24h=300.0, spread_pct=0.2),
            "SOL/USDT": SymbolMarketContext(quote_volume_24h=999.0, spread_pct=9.0),
        },
        state,
        strategy,
        trigger_tf="1h",
    )

    assert intent == PortfolioIntent(
        as_of_ms=as_of_ms,
        universe=universe,
        intents=(PositionIntent("BTC/USDT", Side.LONG, 0.25, timeframe="1h"),),
    )
    assert strategy.received_state is state
    assert strategy.received_features is not None
    assert set(strategy.received_features.features_by_symbol) == {"BTC/USDT", "ETH/USDT"}
    assert strategy.received_features.features_by_symbol["BTC/USDT"].spread_pct == 0.1
    assert strategy.received_features.features_by_symbol["BTC/USDT"].liquidity_score == 0.5


def test_portfolio_pipeline_rejects_non_portfolio_strategy_and_empty_features():
    universe = UniverseSnapshot(as_of_ms=1, symbols=("BTC/USDT",))
    state = PortfolioState(as_of_ms=1, mode=Mode.PAPER, equity=100.0, cash=100.0)
    pipeline = PortfolioDecisionPipeline(_builder())

    with pytest.raises(ValueError, match="PortfolioStrategy"):
        pipeline.process(universe, {}, {}, state, object())  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="no portfolio features"):
        pipeline.process(universe, {}, {}, state, _RecordingPortfolioStrategy())


def test_process_market_runs_a_market_based_strategy_end_to_end():
    from crypto_bot.portfolio import get_market_snapshot, get_universe_snapshot
    from crypto_bot.simulation.historical_source import HistoricalCandleSource
    from crypto_bot.strategy import CrossSectionalMomentumStrategy
    from crypto_bot.strategy.base import StrategyContext as _SC

    rets = {"BTC/USDT": 0.05, "ETH/USDT": 0.02, "SOL/USDT": -0.03}
    source = HistoricalCandleSource()
    for symbol, ret in rets.items():
        candles = []
        price = 100.0
        for i in range(50):  # Need more bars for ADX calculation
            open_price = price
            close = price * (1.0 + 0.01)  # 1% uptrend
            high = close * 1.002
            low = open_price * 0.998
            candles.append(Candle(
                timestamp=1_700_000_000_000 + i * 3_600_000,
                open=open_price,
                high=high,
                low=low,
                close=close,
                volume=1000.0 + i * 10,
            ))
            price = close
        source.load_all(symbol, "1h", candles)
    anchor = 1_700_000_000_000 + 20 * 3_600_000
    universe = get_universe_snapshot(source, list(rets), "1h", anchor)
    snapshot = get_market_snapshot(source, universe, "1h")

    state = PortfolioState(as_of_ms=anchor, mode=Mode.PAPER, equity=10_000.0, cash=10_000.0)
    ctx = _SC(
        scoring_weights={},
        min_score=0.0,
        min_confidence=0.0,
        adx_min=0.0,
        rsi_long=(0.0, 100.0),
        rsi_short=(0.0, 100.0),
        atr_min_pct=0.0,
        atr_max_pct=100.0,
        take_profit_risk_multiple=2.0,
    )
    strategy = CrossSectionalMomentumStrategy(ctx, ["1h"], lookback_bars=5, top_fraction=0.5)
    from crypto_bot.config.schemas import RegimeConfig
    pipeline = PortfolioDecisionPipeline(
        _builder(),
        regime_config=RegimeConfig(
            enabled=True,
            reference="universe_basket",
            trend_period=5,
            trend_threshold=0.1,
            vol_lookback_bars=10,
            vol_percentile_high=0.75,
        ),
    )

    intent = pipeline.process_market(snapshot, state, strategy)

    assert intent.as_of_ms == anchor
    assert intent.strategy_name == "cross_sectional_momentum_v0"
    assert [(i.symbol, i.side, i.target_weight) for i in intent.intents] == [
        ("BTC/USDT", Side.LONG, 0.5),
        ("ETH/USDT", Side.LONG, 0.5),
    ]


def test_process_market_rejects_bad_inputs():
    from crypto_bot.portfolio import MarketSnapshot

    pipeline = PortfolioDecisionPipeline(_builder())
    state = PortfolioState(as_of_ms=1, mode=Mode.PAPER, equity=100.0, cash=100.0)
    first_candle = _candles()[0]
    empty_snapshot = MarketSnapshot(
        as_of_ms=first_candle.timestamp + 3_600_000,
        timeframe="1h",
        candles_by_symbol={"BTC/USDT": (first_candle,)},
    )

    with pytest.raises(ValueError, match="snapshot"):
        pipeline.process_market(object(), state, object())  # type: ignore[arg-type]

    with pytest.raises(NotImplementedError, match="evaluate_market"):
        pipeline.process_market(empty_snapshot, state, _RecordingPortfolioStrategy())