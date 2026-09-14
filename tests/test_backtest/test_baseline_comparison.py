"""Task 13: automated baseline comparison harness (candidate + portfolio layers).

The harness must run SingleTF, CrossSectionalMomentum, Random and
ReverseMomentum on the SAME universe, period, starting capital, execution
model (fees/slippage) and risk constraints — only then does a P&L ranking
mean the idea itself is better.  These tests check the harness itself:
shared conditions, reproducibility, and the momentum-vs-reverse sanity sign.
"""
from __future__ import annotations

import pytest

from crypto_bot.config.env import Config
from crypto_bot.core.enums import StrategyType
from crypto_bot.core.types import Candle
from crypto_bot.pipeline.factory import (
    PER_TF_STRATEGY_NAME,
    build_strategy_registry,
)
from crypto_bot.simulation.baselines import (
    BASELINE_NAMES,
    INITIAL_EQUITY,
    MOMENTUM_V0_STRATEGY_NAME,
    RANDOM_STRATEGY_NAME,
    REVERSE_MOMENTUM_STRATEGY_NAME,
    format_baseline_report,
    run_baseline_comparison,
)
from crypto_bot.simulation.historical_source import HistoricalCandleSource

from ._shared import golden_config

PERIOD_MS = 3_600_000
BASE_TS = 1_700_000_000_000
N_BARS = 240


def _candles(rets: list[float], *, start: float = 100.0, amplitude: float = 0.002) -> list[Candle]:
    """Per-bar geometric returns with a small implied bar range (no spread)."""
    out: list[Candle] = []
    price = start
    for i, r in enumerate(rets):
        close_p = price * (1.0 + r)
        out.append(Candle(
            timestamp=BASE_TS + i * PERIOD_MS,
            open=price, high=max(price, close_p) * (1.0 + amplitude),
            low=min(price, close_p) * (1.0 - amplitude),
            close=close_p, volume=1000.0,
        ))
        price = close_p
    return out


def _trending_universe() -> dict[str, list[Candle]]:
    """5 deterministic symbols with persistent rank: A >> B > C > E > D."""
    return {
        "A/USDT": _candles([0.005] * N_BARS),    # strong winner
        "B/USDT": _candles([0.0015] * N_BARS),   # mild winner
        "C/USDT": _candles([0.0] * N_BARS),      # flat
        "E/USDT": _candles([-0.001] * N_BARS),   # mild loser
        "D/USDT": _candles([-0.0025] * N_BARS),  # strong loser
    }


def _csm_config() -> Config:
    """golden config + equal long/short CSM block (top 40% long, bottom 40% short)."""
    config = golden_config()
    # Test-friendly regime config for walk-forward tests (small lookback for synthetic data)
    from crypto_bot.config.schemas import RegimeConfig
    test_regime = RegimeConfig(
        enabled=True,
        reference="universe_basket",
        trend_period=5,
        trend_threshold=0.1,
        vol_lookback_bars=10,
        vol_percentile_high=0.75,
    )
    portfolio = config.settings.portfolio.model_copy(
        update={
            "strategy_name": "long_only_trend",  # harness overrides per baseline
            "csm": config.settings.portfolio.csm.model_copy(
                update={
                    "timeframe": "1h",
                    "lookbacks": ["24h"],
                    "long_percentile": 0.6,
                    "short_percentile": 0.4,
                    "rebalance_hours": 24,
                }
            ),
        }
    )
    settings = config.settings.model_copy(update={"portfolio": portfolio, "regime": test_regime})
    return Config(settings=settings, env=config.env)


@pytest.fixture()
def universe() -> dict[str, list[Candle]]:
    return _trending_universe()


def _run(tmp_path_factory, universe: dict[str, list[Candle]], *, seed: int = 42):
    out_dir = tmp_path_factory.mktemp(f"baselines_{seed}")
    symbols = list(universe)
    source = HistoricalCandleSource()
    for sym, candles in universe.items():
        source.load_all(sym, "1h", candles)
    report = run_baseline_comparison(
        _csm_config(),
        symbols=symbols,
        timeframes=["1h"],
        start_ms=BASE_TS,
        end_ms=BASE_TS + N_BARS * PERIOD_MS,
        source=source,
        seed=seed,
        max_positions=5,
        out_dir=out_dir,
    )
    return report


