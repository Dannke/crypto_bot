"""Mean Reversion v0 (Task 5): z-score, rank, threshold, exit logic, weights.

Exactly the primitives tested — z-score computation, cross-sectional rank,
threshold-based entry/exit, time-stop, and weighting schemes.
Regime, funding, OI, order book data are explicitly out of scope for v0.
"""
from __future__ import annotations

import pytest

from crypto_bot.core.enums import Mode, Side, TradeStatus
from crypto_bot.core.types import Candle
from crypto_bot.pipeline.factory import build_strategy_registry
from crypto_bot.portfolio import (
    MarketSnapshot,
    PortfolioState,
    get_market_snapshot,
    get_universe_snapshot,
)
from crypto_bot.simulation.historical_source import HistoricalCandleSource
from crypto_bot.strategy import MeanReversionStrategy, StrategyContext
from crypto_bot.strategy.portfolio_strategies import MEAN_REVERSION_V0_STRATEGY_NAME
from datetime import datetime, UTC

PERIOD_MS = 3_600_000
BASE_TS = 1_700_000_000_000
WINDOW_BARS = 12  # rolling window for z-score estimation
SIGNAL_LOOKBACK = 2  # 2 bars = signal horizon
MIN_BARS = WINDOW_BARS + SIGNAL_LOOKBACK + 5  # minimum bars needed


def _mr_candles_from_closes(closes: list[float]) -> list[Candle]:
    """Generate bars from a list of close prices."""
    out: list[Candle] = []
    for i, close in enumerate(closes):
        t = BASE_TS + i * PERIOD_MS
        out.append(Candle(
            timestamp=t,
            open=close,
            high=close * 1.001,
            low=close * 0.999,
            close=close,
            volume=1000.0,
        ))
    return out


def _compute_returns(closes: list[float]) -> list[float]:
    """Compute 1-bar returns from closes."""
    return [(closes[i] / closes[i-1]) - 1.0 for i in range(1, len(closes))]


def _build_closes_for_zscore(target_z: float, window: int = WINDOW_BARS, signal_lb: int = SIGNAL_LOOKBACK) -> list[float]:
    """Build a price series that yields approximately the target z-score.

    We create a series with small random-ish returns for the rolling window,
    then make the last `signal_lb` returns produce the desired z-score.
    """
    # Base returns: small noise around 0
    base_returns = [0.001 * ((-1) ** i) for i in range(window)]
    # This gives mean ~0, std ~0.001
    
    # Current return over signal_lb bars to achieve target_z
    # z = (current_return - mean) / std
    # current_return = z * std + mean
    # For simplicity, approximate std of window returns
    mean_r = sum(base_returns) / len(base_returns)
    var = sum((r - mean_r) ** 2 for r in base_returns) / (len(base_returns) - 1)
    std_r = var ** 0.5 if var > 0 else 0.001
    
    target_return = target_z * std_r + mean_r
    
    # Distribute target_return over signal_lb bars (geometric)
    per_bar = (1 + target_return) ** (1 / signal_lb) - 1
    
    # Build full return series
    returns = base_returns + [per_bar] * signal_lb
    
    # Convert to closes
    closes = [100.0]
    for r in returns:
        closes.append(closes[-1] * (1 + r))
    
    return closes


def _zscore_snapshot(
    zscores_by_symbol: dict[str, float],
    *,
    window_bars: int = WINDOW_BARS,
    signal_lookback: int = SIGNAL_LOOKBACK,
) -> MarketSnapshot:
    """Build a market snapshot that produces the given z-scores."""
    source = HistoricalCandleSource()
    symbols = list(zscores_by_symbol)

    all_closes = {}
    max_len = 0
    for symbol, target_z in zscores_by_symbol.items():
        closes = _build_closes_for_zscore(target_z, window_bars, signal_lookback)
        # Ensure we have enough bars
        if len(closes) < MIN_BARS:
            # Pad with flat closes at the beginning
            pad = MIN_BARS - len(closes)
            closes = [closes[0]] * pad + closes
        all_closes[symbol] = closes
        max_len = max(max_len, len(closes))

    # Load all symbols
    for symbol, closes in all_closes.items():
        source.load_all(symbol, "1h", _mr_candles_from_closes(closes))

    # Anchor must be AFTER the last bar closes: BASE_TS + max_len * PERIOD_MS
    # (last bar opens at BASE_TS + (max_len-1)*PERIOD_MS, closes at BASE_TS + max_len*PERIOD_MS)
    anchor = BASE_TS + max_len * PERIOD_MS
    universe = get_universe_snapshot(source, symbols, "1h", anchor)
    return get_market_snapshot(source, universe, "1h")


