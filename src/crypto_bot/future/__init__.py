"""Future layer: interfaces for decision fusion.

Provides the fusion engine interface that will combine classical strategy
signals with ML predictions in future stages. Currently uses only classical
signals but the architecture is ready for ML integration.
"""
from .fusion import FusionEngine, FusedDecision

__all__ = [
    "FusionEngine",
    "FusedDecision",
]