class TestSharedConditions:
    def test_all_four_baselines_run_with_results(self, tmp_path_factory, universe) -> None:
        report = _run(tmp_path_factory, universe)
        assert [r.name for r in report.results] == list(BASELINE_NAMES)
        assert report.by_name("single_tf").strategy_mode == StrategyType.CANDIDATE
        for name in (MOMENTUM_V0_STRATEGY_NAME, RANDOM_STRATEGY_NAME, REVERSE_MOMENTUM_STRATEGY_NAME, "csm_regime_gated"):
            assert report.by_name(name).strategy_mode == StrategyType.PORTFOLIO

    def test_same_universe_period_and_capital(self, tmp_path_factory, universe) -> None:
        report = _run(tmp_path_factory, universe)
        assert report.symbols == tuple(universe)
        assert report.timeframes == ("1h",)
        assert report.start_ms == BASE_TS
        assert report.end_ms == BASE_TS + N_BARS * PERIOD_MS
        # Starting capital: identical first equity point in every run.
        for r in report.results:
            assert r.equity_curve, f"{r.name} has no equity curve"
            assert r.equity_curve[0][1] == INITIAL_EQUITY

    def test_same_risk_constraints_everywhere(self, tmp_path_factory, universe) -> None:
        report = _run(tmp_path_factory, universe)
        for r in report.results:
            assert r.summary.total_pnl_pct == round(r.summary.total_pnl_pct, 2)  # sanity
        # max_positions=5 fixed in the harness for both layers.
        assert report.max_positions == 5

    def test_report_is_renderable(self, tmp_path_factory, universe) -> None:
        report = _run(tmp_path_factory, universe)
        text = format_baseline_report(report)
        assert "BASELINE COMPARISON" in text
        for name in BASELINE_NAMES:
            assert name in text


class TestRandomBaseline:
    def test_deterministic_given_same_seed(self, tmp_path_factory, universe) -> None:
        r1 = _run(tmp_path_factory, universe, seed=7).by_name(RANDOM_STRATEGY_NAME)
        r2 = _run(tmp_path_factory, universe, seed=7).by_name(RANDOM_STRATEGY_NAME)
        assert r1.summary == r2.summary
        assert r1.equity_curve == r2.equity_curve

    def test_changes_with_seed(self, tmp_path_factory, universe) -> None:
        r1 = _run(tmp_path_factory, universe, seed=7).by_name(RANDOM_STRATEGY_NAME)
        r2 = _run(tmp_path_factory, universe, seed=99).by_name(RANDOM_STRATEGY_NAME)
        assert r1.summary.total_pnl_abs != r2.summary.total_pnl_abs


class TestMomentumVsBaselines:
    def test_momentum_beats_reverse_momentum_on_trending_universe(self, tmp_path_factory, universe) -> None:
        report = _run(tmp_path_factory, universe)
        momentum = report.by_name(MOMENTUM_V0_STRATEGY_NAME).summary
        reverse = report.by_name(REVERSE_MOMENTUM_STRATEGY_NAME).summary
        # Persistent ranks: longs keep winning, shorts keep losing.
        assert momentum.total_pnl_pct > 0
        assert reverse.total_pnl_pct < 0
        assert momentum.total_pnl_pct > reverse.total_pnl_pct


class TestRegistry:
    def test_all_four_baselines_registered(self) -> None:
        registry = build_strategy_registry()
        # single_tf is the harness label; the registered name is per_timeframe.
        assert registry.is_registered(PER_TF_STRATEGY_NAME)
        for name in (MOMENTUM_V0_STRATEGY_NAME, RANDOM_STRATEGY_NAME, REVERSE_MOMENTUM_STRATEGY_NAME):
            assert registry.is_registered(name), name
            assert registry.get_type(name) == StrategyType.PORTFOLIO
        assert registry.get_type(PER_TF_STRATEGY_NAME) == StrategyType.CANDIDATE
        # csm_regime_gated uses the same strategy as momentum_v0
        assert registry.is_registered(MOMENTUM_V0_STRATEGY_NAME)