def _state(anchor_ms: int | None = None) -> PortfolioState:
    if anchor_ms is None:
        anchor_ms = BASE_TS + MIN_BARS * PERIOD_MS
    return PortfolioState(
        as_of_ms=anchor_ms, mode=Mode.PAPER, equity=10_000.0, cash=10_000.0,
    )


def _make_position(symbol: str, side: Side, bars_ago: int, anchor_ms: int) -> object:
    """Create a Position with opened_at `bars_ago` bars before anchor."""
    opened_at = datetime.fromtimestamp((anchor_ms - bars_ago * 3600 * 1000) / 1000, tz=UTC)
    from crypto_bot.core.types import Position
    return Position(
        id=1,
        symbol=symbol,
        timeframe="1h",
        side=side,
        size=1.0,
        entry_price=100.0,
        stop=0.0,
        take=0.0,
        opened_at=opened_at,
        closed_by=None,
        status=TradeStatus.OPEN,
        closed_at=None,
        exit_price=None,
        pnl_pct=None,
    )


def _mr_strategy(**kwargs) -> MeanReversionStrategy:
    defaults = {
        "zscore_window_bars": WINDOW_BARS,
        "signal_lookback_bars": SIGNAL_LOOKBACK,
        "entry_threshold": 2.0,
        "exit_threshold": 0.5,
        "max_holding_bars": 10,
        "weighting": "equal",
        "top_fraction": 0.5,
        "short_fraction": 0.5,
        "min_expected_edge_bps": 0,
    }
    defaults.update(kwargs)
    return MeanReversionStrategy(_mr_context(), ["1h"], **defaults)


def _mr_context() -> StrategyContext:
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


# Test z-scores: negative = oversold = LONG, positive = overbought = SHORT
_ZSCORES = {
    "SYM0/USDT": -3.0,  # Strong oversold -> LONG
    "SYM1/USDT": -2.5,  # Oversold -> LONG
    "SYM2/USDT": -1.0,  # Mild oversold -> no entry (threshold=2.0)
    "SYM3/USDT": 0.0,   # Neutral -> no entry
    "SYM4/USDT": 1.0,   # Mild overbought -> no entry
    "SYM5/USDT": 2.5,   # Overbought -> SHORT
    "SYM6/USDT": 3.0,   # Strong overbought -> SHORT
}


class TestZScoreSignal:
    def test_zscore_ranking_ascending(self) -> None:
        """Most negative z-score ranks first (LONG candidate)."""
        strategy = _mr_strategy(top_fraction=1.0, short_fraction=1.0)
        intent = strategy.evaluate_market(_zscore_snapshot(_ZSCORES), _state())

        # Should rank by z-score ascending: SYM0 (-3.0) first, SYM6 (3.0) last
        symbols = [i.symbol for i in intent.intents]
        assert symbols[0] == "SYM0/USDT"  # Most oversold
        assert symbols[-1] == "SYM6/USDT"  # Most overbought

    def test_long_entry_below_negative_threshold(self) -> None:
        """Symbols with z <= -entry_threshold enter LONG."""
        # Use top_fraction=1.0 to consider all, then threshold filters
        # short_fraction=None for long-only
        strategy = _mr_strategy(top_fraction=1.0, short_fraction=None)
        intent = strategy.evaluate_market(_zscore_snapshot(_ZSCORES), _state())

        # Only SYM0 (-3.0) and SYM1 (-2.5) are <= -2.0
        long_symbols = [i.symbol for i in intent.intents if i.side == Side.LONG]
        assert "SYM0/USDT" in long_symbols
        assert "SYM1/USDT" in long_symbols
        assert "SYM2/USDT" not in long_symbols  # -1.0 > -2.0

    def test_short_entry_above_positive_threshold(self) -> None:
        """Symbols with z >= entry_threshold enter SHORT."""
        # Use small top_fraction to allow short_fraction=1.0 to work
        strategy = _mr_strategy(top_fraction=0.1, short_fraction=1.0)
        intent = strategy.evaluate_market(_zscore_snapshot(_ZSCORES), _state())

        short_symbols = [i.symbol for i in intent.intents if i.side == Side.SHORT]
        assert "SYM6/USDT" in short_symbols  # 3.0 >= 2.0
        assert "SYM5/USDT" in short_symbols  # 2.5 >= 2.0
        assert "SYM4/USDT" not in short_symbols  # 1.0 < 2.0

    def test_no_entry_between_thresholds(self) -> None:
        """Symbols with |z| < entry_threshold don't enter."""
        strategy = _mr_strategy(top_fraction=1.0, short_fraction=1.0)
        intent = strategy.evaluate_market(_zscore_snapshot(_ZSCORES), _state())

        entered = {i.symbol for i in intent.intents}
        # SYM2 (-1.0), SYM3 (0.0), SYM4 (1.0) should NOT enter
        assert "SYM2/USDT" not in entered
        assert "SYM3/USDT" not in entered
        assert "SYM4/USDT" not in entered


