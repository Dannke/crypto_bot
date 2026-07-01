"""Storage package.

SQLite backend + typed repositories. All SQL is confined to this package; the
rest of the application talks to repository methods returning core domain types.
"""
from .db import (
    CandleRepository,
    Database,
    DecisionRepository,
    EquityRepository,
    PositionRepository,
    Repositories,
    SignalRepository,
    TradeRepository,
)

__all__ = [
    "CandleRepository",
    "Database",
    "DecisionRepository",
    "EquityRepository",
    "PositionRepository",
    "Repositories",
    "SignalRepository",
    "TradeRepository",
]
