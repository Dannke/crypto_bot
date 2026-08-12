"""Strategy base contract.

Defines the common interface every strategy variant will implement, plus a
``StrategyContext`` — the read-only bundle of strategy parameters a strategy
needs. Keeping this abstract means we can add alternative strategies later
without touching the orchestrator.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from ..core.types import FeatureSet


@dataclass(frozen=True, slots=True)
class StrategyContext:
    """Resolved, read-only strategy parameters handed to a strategy.

    Decoupling the raw ``Settings`` from what a strategy reads keeps the
    strategy layer independent of the config schema's evolution.
    """

    scoring_weights: dict[str, float]
    min_score: float
    min_confidence: float
    adx_min: float
    rsi_long: tuple[float, float]
    rsi_short: tuple[float, float]
    atr_min_pct: float | dict[str, float]
    atr_max_pct: float | dict[str, float]
    take_profit_risk_multiple: float | dict[str, float]
    max_stop_distance_pct: float = 3.0
    raw: dict[str, Any] | None = None


class Strategy(ABC):
    """A strategy evaluates feature sets and produces directional signals.

    The only contract a strategy must fulfil is ``evaluate`` — it returns a
    ``SignalResult`` (BUY/SELL/HOLD per symbol).  Scoring is handled by
    ``ScoreEngine`` in the pipeline, not by the strategy itself.
    """

    @abstractmethod
    def evaluate(self, symbol: str, features_by_tf: dict[str, FeatureSet]) -> Any:
        """Produce a directional signal from multi-timeframe features."""
