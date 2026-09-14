"""Simulation layer: paper trading, backtesting and P&L calculations."""
from .backtest_clock import BacktestClock
from .backtester import Backtester, BacktestManifest
from .decision_aggregate import aggregate_raw_rows
from .decision_diff import DiffEntry, DiffReport, compare_decisions
from .executor import ExecutionResult, SignalExecutor
from .fees import FeeCalculator
from .historical_source import HistoricalCandleSource
from .paper_position import PaperPosition
from .pnl import PnLSummary, PnLTracker
from .portfolio_executor import PortfolioExecutionResult, PortfolioExecutor
from .sl_tp import SLTPCalculator, SLTPLevels

__all__ = [
    "BacktestClock",
    "BacktestManifest",
    "Backtester",
    "DiffEntry",
    "DiffReport",
    "ExecutionResult",
    "FeeCalculator",
    "HistoricalCandleSource",
    "PaperPosition",
    "PnLSummary",
    "PnLTracker",
    "PortfolioExecutionResult",
    "PortfolioExecutor",
    "SLTPCalculator",
    "SLTPLevels",
    "SignalExecutor",
    "aggregate_raw_rows",
    "compare_decisions",
]
