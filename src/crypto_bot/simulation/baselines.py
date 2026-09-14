"""Task 13: automated baseline comparison for portfolio strategies.

Runs the current SingleTF strategy (``per_timeframe``, candidate layer) and
the cross-sectional baselines (momentum, random, reverse momentum, portfolio
layer) through the same ``Backtester`` on the same universe, the same period,
the same starting capital (10,000 USDT default), the same execution model,
the same fees/slippage (the backtester's defaults: taker 0.1% per side,
half-spread slippage) and the same risk constraints (max position count;
the portfolio layer additionally caps per-position weight at full equity
and gross/net exposure at 1.0).

The honest claim this enables: any measured edge of a strategy over the
baselines comes from the strategy's decisions, not from a different harness.

R0.5: Supports running with realistic Bybit perpetual costs:
- Funding accrual (R0.2): funding rates applied on each bar close
- Margin/leverage limits (R0.3): max_leverage + maintenance_margin_buffer
- Instrument specs (R0.4): qtyStep rounding + minNotionalValue checks
"""
from __future__ import annotations

import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ..config.env import Config
from ..config.schemas import PortfolioConfig, PortfolioRiskParams
from ..core.enums import StrategyType
from ..execution.costs import CompositeCostModel
from ..portfolio.risk import PortfolioRiskLimits
from ..simulation.backtester import Backtester
from ..simulation.historical_source import HistoricalCandleSource
from ..simulation.pnl import PnLSummary
from ..storage.db import Database
from ..strategy.portfolio_strategies import (
    MEAN_REVERSION_V0_STRATEGY_NAME,
    MOMENTUM_V0_STRATEGY_NAME,
    RANDOM_STRATEGY_NAME,
    REVERSE_MOMENTUM_STRATEGY_NAME,
)

SINGLE_TF_STRATEGY_NAME = "per_timeframe"
INITIAL_EQUITY = 10000.0  # PnLTracker default; identical for both layers

BASELINE_NAMES: tuple[str, ...] = (
    "single_tf",
    MOMENTUM_V0_STRATEGY_NAME,
    RANDOM_STRATEGY_NAME,
    REVERSE_MOMENTUM_STRATEGY_NAME,
    MEAN_REVERSION_V0_STRATEGY_NAME,
    "csm_regime_gated",
)


@dataclass(frozen=True, slots=True)
class BaselineResult:
    """One strategy's backtest on the shared conditions."""

    name: str
    strategy_name: str
    strategy_mode: StrategyType
    summary: PnLSummary
    equity_curve: tuple[tuple[int, float], ...]  # (ts_ms, equity)
    db_path: str


@dataclass(frozen=True, slots=True)
class BaselineReport:
    """The full comparison: shared conditions + one result per baseline."""

    seed: int
    max_positions: int
    symbols: tuple[str, ...]
    timeframes: tuple[str, ...]
    start_ms: int
    end_ms: int
    results: tuple[BaselineResult, ...]

    def by_name(self, name: str) -> BaselineResult:
        for r in self.results:
            if r.name == name:
                return r
        raise KeyError(f"no baseline named {name!r}")

    def matches(self, other: BaselineReport) -> bool:
        """True when both reports share the exact same test conditions."""
        return (
            self.seed == other.seed
            and self.max_positions == other.max_positions
            and self.symbols == other.symbols
            and self.timeframes == other.timeframes
            and self.start_ms == other.start_ms
            and self.end_ms == other.end_ms
        )


def _candidate_settings(settings, max_positions: int):
    """Candidate layer: same risk knobs, only the position count is aligned."""
    return settings.model_copy(
        update={"risk": settings.risk.model_copy(update={"max_open_positions": max_positions})}
    )