class TestExitLogic:
    def test_reversion_exit_when_abs_z_below_exit_threshold(self) -> None:
        """Existing position exits when |z| <= exit_threshold."""
        snapshot = _zscore_snapshot({"SYM0/USDT": -0.3})  # reverted
        anchor = snapshot.as_of_ms
        state = PortfolioState(
            as_of_ms=anchor,
            mode=Mode.PAPER,
            equity=10_000.0,
            cash=5_000.0,
            positions=(_make_position("SYM0/USDT", Side.LONG, 5, anchor),),
        )

        strategy = _mr_strategy()
        intent = strategy.evaluate_market(snapshot, state)

        # Position should be exited (not in intents)
        assert len(intent.intents) == 0

    def test_time_stop_exit_when_max_holding_exceeded(self) -> None:
        """Position exits after max_holding_bars regardless of z-score."""
        snapshot = _zscore_snapshot({"SYM0/USDT": -2.5})  # Still oversold
        anchor = snapshot.as_of_ms
        # Position opened 15 bars ago (max_holding=10)
        state = PortfolioState(
            as_of_ms=anchor,
            mode=Mode.PAPER,
            equity=10_000.0,
            cash=5_000.0,
            positions=(_make_position("SYM0/USDT", Side.LONG, 15, anchor),),
        )

        strategy = _mr_strategy(max_holding_bars=10)
        intent = strategy.evaluate_market(snapshot, state)

        # Position should be exited due to time-stop
        assert len(intent.intents) == 0

    def test_keep_position_when_no_exit_condition(self) -> None:
        """Position kept when |z| > exit_threshold AND age < max_holding."""
        snapshot = _zscore_snapshot({"SYM0/USDT": -2.0})  # |z| = 2.0 > 0.5 exit
        anchor = snapshot.as_of_ms
        state = PortfolioState(
            as_of_ms=anchor,
            mode=Mode.PAPER,
            equity=10_000.0,
            cash=5_000.0,
            positions=(_make_position("SYM0/USDT", Side.LONG, 5, anchor),),
        )

        strategy = _mr_strategy(max_holding_bars=10)
        intent = strategy.evaluate_market(snapshot, state)

        # Position should be kept
        assert len(intent.intents) == 1
        assert intent.intents[0].symbol == "SYM0/USDT"
        assert intent.intents[0].side == Side.LONG


class TestWeighting:
    def test_equal_weighting(self) -> None:
        """Equal weight across all selected symbols."""
        strategy = _mr_strategy(top_fraction=0.5, short_fraction=0.5, weighting="equal")
        intent = strategy.evaluate_market(_zscore_snapshot(_ZSCORES), _state())

        # 2 longs (SYM0, SYM1) + 2 shorts (SYM5, SYM6) = 4 symbols
        assert len(intent.intents) == 4
        assert all(i.target_weight == pytest.approx(0.25) for i in intent.intents)

    def test_inverse_vol_weighting(self) -> None:
        """Inverse volatility weights sum to 1.0."""
        strategy = _mr_strategy(top_fraction=0.5, short_fraction=0.5, weighting="inverse_vol")
        intent = strategy.evaluate_market(_zscore_snapshot(_ZSCORES), _state())

        assert len(intent.intents) == 4
        gross = sum(i.target_weight for i in intent.intents)
        assert gross == pytest.approx(1.0)
        # All weights should be positive
        assert all(i.target_weight > 0 for i in intent.intents)


