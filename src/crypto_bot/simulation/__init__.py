"""Simulation layer: paper trading, backtesting and P&L calculations."""
from .backtest_clock import BacktestClock
from .backtester import Backtester, BacktestManifest
from .executor import ExecutionResult, SignalExecutor
from .fees import FeeCalculator
from .historical_source import HistoricalCandleSource
from .paper_position import PaperPosition
from .pnl import PnLSummary, PnLTracker
from .sl_tp import SLTPCalculator, SLTPLevels

__all__ = [
    "BacktestClock",
    "BacktestManifest",
    "Backtester",
    "ExecutionResult",
    "FeeCalculator",
    "HistoricalCandleSource",
    "PaperPosition",
    "PnLSummary",
    "PnLTracker",
    "SLTPCalculator",
    "SLTPLevels",
    "SignalExecutor",
]
