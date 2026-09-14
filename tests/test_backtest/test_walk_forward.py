"""Task 14: walk-forward integration — both decision layers, same semantics.

Candidate (SingleTF) and portfolio (CSM) walk-forward runs must share the
same feature semantics (one candle source loaded once, closed bars only,
pre-window history available for warmup), the same execution semantics
(fees/slippage from the same Config) and the same risk semantics (same
limits in both windows).  The only difference between train and test is the
calendar window itself.
"""
from __future__ import annotations

import pytest

from crypto_bot.config.env import Config
from crypto_bot.core.enums import StrategyType
from crypto_bot.pipeline.factory import PER_TF_STRATEGY_NAME
from crypto_bot.simulation.historical_source import HistoricalCandleSource
from crypto_bot.simulation.pnl import PnLSummary
from crypto_bot.strategy.portfolio_strategies import MOMENTUM_V0_STRATEGY_NAME
from crypto_bot.simulation.walk_forward import (
    WalkForwardResult,
    calendar_split,
    run_walk_forward,
)

from .test_baseline_comparison import (
    BASE_TS,
    N_BARS,
    PERIOD_MS,
    _csm_config,
    _trending_universe,
)

UNIVERSE = _trending_universe()
SYMBOLS = list(UNIVERSE)


def _source() -> HistoricalCandleSource:
    source = HistoricalCandleSource()
    for sym, candles in UNIVERSE.items():
        source.load_all(sym, "1h", candles)
    return source


def _portfolio_config(strategy_name: str = MOMENTUM_V0_STRATEGY_NAME) -> Config:
    """CSM block (from _csm_config) with the requested portfolio strategy."""
    config = _csm_config()
    portfolio = config.settings.portfolio.model_copy(update={"strategy_name": strategy_name})
    settings = config.settings.model_copy(update={"portfolio": portfolio})
    return Config(settings=settings, env=config.env)


def _code_candles() -> list:
    """First symbol's candles (sorted ascending by timestamp)."""
    return UNIVERSE[SYMBOLS[0]]


class TestCalendarSplit:
    def test_windows_contiguous_and_full_range(self) -> None:
        candles = _code_candles()
        train_start, train_end, test_start, test_end = calendar_split(
            candles, PERIOD_MS, 0.7
        )
        assert train_start == BASE_TS
        assert train_end == test_start
        assert test_end == BASE_TS + N_BARS * PERIOD_MS

    def test_split_ratio_applied_to_calendar_time(self) -> None:
        candles = _code_candles()
        for ratio in (0.5, 0.7, 0.9):
            _, train_end, _, _ = calendar_split(candles, PERIOD_MS, ratio)
            train_ms = train_end - BASE_TS
            expected = ratio * N_BARS * PERIOD_MS
            # within one bar of the exact ratio point
            assert abs(train_ms - expected) <= PERIOD_MS


class TestWalkForwardCandidate:
    def test_candidate_layer_runs_and_reports(self) -> None:
        result = run_walk_forward(
            _csm_config(),
            symbols=SYMBOLS,
            timeframe="1h",
            source=_source(),
            split_ratio=0.7,
        )
        assert result.strategy_mode == StrategyType.CANDIDATE
        assert result.strategy_name == PER_TF_STRATEGY_NAME
        assert result.train_n_bars > 0 and result.test_n_bars > 0
        assert result.train.total_trades > 0
        assert result.test.total_trades > 0

    def test_same_calendar_windows_across_modes(self) -> None:
        candidate = run_walk_forward(
            _csm_config(),
            symbols=SYMBOLS,
            timeframe="1h",
            source=_source(),
            split_ratio=0.7,
        )
        portfolio = run_walk_forward(
            _portfolio_config(),
            symbols=SYMBOLS,
            timeframe="1h",
            source=_source(),
            split_ratio=0.7,
            strategy_mode=StrategyType.PORTFOLIO,
        )
        for attr in ("train_start_ms", "train_end_ms", "test_start_ms", "test_end_ms", "train_n_bars", "test_n_bars"):
            assert getattr(candidate, attr) == getattr(portfolio, attr), attr


class TestWalkForwardPortfolio:
    def test_portfolio_csm_runs_on_both_windows(self) -> None:
        result = run_walk_forward(
            _portfolio_config(),
            symbols=SYMBOLS,
            timeframe="1h",
            source=_source(),
            split_ratio=0.7,
            strategy_mode=StrategyType.PORTFOLIO,
        )
        assert result.strategy_mode == StrategyType.PORTFOLIO
        assert result.strategy_name == MOMENTUM_V0_STRATEGY_NAME
        assert result.train.total_trades > 0
        assert result.test.total_trades > 0

    def test_timeframe_mismatch_raises(self) -> None:
        """Market-snapshot strategies must run on csm.timeframe, or lookbacks
        silently use the wrong bar units."""
        config = _portfolio_config()
        csm = config.settings.portfolio.csm.model_copy(update={"timeframe": "4h"})
        portfolio = config.settings.portfolio.model_copy(update={"csm": csm})
        settings = config.settings.model_copy(update={"portfolio": portfolio})
        bad = config.__class__(settings=settings, env=config.env)

        with pytest.raises(ValueError, match="csm.timeframe"):
            run_walk_forward(
                bad,
                symbols=SYMBOLS,
                timeframe="1h",
                source=_source(),
                split_ratio=0.7,
                strategy_mode=StrategyType.PORTFOLIO,
            )


class TestWalkForwardResultHints:
    def _result(self, train: PnLSummary, test: PnLSummary) -> WalkForwardResult:
        return WalkForwardResult(
            symbol="A/USDT", timeframe="1h", split_ratio=0.7,
            strategy_mode=StrategyType.PORTFOLIO, strategy_name=MOMENTUM_V0_STRATEGY_NAME,
            train=train, test=test,
            train_n_bars=100, test_n_bars=100,
            train_start_ms=BASE_TS, train_end_ms=BASE_TS + 100 * PERIOD_MS,
            test_start_ms=BASE_TS + 100 * PERIOD_MS, test_end_ms=BASE_TS + 200 * PERIOD_MS,
        )

    def test_not_enough_data_hint(self) -> None:
        r = self._result(
            PnLSummary(total_trades=3, total_pnl_pct=5.0, sharpe_ratio=1.5, win_rate=0.7),
            PnLSummary(total_trades=3, total_pnl_pct=-3.0, sharpe_ratio=-0.5, win_rate=0.3),
        )
        assert "NOT ENOUGH DATA" in r._overfit_hint()

    def test_consistent_results_no_hint(self) -> None:
        r = self._result(
            PnLSummary(total_trades=25, total_pnl_pct=4.0, sharpe_ratio=1.1, win_rate=0.55, max_drawdown_pct=3.0),
            PnLSummary(total_trades=25, total_pnl_pct=3.0, sharpe_ratio=0.9, win_rate=0.52, max_drawdown_pct=3.5),
        )
        assert "POSSIBLE OVERFIT" not in r._overfit_hint()
        assert "looks consistent" in r._overfit_hint()

    def test_overfit_detected(self) -> None:
        r = self._result(
            PnLSummary(total_trades=25, total_pnl_pct=5.0, sharpe_ratio=2.0, win_rate=0.7, max_drawdown_pct=1.0),
            PnLSummary(total_trades=25, total_pnl_pct=-3.0, sharpe_ratio=-0.2, win_rate=0.4, max_drawdown_pct=9.0),
        )
        assert "POSSIBLE OVERFIT" in r._overfit_hint()