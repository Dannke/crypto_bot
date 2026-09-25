"""Walk-forward analysis for both decision layers (Task 14).

Runs the Backtester on two consecutive calendar windows (train / test) with
the *same* decision pipeline that the live orchestrator uses — no copied logic, no shortcuts.

Supports multi-symbol multi-timeframe backtesting with a unified clock that
ticks on *every* bar close across *all* configured timeframes — the same
shared-capital regime as the live SignalExecutor.

Two decision layers run through the SAME replay loop, selected with the
``strategy_mode`` argument::

    Backtester
        ├── candidate  (default) — DecisionPipeline (SingleTF per_timeframe) ─> SignalExecutor
        └── portfolio  — PortfolioDecisionPipeline ─> PortfolioStrategy
                          ─> PortfolioRiskEngine ─> PortfolioExecutor

Usage (from CLI)::

    crypto-bot backtest BTC/USDT 1h --start 2025-01-01 --end 2025-02-01
"""
from __future__ import annotations

import asyncio
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, overload

from ..config.env import Config
from ..config.schemas import RegimeConfig
from ..core.enums import StrategyType
from ..core.logging_setup import get_logger
from ..core.types import Candle
from ..execution.costs import CompositeCostModel
from ..features.context import SymbolMarketContext
from ..portfolio import (
    PortfolioRiskLimits,
)
from ..simulation.backtester import Backtester
from ..simulation.pnl import PnLSummary
from ..storage.db import Database
from .historical_source import HistoricalCandleSource

logger = get_logger(__name__)


def pin_candles(
    candles: list[Candle], start_ms: int | None = None, end_ms: int | None = None,
) -> list[Candle]:
    """Bars whose OPEN time lies in ``[start_ms, end_ms)``; None leaves that side open.

    Pinning the window makes the split a function of the registration, not of
    how much history the database happens to hold on the day of the run.
    """
    return [
        c for c in candles
        if (start_ms is None or c.timestamp >= start_ms)
        and (end_ms is None or c.timestamp < end_ms)
    ]


@dataclass(slots=True)
class WalkForwardResult:
    """Result of a walk-forward analysis with train/test windows (2-way) or train/validation/test (3-way)."""

    symbol: str
    timeframe: str
    split_ratio: float
    strategy_mode: StrategyType
    strategy_name: str
    train: PnLSummary
    test: PnLSummary
    train_n_bars: int
    test_n_bars: int
    train_start_ms: int
    train_end_ms: int
    test_start_ms: int
    test_end_ms: int
    # 3-way split fields
    validation: PnLSummary | None = None
    validation_n_bars: int | None = None
    validation_start_ms: int | None = None
    validation_end_ms: int | None = None
    split_type: str = "2-way"  # "2-way" or "3-way"

    def _overfit_hint(self) -> str:
        """Generate a human-readable overfit diagnostic."""
        if self.train.total_trades < 5 or self.test.total_trades < 5:
            return f"NOT ENOUGH DATA (train={self.train.total_trades} trades, test={self.test.total_trades} trades)"
        
        pnl_diff = self.train.total_pnl_pct - self.test.total_pnl_pct
        sharpe_diff = self.train.sharpe_ratio - self.test.sharpe_ratio
        
        hints = []
        if pnl_diff > 5.0:
            hints.append(f"PnL drop {pnl_diff:.1f}%")
        if sharpe_diff > 1.0:
            hints.append(f"Sharpe drop {sharpe_diff:.2f}")
        if self.train.win_rate - self.test.win_rate > 0.2:
            hints.append(f"WinRate drop {self.train.win_rate - self.test.win_rate:.0%}")
        
        if hints:
            return "POSSIBLE OVERFIT: " + "; ".join(hints)
        return f"looks consistent (train PnL {self.train.total_pnl_pct:.1f}%, test PnL {self.test.total_pnl_pct:.1f}%)"

    def print(self) -> None:
        """Print a formatted summary."""
        if self.split_type == "3-way" and self.validation is not None:
            print(f"  Train:      {self.train.total_trades} trades, PnL={self.train.total_pnl_pct:.2f}%, Sharpe={self.train.sharpe_ratio:.2f}, WinRate={self.train.win_rate:.1%}, MaxDD={self.train.max_drawdown_pct:.2f}%")
            print(f"  Validation: {self.validation.total_trades} trades, PnL={self.validation.total_pnl_pct:.2f}%, Sharpe={self.validation.sharpe_ratio:.2f}, WinRate={self.validation.win_rate:.1%}, MaxDD={self.validation.max_drawdown_pct:.2f}%")
            print(f"  Test:       {self.test.total_trades} trades, PnL={self.test.total_pnl_pct:.2f}%, Sharpe={self.test.sharpe_ratio:.2f}, WinRate={self.test.win_rate:.1%}, MaxDD={self.test.max_drawdown_pct:.2f}%")
        else:
            print(f"  Train:  {self.train.total_trades} trades, PnL={self.train.total_pnl_pct:.2f}%, Sharpe={self.train.sharpe_ratio:.2f}, WinRate={self.train.win_rate:.1%}, MaxDD={self.train.max_drawdown_pct:.2f}%")
            print(f"  Test:   {self.test.total_trades} trades, PnL={self.test.total_pnl_pct:.2f}%, Sharpe={self.test.sharpe_ratio:.2f}, WinRate={self.test.win_rate:.1%}, MaxDD={self.test.max_drawdown_pct:.2f}%")
        print(f"  Hint:   {self._overfit_hint()}")