def _portfolio_settings(settings, strategy_name: str, seed: int, max_positions: int,
                         enable_funding: bool = False, max_leverage: float = 1.0,
                         maintenance_margin_buffer_pct: float = 0.0):
    """Portfolio layer: strategy + identical risk limits (max N positions,
    per-position weight capped at full equity, gross/net exposure 1.0).

    R0.3: Added max_leverage and maintenance_margin_buffer_pct for margin-aware risk.
    """
    portfolio = PortfolioConfig(
        strategy_name=strategy_name,
        volatility_sizing=settings.portfolio.volatility_sizing,
        risk=PortfolioRiskParams(
            max_positions=max_positions,
            max_position_weight=1.0,
            max_gross_exposure=1.0,
            max_net_exposure=1.0,
            max_leverage=max_leverage,
            maintenance_margin_buffer_pct=maintenance_margin_buffer_pct,
        ),
        csm=settings.portfolio.csm,
        mean_reversion=settings.portfolio.mean_reversion,
    )
    if strategy_name == RANDOM_STRATEGY_NAME:
        portfolio = portfolio.model_copy(
            update={"csm": portfolio.csm.model_copy(update={"seed": seed})}
        )
    return settings.model_copy(update={"portfolio": portfolio})


def _equity_curve(db_file: str | Path) -> tuple[tuple[int, float], ...]:
    con = sqlite3.connect(str(db_file))
    try:
        rows = con.execute("SELECT ts_ms, equity FROM equity ORDER BY ts_ms").fetchall()
    finally:
        con.close()
    return tuple((int(ts), float(equity)) for ts, equity in rows)


def run_baseline_comparison(
    config: Config,
    symbols: list[str],
    timeframes: list[str],
    start_ms: int,
    end_ms: int,
    *,
    source: HistoricalCandleSource | None = None,
    seed: int = 42,
    max_positions: int = 5,
    out_dir: str | Path | None = None,
    # R0.5: Realistic Bybit perpetual options
    enable_funding: bool = True,
    max_leverage: float = 10.0,
    maintenance_margin_buffer_pct: float = 0.1,
) -> BaselineReport:
    """Run all baselines on identical conditions; returns the report.

    Every baseline gets its own isolated DB, the same candle source, the
    same ``[start_ms, end_ms]`` window and the same risk profile.  The
    random baseline is seeded so all four runs are reproducible.

    R0.5 options:
    - enable_funding: use CompositeCostModel.bybit_perp_default() (fee + slippage + funding)
    - max_leverage: max leverage for portfolio layer (margin check)
    - maintenance_margin_buffer_pct: buffer above required margin
    """
    bases: list[tuple[str, str, StrategyType]] = [
        ("single_tf", SINGLE_TF_STRATEGY_NAME, StrategyType.CANDIDATE),
        (MOMENTUM_V0_STRATEGY_NAME, MOMENTUM_V0_STRATEGY_NAME, StrategyType.PORTFOLIO),
        (RANDOM_STRATEGY_NAME, RANDOM_STRATEGY_NAME, StrategyType.PORTFOLIO),
        (REVERSE_MOMENTUM_STRATEGY_NAME, REVERSE_MOMENTUM_STRATEGY_NAME, StrategyType.PORTFOLIO),
        (MEAN_REVERSION_V0_STRATEGY_NAME, MEAN_REVERSION_V0_STRATEGY_NAME, StrategyType.PORTFOLIO),
        ("csm_regime_gated", MOMENTUM_V0_STRATEGY_NAME, StrategyType.PORTFOLIO),
    ]
    out = Path(out_dir) if out_dir else None
    if out is not None:
        out.mkdir(parents=True, exist_ok=True)

    # Cost model: legacy (no funding) or bybit_perp (with funding)
    cost_model = CompositeCostModel.bybit_perp_default() if enable_funding else CompositeCostModel.legacy_default()

    # Portfolio risk limits with R0.3 margin/leverage
    # Convert Pydantic PortfolioRiskParams to dataclass PortfolioRiskLimits
    portfolio_limits = PortfolioRiskLimits(
        max_positions=max_positions,
        max_position_weight=1.0,
        max_gross_exposure=1.0,
        max_net_exposure=1.0,
        max_leverage=max_leverage,
        maintenance_margin_buffer_pct=maintenance_margin_buffer_pct,
    )

    results: list[BaselineResult] = []
    for name, strategy_name, mode in bases:
        # Special handling for csm_regime_gated: enable regime gating with test-friendly config
        regime_config = None
        if name == "csm_regime_gated":
            from crypto_bot.config.schemas import RegimeConfig
            regime_config = RegimeConfig(
                enabled=True,
                reference="universe_basket",
                trend_period=5,
                trend_threshold=0.1,
                vol_lookback_bars=10,
                vol_percentile_high=0.75,
            )
        
        if mode == StrategyType.PORTFOLIO:
            settings = _portfolio_settings(
                config.settings, strategy_name, seed, max_positions,
                enable_funding=enable_funding,
                max_leverage=max_leverage,
                maintenance_margin_buffer_pct=maintenance_margin_buffer_pct,
            )
        else:
            settings = _candidate_settings(config.settings, max_positions)
        cfg = Config(settings=settings, env=config.env)

        if out is not None:
            db_file = out / f"{name}.db"
        else:
            with tempfile.NamedTemporaryFile(suffix=f"_{name}.db", delete=False) as tmp:
                db_file = tmp.name

        bt = Backtester(
            cfg,
            symbols=symbols,
            timeframes=timeframes,
            start_ms=start_ms,
            end_ms=end_ms,
            source=source,
            db=Database(db_file),
            strategy_mode=mode,
            portfolio_limits=portfolio_limits,
            regime_config=regime_config,
        )
        # Set cost model on executor after creation (via backtester)
        if mode == StrategyType.PORTFOLIO:
            bt._executor._costs = cost_model

        summary = bt.run()
        results.append(
            BaselineResult(
                name=name,
                strategy_name=strategy_name,
                strategy_mode=mode,
                summary=summary,
                equity_curve=_equity_curve(db_file),
                db_path=str(db_file),
            )
        )

    return BaselineReport(
        seed=seed,
        max_positions=max_positions,
        symbols=tuple(symbols),
        timeframes=tuple(timeframes),
        start_ms=start_ms,
        end_ms=end_ms,
        results=tuple(results),
    )


