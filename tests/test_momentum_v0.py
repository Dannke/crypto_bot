"""Cross-sectional momentum v0 (task 9): returns, rank, percentile, weights.

Exactly four primitives are tested — returns, cross-sectional rank,
top/bottom percentile selection, and equal weights.  ML, regime, OI,
funding and order book data are explicitly out of scope for v0.
"""
from __future__ import annotations

import pytest

from crypto_bot.core.enums import Mode, Side, StrategyType
from crypto_bot.core.types import Candle
from crypto_bot.pipeline.factory import build_strategy_registry
from crypto_bot.portfolio import (
    MarketSnapshot,
    PortfolioState,
    get_market_snapshot,
    get_universe_snapshot,
)
from crypto_bot.simulation.historical_source import HistoricalCandleSource
from crypto_bot.strategy import CrossSectionalMomentumStrategy, StrategyContext
from crypto_bot.strategy.portfolio_strategies import MOMENTUM_V0_STRATEGY_NAME

PERIOD_MS = 3_600_000
BASE_TS = 1_700_000_000_000
LOOKBACK = 5


def _mom_candles(ret: float, n_bars: int, *, ts: int = BASE_TS) -> list[Candle]:
    """n_bars bars; the first n_bars-1 close flat, the last jumps by ``ret``."""
    out: list[Candle] = []
    for i in range(n_bars):
        t = ts + i * PERIOD_MS
        close = 100.0 * (1.0 + ret) if i == n_bars - 1 else 100.0
        out.append(Candle(
            timestamp=t, open=100.0, high=max(100.0, close) * 1.001,
            low=min(100.0, close) * 0.999, close=close, volume=1000.0,
        ))
    return out


def _closes_snapshot(closes_by_symbol: dict[str, list[float]]) -> MarketSnapshot:
    """Snapshot whose bars follow the given close series (for multi-lookback)."""
    source = HistoricalCandleSource()
    symbols = list(closes_by_symbol)
    n_bars = len(next(iter(closes_by_symbol.values())))
    for symbol, closes in closes_by_symbol.items():
        candles = [
            Candle(
                timestamp=BASE_TS + i * PERIOD_MS,
                open=100.0,
                high=max(100.0, c) * 1.001,
                low=min(100.0, c) * 0.999,
                close=c,
                volume=1000.0,
            )
            for i, c in enumerate(closes)
        ]
        source.load_all(symbol, "1h", candles)
    anchor = BASE_TS + n_bars * PERIOD_MS
    universe = get_universe_snapshot(source, symbols, "1h", anchor)
    return get_market_snapshot(source, universe, "1h")


def _snapshot(rets: dict[str, float], *, n_bars: int = LOOKBACK + 2) -> MarketSnapshot:
    """Build a closed-bars-only market snapshot with the same bar grid."""
    source = HistoricalCandleSource()
    symbols = list(rets)
    for symbol, ret in rets.items():
        source.load_all(symbol, "1h", _mom_candles(ret, n_bars))
    anchor = BASE_TS + n_bars * PERIOD_MS
    universe = get_universe_snapshot(source, symbols, "1h", anchor)
    return get_market_snapshot(source, universe, "1h")


def _state(anchor_ms: int = BASE_TS + (LOOKBACK + 2) * PERIOD_MS) -> PortfolioState:
    return PortfolioState(
        as_of_ms=anchor_ms, mode=Mode.PAPER, equity=10_000.0, cash=10_000.0,
    )


def _momentum(**kwargs) -> CrossSectionalMomentumStrategy:
    params: dict[str, object] = {}
    if "lookbacks_bars" not in kwargs:
        params["lookback_bars"] = LOOKBACK
    params.update(kwargs)
    return CrossSectionalMomentumStrategy(_momentum_context(), ["1h"], **params)


_RETS = {f"SYM{i}/USDT": i * 0.01 for i in range(10)}  # return = 0, 0.01, ..., 0.09


class TestReturns:
    def test_returns_are_cumulative_close_over_lookback(self) -> None:
        # Symbol with ret=0.09 must rank first, ret=0.0 last.
        strategy = _momentum(top_fraction=1.0)
        intent = strategy.evaluate_market(_snapshot(_RETS), _state())

        assert [i.symbol for i in intent.intents] == [
            "SYM9/USDT", "SYM8/USDT", "SYM7/USDT", "SYM6/USDT", "SYM5/USDT",
            "SYM4/USDT", "SYM3/USDT", "SYM2/USDT", "SYM1/USDT", "SYM0/USDT",
        ]

    def test_short_history_symbols_are_excluded(self) -> None:
        # SYM_SHORT has exactly `lookback` bars -> no return computable.
        snapshot = _snapshot(
            {"SYM9/USDT": 0.09, "SYM_SHORT/USDT": 0.5},
            n_bars=LOOKBACK + 2,
        )
        shorter = snapshot.candles_by_symbol["SYM_SHORT/USDT"][:LOOKBACK]
        snapshot = MarketSnapshot(
            as_of_ms=snapshot.as_of_ms,
            timeframe=snapshot.timeframe,
            candles_by_symbol={
                "SYM9/USDT": snapshot.candles_by_symbol["SYM9/USDT"],
                "SYM_SHORT/USDT": shorter,
            },
        )
        intent = _momentum(top_fraction=1.0).evaluate_market(snapshot, _state())
        assert [i.symbol for i in intent.intents] == ["SYM9/USDT"]