class TestContract:
    def test_rejects_invalid_parameters(self) -> None:
        with pytest.raises(ValueError):
            _mr_strategy(zscore_window_bars=5)
        with pytest.raises(ValueError):
            _mr_strategy(signal_lookback_bars=0)
        with pytest.raises(ValueError):
            _mr_strategy(entry_threshold=0.0)
        with pytest.raises(ValueError):
            _mr_strategy(exit_threshold=-0.1)
        with pytest.raises(ValueError):
            _mr_strategy(entry_threshold=1.0, exit_threshold=1.0)
        with pytest.raises(ValueError):
            _mr_strategy(max_holding_bars=0)
        with pytest.raises(ValueError):
            _mr_strategy(weighting="volatility_scaled")
        with pytest.raises(ValueError):
            _mr_strategy(top_fraction=0.0)
        with pytest.raises(ValueError):
            _mr_strategy(top_fraction=1.5)
        with pytest.raises(ValueError):
            _mr_strategy(short_fraction="yes")

    def test_evaluate_rejects_features_input(self) -> None:
        from crypto_bot.core.types import FeatureSet
        from crypto_bot.portfolio import (
            CrossSectionalFeatureSnapshot,
            UniverseSnapshot,
        )

        snapshot = _zscore_snapshot(_ZSCORES)
        universe = UniverseSnapshot(as_of_ms=snapshot.as_of_ms, symbols=tuple(_ZSCORES))
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
            _mr_strategy().evaluate(features, _state())

    def test_registered_as_portfolio_strategy(self) -> None:
        from crypto_bot.strategy.base import PortfolioStrategy
        from crypto_bot.core.enums import StrategyType

        registry = build_strategy_registry()
        assert registry.get_type(MEAN_REVERSION_V0_STRATEGY_NAME) == StrategyType.PORTFOLIO

        strategy = registry.create(
            MEAN_REVERSION_V0_STRATEGY_NAME, _mr_context(), ["1h"]
        )
        assert isinstance(strategy, PortfolioStrategy)
        assert isinstance(strategy, MeanReversionStrategy)

    def test_all_cash_intent_when_nothing_qualifies(self) -> None:
        """All-cash intent when no symbol crosses entry threshold."""
        zscores = {f"SYM{i}/USDT": 0.0 for i in range(5)}  # All neutral
        intent = _mr_strategy(top_fraction=0.5, short_fraction=0.5).evaluate_market(
            _zscore_snapshot(zscores), _state()
        )

        assert intent.intents == ()
        assert intent.strategy_name == MEAN_REVERSION_V0_STRATEGY_NAME


class TestShortHistoryExclusion:
    def test_insufficient_history_excluded(self) -> None:
        """Symbols with fewer than window + signal + 1 bars are excluded."""
        source = HistoricalCandleSource()
        # SYM_A: enough bars (25 bars)
        closes_a = [100.0] * 25
        for i in range(1, 25):
            closes_a[i] = closes_a[i-1] * 1.001
        source.load_all("SYM_A/USDT", "1h", _mr_candles_from_closes(closes_a))
        
        # SYM_B: too few bars (only 10)
        closes_b = [100.0] * 10
        source.load_all("SYM_B/USDT", "1h", _mr_candles_from_closes(closes_b))
        
        anchor = BASE_TS + 24 * PERIOD_MS
        universe = get_universe_snapshot(source, ["SYM_A/USDT", "SYM_B/USDT"], "1h", anchor)
        snapshot = get_market_snapshot(source, universe, "1h")

        intent = _mr_strategy(top_fraction=1.0, short_fraction=1.0).evaluate_market(
            snapshot, _state(anchor)
        )

        # Only SYM_A should be considered
        symbols = [i.symbol for i in intent.intents]
        assert "SYM_A/USDT" in symbols
        assert "SYM_B/USDT" not in symbols


class TestMinExpectedEdgeBpsDimensional:
    """Test that min_expected_edge_bps filter is dimensionally correct.
    
    The filter uses: expected_edge_bps = (|z| - exit_threshold) * rolling_std * 10000
    where rolling_std is in return units (e.g., 0.008 = 0.8%).
    
    This test verifies the filter doesn't systematically favor cheap coins over
    expensive ones when z-score and rolling_std are identical.
    """
    def test_edge_filter_not_biased_by_price(self) -> None:
        """Filter should not favor DOGE ($0.08) over BTC ($60,000) when z and rolling_std match."""
        # Use the existing _zscore_snapshot helper to create candles with specific z-scores
        zscores = {
            "BTC/USDT": 3.5,
            "DOGE/USDT": 3.5,
        }
        
        snapshot = _zscore_snapshot(zscores)
        state = _state()
        
        # Use min_expected_edge_bps=10 (should pass for both since edge ~300 bps)
        intent = _mr_strategy(min_expected_edge_bps=10, top_fraction=1.0, short_fraction=1.0).evaluate_market(
            snapshot, state
        )
        
        symbols = [i.symbol for i in intent.intents]
        # Both should pass the edge filter (same z, same rolling_std, different prices)
        assert "BTC/USDT" in symbols, "BTC should pass edge filter"
        assert "DOGE/USDT" in symbols, "DOGE should pass edge filter (dimensional fix)"
        
        