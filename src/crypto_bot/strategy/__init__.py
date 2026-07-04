"""Strategy package.

Pure functions: signal engine (multi-timeframe confluence) and scorer
(composite score). No I/O dependency — these import only core types, indicators
and features. They are reused verbatim by the backtester later.
"""
from .base import Strategy, StrategyContext
from .scorer import Scorer, select_top_candidates
from .signal_engine import SignalEngine
from .single_tf import SingleTfEngine

__all__ = ["Strategy", "StrategyContext", "Scorer", "SignalEngine", "SingleTfEngine", "select_top_candidates"]
