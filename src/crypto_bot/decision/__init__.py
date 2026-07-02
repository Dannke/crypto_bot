"""Decision layer: generates explainable decision reports.

Every trading decision is accompanied by a full report explaining the
rationale, including sub-scores, rejected filters, and final signal.
"""
from .decision_report import DecisionReport
from .explanation import ExplanationGenerator

__all__ = [
    "DecisionReport",
    "ExplanationGenerator",
]
