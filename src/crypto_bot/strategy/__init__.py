"""Strategy package.

Pure functions: signal engines (cross-timeframe confluence and per-timeframe).
No I/O dependency — import only core types, indicators and features.
Reused verbatim by the backtester.
"""
from .base import CandidateStrategy, PortfolioStrategy, Strategy, StrategyContext
from .portfolio_strategies import (
    MEAN_REVERSION_V0_STRATEGY_NAME,
    MOMENTUM_V0_STRATEGY_NAME,
    RANDOM_STRATEGY_NAME,
    REVERSE_MOMENTUM_STRATEGY_NAME,
    CrossSectionalMomentumStrategy,
    LongOnlyTrendPortfolioStrategy,
    MeanReversionStrategy,
)
from .signal_engine import SignalEngine
from .single_tf import SingleTfEngine

__all__ = [
    "CandidateStrategy",
    "CrossSectionalMomentumStrategy",
    "LongOnlyTrendPortfolioStrategy",
    "MeanReversionStrategy",
    "MEAN_REVERSION_V0_STRATEGY_NAME",
    "MOMENTUM_V0_STRATEGY_NAME",
    "RANDOM_STRATEGY_NAME",
    "REVERSE_MOMENTUM_STRATEGY_NAME",
    "PortfolioStrategy",
    "Strategy",
    "StrategyContext",
    "SignalEngine",
    "SingleTfEngine",
]
