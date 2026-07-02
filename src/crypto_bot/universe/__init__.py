"""Universe layer: manages which symbols to analyze.

Provides flexible universe selection including static lists, auto-discovery
by liquidity, and dynamic updates based on market conditions.
"""
from .selector import UniverseSelector
from .updater import UniverseUpdater

__all__ = [
    "UniverseSelector",
    "UniverseUpdater",
]