@overload
def calendar_split(
    candles: list[Candle], period_ms: int, ratio: float,
    three_way: Literal[False] = False, validation_ratio: float = 0.2
) -> tuple[int, int, int, int]: ...

@overload
def calendar_split(
    candles: list[Candle], period_ms: int, ratio: float,
    three_way: Literal[True], validation_ratio: float = 0.2
) -> tuple[int, int, int, int, int, int]: ...

def calendar_split(
    candles: list[Candle], period_ms: int, ratio: float,
    three_way: bool = False, validation_ratio: float = 0.2
) -> tuple[int, int, int, int] | tuple[int, int, int, int, int, int]:
    """Split candles into train/test windows by calendar time.

    Args:
        candles: List of candles sorted by timestamp (ascending).
        period_ms: Bar period in milliseconds (e.g., 3600000 for 1h).
        ratio: Train fraction (0 < ratio < 1).
        three_way: If True, split into train/validation/test (3-way).
        validation_ratio: Fraction for validation window (used when three_way=True).
                         Must satisfy ratio + validation_ratio < 1.

    Returns:
        For 2-way: Tuple of (train_start_ms, train_end_ms, test_start_ms, test_end_ms).
        For 3-way: Tuple of (train_start_ms, train_end_ms, validation_start_ms, validation_end_ms, test_start_ms, test_end_ms).
    """
    if not candles:
        raise ValueError("No candles provided")
    if not 0 < ratio < 1:
        raise ValueError("ratio must be between 0 and 1 (exclusive)")
    if three_way:
        if not 0 < validation_ratio < 1:
            raise ValueError("validation_ratio must be between 0 and 1 (exclusive)")
        if not ratio + validation_ratio < 1:
            raise ValueError("ratio + validation_ratio must be < 1")

    first_ts = candles[0].timestamp
    last_ts = candles[-1].timestamp + period_ms
    total_ms = last_ts - first_ts

    if three_way:
        train_ms = int(total_ms * ratio)
        val_ms = int(total_ms * validation_ratio)
        
        train_start = first_ts
        train_end = ((first_ts + train_ms - first_ts) // period_ms) * period_ms + first_ts
        val_start = train_end
        val_end = ((val_start + val_ms - first_ts) // period_ms) * period_ms + first_ts
        test_start = val_end
        test_end = last_ts
        
        return train_start, train_end, val_start, val_end, test_start, test_end
    else:
        split_ms = first_ts + int(total_ms * ratio)
        # Align to bar boundary
        split_ms = ((split_ms - first_ts) // period_ms) * period_ms + first_ts
        return first_ts, split_ms, split_ms, last_ts


async def _run_single_window(
    config: Config,
    symbols: list[str],
    timeframe: str,
    source: HistoricalCandleSource,
    start_ms: int | None,
    end_ms: int | None,
    *,
    strategy_mode: StrategyType,
    portfolio_limits: PortfolioRiskLimits | None = None,
    regime_config: RegimeConfig | None = None,
    market_map: dict[str, SymbolMarketContext] | None = None,
    db_path: str | None = None,
    # R0.5: Funding/cost model options
    enable_funding: bool = True,
    max_leverage: float = 10.0,
    maintenance_margin_buffer_pct: float = 0.1,
) -> PnLSummary:
    """Run a single backtest window."""
    from ..storage.db import Database

    if start_ms is None or end_ms is None:
        raise ValueError("start_ms and end_ms must be provided for _run_single_window")

    if db_path is None:
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            db_path = tmp.name

    db = Database(db_path)
    bt = Backtester(
        config,
        symbols=symbols,
        timeframes=[timeframe],
        start_ms=start_ms,
        end_ms=end_ms,
        source=source,
        db=db,
        strategy_mode=strategy_mode,
        portfolio_limits=portfolio_limits,
        regime_config=regime_config,
        market_map=market_map,
    )
    # R0.5: Apply cost model (funding-aware, legacy, or maker-only for MR post-only)
    strategy_name = getattr(config.settings.portfolio, 'strategy_name', None)
    mr = getattr(config.settings.portfolio, 'mean_reversion', None)
    post_only = (
        getattr(mr, 'entry_execution', None) == 'post_only' and
        getattr(mr, 'exit_execution', None) == 'post_only'
    )
    if enable_funding:
        if post_only and strategy_name == 'mean_reversion_v0':
            cost_model = CompositeCostModel.bybit_perp_maker_only()
        else:
            cost_model = CompositeCostModel.bybit_perp_default()
    else:
        cost_model = CompositeCostModel.legacy_default()
    bt._executor._costs = cost_model  # type: ignore[attr-defined]
    return await bt.run_async()


def run_walk_forward(
    config: Config,
    symbols: list[str],
    timeframe: str,
    source: HistoricalCandleSource,
    split_ratio: float,
    *,
    strategy_mode: StrategyType = StrategyType.CANDIDATE,
    portfolio_limits: PortfolioRiskLimits | None = None,
    regime_config: RegimeConfig | None = None,
    market_map: dict[str, SymbolMarketContext] | None = None,
    db_dir: str | None = None,
    # R0.5: Funding/cost model options
    enable_funding: bool = True,
    max_leverage: float = 10.0,
    maintenance_margin_buffer_pct: float = 0.1,
    # 3-way split options
    three_way: bool = False,
    validation_ratio: float = 0.2,
    # Pinned data window: first symbol's bars with open time in [start_ms, end_ms)
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> WalkForwardResult:
    """Run walk-forward analysis on calendar windows (2-way or 3-way split).

    Args:
        config: Configuration object.
        symbols: List of symbols to backtest.
        timeframe: Timeframe for the backtest (e.g., "1h").
        source: Historical candle source with loaded data.
        split_ratio: Train fraction (0 < ratio < 1).
        strategy_mode: CANDIDATE or PORTFOLIO.
        portfolio_limits: Optional risk limits for portfolio mode.
        regime_config: Optional regime configuration for portfolio mode.
                       If not provided, uses config.settings.regime.
        market_map: Optional market context for symbols.
        db_dir: Optional directory to persist train/test/validation databases.
        enable_funding: Use Bybit perp cost model with funding (default True).
        max_leverage: Max leverage for portfolio layer (margin check).
        maintenance_margin_buffer_pct: Maintenance margin buffer (fraction).
        three_way: If True, use 3-way split (train/validation/test).
        validation_ratio: Validation fraction when three_way=True.
        start_ms: Pin the split to bars opening at or after this time (None = first bar).
        end_ms: Pin the split to bars opening before this time (None = last bar).
                History before start_ms stays in ``source`` for warmup.

    Returns:
        WalkForwardResult with train/validation/test summaries and overfit hint.
    """

    # Use regime config from settings if not explicitly provided
    if regime_config is None and strategy_mode == StrategyType.PORTFOLIO:
        regime_config = config.settings.regime

    # Use first symbol's candles to determine calendar windows
    first_symbol = symbols[0]
    candles = pin_candles(
        source.slice_between(0, 2**63 - 1, first_symbol, timeframe), start_ms, end_ms,
    )
    if not candles:
        raise ValueError(
            f"No candles found for {first_symbol} {timeframe} "
            f"in the window [{start_ms}, {end_ms})"
        )

    period_ms = 3600000 if timeframe == "1h" else (900000 if timeframe == "15m" else 14400000)

    val_start: int | None
    val_end: int | None
    if three_way:
        train_start, train_end, val_start, val_end, test_start, test_end = calendar_split(
            candles, period_ms, split_ratio, three_way=True, validation_ratio=validation_ratio
        )
    else:
        train_start, train_end, test_start, test_end = calendar_split(
            candles, period_ms, split_ratio
        )
        val_start = val_end = None

    # Count bars in each window
    train_candles = [c for c in candles if train_start <= c.timestamp < train_end]
    val_candles = [c for c in candles if val_start is not None and val_end is not None and val_start <= c.timestamp < val_end]
    test_candles = [c for c in candles if test_start <= c.timestamp < test_end]

    # Build db paths if requested
    train_db = val_db = test_db = None
    if db_dir:
        Path(db_dir).mkdir(parents=True, exist_ok=True)
        train_db = str(Path(db_dir) / "train.db")
        if three_way:
            val_db = str(Path(db_dir) / "validation.db")
        test_db = str(Path(db_dir) / "test.db")

    # Run train window
    train_summary = asyncio.run(_run_single_window(
        config, symbols, timeframe, source,
        train_start, train_end,
        strategy_mode=strategy_mode,
        portfolio_limits=portfolio_limits,
        regime_config=regime_config,
        market_map=market_map,
        db_path=train_db,
        enable_funding=enable_funding,
        max_leverage=max_leverage,
        maintenance_margin_buffer_pct=maintenance_margin_buffer_pct,
    ))

    # Run validation window (3-way only)
    validation_summary = None
    if three_way:
        validation_summary = asyncio.run(_run_single_window(
            config, symbols, timeframe, source,
            val_start, val_end,
            strategy_mode=strategy_mode,
            portfolio_limits=portfolio_limits,
            regime_config=regime_config,
            market_map=market_map,
            db_path=val_db,
            enable_funding=enable_funding,
            max_leverage=max_leverage,
            maintenance_margin_buffer_pct=maintenance_margin_buffer_pct,
        ))

    # Run test window
    test_summary = asyncio.run(_run_single_window(
        config, symbols, timeframe, source,
        test_start, test_end,
        strategy_mode=strategy_mode,
        portfolio_limits=portfolio_limits,
        regime_config=regime_config,
        market_map=market_map,
        db_path=test_db,
        enable_funding=enable_funding,
        max_leverage=max_leverage,
        maintenance_margin_buffer_pct=maintenance_margin_buffer_pct,
    ))

    # Get strategy name
    if strategy_mode == StrategyType.CANDIDATE:
        strategy_name = "per_timeframe"
    else:
        strategy_name = getattr(config.settings.portfolio, "strategy_name", "unknown")

    return WalkForwardResult(
        symbol=first_symbol,
        timeframe=timeframe,
        split_ratio=split_ratio,
        strategy_mode=strategy_mode,
        strategy_name=strategy_name,
        train=train_summary,
        test=test_summary,
        validation=validation_summary,
        train_n_bars=len(train_candles),
        test_n_bars=len(test_candles),
        validation_n_bars=len(val_candles) if three_way else None,
        train_start_ms=train_start,
        train_end_ms=train_end,
        validation_start_ms=val_start,
        validation_end_ms=val_end,
        test_start_ms=test_start,
        test_end_ms=test_end,
        split_type="3-way" if three_way else "2-way",
    )


def fetch_all_candles(db_path: str, symbols: list[str], timeframe: str) -> dict[str, list[Candle]]:
    """Fetch all candles for multiple symbols from the database."""
    from ..storage.db import CandleRepository

    db = Database(db_path)
    try:
        repo = CandleRepository(db)
        result = {}
        for sym in symbols:
            candles = repo.fetch_since(sym, timeframe, since_ms=0)
            result[sym] = candles
        return result
    finally:
        db.close()