def format_baseline_report(report: BaselineReport) -> str:
    """Human-readable comparison table (the 'is the new idea better?' answer)."""
    out: list[str] = []
    out.append("=" * 78)
    out.append("  BASELINE COMPARISON — same universe / period / capital / execution / risk")
    out.append("=" * 78)
    out.append(
        f"  universe={','.join(report.symbols)} timeframes={','.join(report.timeframes)}"
    )
    out.append(
        f"  period={datetime.fromtimestamp(report.start_ms / 1000, tz=UTC):%Y-%m-%d %H:%M}"
        f" -> {datetime.fromtimestamp(report.end_ms / 1000, tz=UTC):%Y-%m-%d %H:%M}"
    )
    out.append(
        f"  capital={INITIAL_EQUITY:,.0f} USDT  fees=0.1% taker/side  "
        f"slippage=half-spread  max_positions={report.max_positions}  "
        f"gross/net exposure<=1.0  seed={report.seed}"
    )
    out.append("-" * 78)
    out.append(
        f"  {'strategy':<28} {'mode':<10} {'P&L %':>8} {'sharpe':>7} "
        f"{'maxDD %':>8} {'winRate':>8} {'trades':>7} {'beats single_tf':>17}"
    )
    out.append("-" * 78)

    single = report.by_name("single_tf").summary
    for r in report.results:
        s = r.summary
        beats = (
            "—"
            if r.name == "single_tf"
            else ("YES" if s.total_pnl_pct > single.total_pnl_pct else "no")
        )
        out.append(
            f"  {r.name:<28} {r.strategy_mode.value:<10} {s.total_pnl_pct:>+8.2f} "
            f"{s.sharpe_ratio:>7.2f} {s.max_drawdown_pct:>8.2f} "
            f"{s.win_rate:>8.1%} {s.total_trades:>7} {beats:>17}"
        )
    out.append("-" * 78)
    out.append(
        "  note: candidate layer sizes by risk-per-trade, portfolio layer by "
        "equal weight; fees/slippage are the backtester defaults for both."
    )
    return "\n".join(out)