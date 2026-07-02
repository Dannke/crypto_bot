"""Filter layer: independent quality filters for candidate evaluation.

Each filter implements a simple contract: PASS or REJECT with a reason.
Filters are stateless and independent of each other for maximum testability.
"""
from .base import Filter, FilterResult
from .blacklist import BlacklistFilter
from .cooldown import CooldownFilter
from .liquidity import LiquidityFilter
from .spread import SpreadFilter
from .trend import TrendFilter
from .volatility import VolatilityFilter
from .volume import VolumeFilter

__all__ = [
    "Filter",
    "FilterResult",
    "BlacklistFilter",
    "CooldownFilter",
    "LiquidityFilter",
    "SpreadFilter",
    "TrendFilter",
    "VolatilityFilter",
    "VolumeFilter",
]