class TestRankAndPercentile:
    def test_top_percentile_longs_only(self) -> None:
        # 10 symbols, top 20% -> exactly the two best (ceiling of 0.2*10).
        strategy = _momentum(top_fraction=0.2, short_fraction=None)
        intent = strategy.evaluate_market(_snapshot(_RETS), _state())

        assert [i.symbol for i in intent.intents] == ["SYM9/USDT", "SYM8/USDT"]
        assert all(i.side == Side.LONG for i in intent.intents)

    def test_bottom_percentile_shorts_when_allowed(self) -> None:
        strategy = _momentum(top_fraction=0.2, short_fraction=0.2)
        intent = strategy.evaluate_market(_snapshot(_RETS), _state())

        symbols = [(i.symbol, i.side) for i in intent.intents]
        assert symbols == [
            ("SYM9/USDT", Side.LONG),
            ("SYM8/USDT", Side.LONG),
            ("SYM1/USDT", Side.SHORT),
            ("SYM0/USDT", Side.SHORT),
        ]

    def test_long_only_never_shorts_the_bottom(self) -> None:
        strategy = _momentum(top_fraction=0.2, short_fraction=None)
        intent = strategy.evaluate_market(_snapshot(_RETS), _state())
        assert all(i.side == Side.LONG for i in intent.intents)
        assert len(intent.intents) == 2

    def test_percentile_rounds_up_for_tiny_universes(self) -> None:
        # 3 symbols, 20% -> ceil(0.6) = 1 selected.
        rets = {"A/USDT": 0.01, "B/USDT": 0.02, "C/USDT": 0.03}
        intent = _momentum(top_fraction=0.2).evaluate_market(
            _snapshot(rets), _state()
        )
        assert [i.symbol for i in intent.intents] == ["C/USDT"]

    def test_nothing_qualifying_emits_all_cash_intent(self) -> None:
        # Every symbol has exactly `lookback` bars: no return is computable,
        # so the strategy must emit the all-cash intent rather than guess.
        source = HistoricalCandleSource()
        for symbol in ("A/USDT", "B/USDT"):
            source.load_all(symbol, "1h", _mom_candles(0.01, LOOKBACK))
        anchor = BASE_TS + LOOKBACK * PERIOD_MS
        universe = get_universe_snapshot(source, ["A/USDT", "B/USDT"], "1h", anchor)
        snapshot = get_market_snapshot(source, universe, "1h")

        intent = _momentum(top_fraction=0.5).evaluate_market(snapshot, _state(anchor))

        assert intent.intents == ()
        assert intent.as_of_ms == anchor
        assert intent.strategy_name == MOMENTUM_V0_STRATEGY_NAME
        assert intent.universe is not None
        assert intent.universe.symbols == ("A/USDT", "B/USDT")


class TestEqualWeights:
    def test_each_selected_symbol_gets_one_over_n(self) -> None:
        intent = _momentum(top_fraction=0.2).evaluate_market(
            _snapshot(_RETS), _state()
        )
        assert len(intent.intents) == 2
        assert all(i.target_weight == pytest.approx(0.5) for i in intent.intents)

    def test_long_short_gross_exposure_is_one(self) -> None:
        intent = _momentum(top_fraction=0.2, short_fraction=0.2).evaluate_market(
            _snapshot(_RETS), _state()
        )
        assert len(intent.intents) == 4
        assert all(i.target_weight == pytest.approx(0.25) for i in intent.intents)
        gross = sum(i.target_weight for i in intent.intents)
        assert gross == pytest.approx(1.0)

    def test_intents_carry_last_close_as_reference_price(self) -> None:
        intent = _momentum(top_fraction=0.2).evaluate_market(
            _snapshot(_RETS), _state()
        )
        for i in intent.intents:
            expected = 100.0 * (1.0 + _RETS[i.symbol])
            assert i.reference_price == pytest.approx(expected)


