"""Strategy package.

Pure functions: signal engines (cross-timeframe confluence and per-timeframe).
No I/O dependency — import only core types, indicators and features.
Reused verbatim by the backtester.
"""
from .base import Strategy, StrategyContext
from .signal_engine import SignalEngine
from .single_tf import SingleTfEngine

__all__ = ["Strategy", "StrategyContext", "SignalEngine", "SingleTfEngine"]
