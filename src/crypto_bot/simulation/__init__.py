"""Simulation layer: paper trading position and P&L calculations.

Provides a complete simulation framework for paper trading without real
order execution. Includes position management, stop-loss/take-profit
calculations, fee estimation, and P&L tracking.
"""
from .fees import FeeCalculator
from .paper_position import PaperPosition
from .pnl import PnLCalculator
from .sl_tp import SLTPCalculator

__all__ = [
    "FeeCalculator",
    "PaperPosition",
    "PnLCalculator",
    "SLTPCalculator",
]