class TestContract:
    def test_rejects_invalid_parameters(self) -> None:
        with pytest.raises(ValueError):
            _momentum(lookback_bars=0)
        with pytest.raises(ValueError):
            _momentum(lookbacks_bars=(0,))
        with pytest.raises(ValueError):
            _momentum(lookback_bars=2, lookbacks_bars=(2,))
        with pytest.raises(ValueError):
            _momentum(top_fraction=0.0)
        with pytest.raises(ValueError):
            _momentum(top_fraction=1.5)
        with pytest.raises(ValueError):
            _momentum(short_fraction="yes")

    def test_default_construction_uses_single_20_bar_lookback(self) -> None:
        strategy = _momentum(lookback_bars=None)
        assert strategy.lookbacks_bars == (20,)
        strategy = CrossSectionalMomentumStrategy(_momentum_context(), ["1h"])
        assert strategy.lookbacks_bars == (20,)

    def test_evaluate_rejects_features_input(self) -> None:
        from crypto_bot.core.types import FeatureSet
        from crypto_bot.portfolio import (
            CrossSectionalFeatureSnapshot,
            UniverseSnapshot,
        )

        snapshot = _snapshot(_RETS)
        universe = UniverseSnapshot(as_of_ms=snapshot.as_of_ms, symbols=tuple(_RETS))
        feature = FeatureSet(
            symbol="SYM0/USDT",
            timeframe="1h",
            trend_score=0.5,
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
        features = CrossSectionalFeatureSnapshot(
            as_of_ms=universe.as_of_ms,
            universe=universe,
            features_by_symbol={feature.symbol: feature},
        )
        with pytest.raises(NotImplementedError, match="evaluate_market"):
            _momentum().evaluate(features, _state())

    def test_registered_as_portfolio_strategy(self) -> None:
        from crypto_bot.strategy.base import PortfolioStrategy

        registry = build_strategy_registry()
        assert registry.get_type(MOMENTUM_V0_STRATEGY_NAME) == StrategyType.PORTFOLIO

        strategy = registry.create(
            MOMENTUM_V0_STRATEGY_NAME, _momentum_context(), ["1h"]
        )
        assert isinstance(strategy, PortfolioStrategy)
        assert isinstance(strategy, CrossSectionalMomentumStrategy)


def _momentum_context() -> StrategyContext:
    return StrategyContext(
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


class TestMultiLookback:
    """CSM config lookbacks (e.g. 24h/72h/168h) -> composite returns."""

    def test_composite_return_is_the_mean_of_per_lookback_returns(self) -> None:
        # A: 2-bar +10%, 5-bar 0%    -> composite 0.05
        # B: 2-bar +4.76%, 5-bar +10% -> composite ~0.0738 -> B ranks first.
        n = 8
        closes_a = [100.0] * n
        closes_a[5] = 100.0  # 2-bar base (close[-3])
        closes_a[2] = 110.0  # 5-bar base (close[-6])
        closes_a[-1] = 110.0
        closes_b = [100.0] * n
        closes_b[5] = 105.0
        closes_b[2] = 100.0
        closes_b[-1] = 110.0

        snapshot = _closes_snapshot({
            "A/USDT": closes_a,
            "B/USDT": closes_b,
        })
        strategy = _momentum(lookbacks_bars=(2, 5), top_fraction=0.5)
        intent = strategy.evaluate_market(snapshot, _state())

        assert [i.symbol for i in intent.intents] == ["B/USDT"]

    def test_insufficient_bars_for_the_longest_lookback_excludes_symbol(self) -> None:
        # C has enough bars for the 2-bar horizon but not the 5-bar one.
        n = 8
        closes_a = [100.0] * n
        closes_a[-1] = 110.0
        closes_c = [100.0] * 5  # only 4 past bars relative to last
        closes_c[-1] = 110.0

        snapshot = _closes_snapshot({"A/USDT": closes_a, "C/USDT": closes_c})
        strategy = _momentum(lookbacks_bars=(2, 5), top_fraction=1.0)
        intent = strategy.evaluate_market(snapshot, _state())

        assert [i.symbol for i in intent.intents] == ["A/USDT"]

    def test_short_fraction_applies_to_composite_returns(self) -> None:
        n = 8
        closes = {f"S{i}/USDT": [100.0] * n for i in range(4)}
        for i, symbol in enumerate(closes):
            closes[symbol][2] = 100.0
            closes[symbol][5] = 100.0
            closes[symbol][-1] = 100.0 * (1.0 + 0.02 * (i - 1))

        snapshot = _closes_snapshot(closes)
        strategy = _momentum(lookbacks_bars=(2, 5), top_fraction=0.25, short_fraction=0.25)
        intent = strategy.evaluate_market(snapshot, _state())

        symbols = [(i.symbol, i.side) for i in intent.intents]
        assert symbols[0][1] == Side.LONG
        assert symbols[-1][1] == Side.SHORT
        assert len(intent.intents) == 2
        assert all(i.target_weight == pytest.approx(0.5) for i in intent.intents)
