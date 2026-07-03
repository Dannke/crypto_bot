"""Signal execution for signal_only and paper trading modes.

Stage 3 scope: compute virtual positions and journal decisions.
No real exchange orders are placed.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from ..config.env import Config
from ..core.enums import Mode, OrderType, RejectReason, Side, Signal, TradeStatus
from ..core.types import DecisionRecord
from ..decision.decision_report import DecisionReport
from ..simulation.fees import FeeCalculator
from ..simulation.paper_position import PaperPosition
from ..simulation.pnl import PnLTracker
from ..simulation.sl_tp import SLTPCalculator
from ..storage.db import Repositories


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """Outcome of handling a selected decision."""

    handled: bool
    message: str
    position: PaperPosition | None = None


class SignalExecutor:
    """Handles selected decisions according to runtime mode."""

    def __init__(
        self,
        config: Config,
        repos: Repositories,
        tracker: PnLTracker | None = None,
    ) -> None:
        self._config = config
        self._settings = config.settings
        self._repos = repos
        self._tracker = tracker or PnLTracker()
        self._sltp = SLTPCalculator(
            max_stop_distance_pct=self._settings.risk.max_stop_distance_pct,
            reward_risk_ratio=self._settings.risk.take_profit_risk_multiple,
        )
        self._fees = FeeCalculator()

    @property
    def tracker(self) -> PnLTracker:
        return self._tracker

    def handle_selected(self, report: DecisionReport) -> ExecutionResult:
        """Process an accepted decision: journal + optional paper position."""
        self._persist_decision(report, accepted=True)
        self._repos.signals.insert(
            symbol=report.symbol,
            signal=report.signal,
            confidence=report.confidence,
            side=report.side,
            score=report.total_score,
            reason=report.explanation[:500] if report.explanation else "",
        )

        mode = self._config.mode
        if mode == Mode.SIGNAL_ONLY:
            return ExecutionResult(
                handled=True,
                message=f"signal logged: {report.signal.value} {report.symbol}",
            )

        if mode == Mode.PAPER:
            return self._open_paper_position(report)

        return ExecutionResult(
            handled=False,
            message="live execution is disabled until a later stage",
        )

    def handle_rejected(self, report: DecisionReport) -> None:
        """Journal a rejected candidate."""
        self._persist_decision(report, accepted=False)

    def _persist_decision(self, report: DecisionReport, *, accepted: bool) -> None:
        reason = report.reject_reason or RejectReason.LOW_SCORE
        detail = report.explanation or (report.rejected_by or "")
        record = DecisionRecord(
            timestamp=report.timestamp,
            symbol=report.symbol,
            accepted=accepted,
            reason=None if accepted else reason,
            detail=detail[:1000],
            score=report.total_score if report.total_score > 0 else None,
            signal=report.signal if report.signal != Signal.HOLD else None,
        )
        self._repos.decisions.insert(record)

    def _open_paper_position(self, report: DecisionReport) -> ExecutionResult:
        if report.side is None:
            return ExecutionResult(handled=False, message="missing side")

        open_positions = self._repos.positions.list_open()
        if len(open_positions) >= self._settings.risk.max_open_positions:
            return ExecutionResult(
                handled=False,
                message="max open positions reached",
            )
        if self._repos.positions.open_exists(report.symbol):
            return ExecutionResult(
                handled=False,
                message=f"position already open for {report.symbol}",
            )

        entry = report.features.get("last_close") or report.features.get("close", 0.0)
        if entry <= 0:
            entry = report.features.get("ema_fast", 0.0)
        if entry <= 0:
            return ExecutionResult(handled=False, message="invalid entry price")

        atr_pct = report.features.get("atr_pct", self._settings.risk.max_stop_distance_pct)
        levels = self._sltp.calculate(entry, report.side, atr_pct)

        equity = self._tracker.current_equity
        risk_amount = equity * (self._settings.risk.risk_per_trade_pct / 100.0)
        stop_distance = abs(entry - levels.stop_loss)
        if stop_distance <= 0:
            return ExecutionResult(handled=False, message="invalid stop distance")

        size = risk_amount / stop_distance
        fee = self._fees.calculate(size, entry, is_maker=False)

        position = PaperPosition(
            symbol=report.symbol,
            side=report.side,
            size=size,
            entry_price=entry,
            stop_loss=levels.stop_loss,
            take_profit=levels.take_profit,
        )
        self._tracker.add_position(position)

        position_id = self._repos.positions.insert(
            symbol=report.symbol,
            side=report.side,
            size=size,
            entry_price=entry,
            stop=levels.stop_loss,
            take=levels.take_profit,
            mode=Mode.PAPER,
            opened_at_ms=int(datetime.now(tz=UTC).timestamp() * 1000),
            status=TradeStatus.OPEN,
        )
        self._repos.trades.insert(
            position_id=position_id,
            symbol=report.symbol,
            side=report.side,
            order_type=OrderType.MARKET,
            size=size,
            price=entry,
            mode=Mode.PAPER,
        )

        return ExecutionResult(
            handled=True,
            message=(
                f"paper {report.side.value} {report.symbol} "
                f"entry={entry:.4f} stop={levels.stop_loss:.4f} "
                f"take={levels.take_profit:.4f} fee={fee.fee_abs:.4f}"
            ),
            position=position,
        )
