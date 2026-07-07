"""Signal execution for signal_only and paper trading modes.

Stage 3 scope: compute virtual positions and journal decisions.
No real exchange orders are placed.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..config.env import Config
from ..core.enums import Mode, OrderType, RejectReason, Signal, TradeStatus
from ..core.logging_setup import get_logger
from ..core.types import DecisionRecord
from ..decision.decision_report import DecisionReport
from ..simulation.fees import FeeCalculator
from ..simulation.paper_position import PaperPosition
from ..simulation.pnl import PnLTracker
from ..simulation.sl_tp import SLTPCalculator
from ..storage.db import Repositories

logger = get_logger(__name__)


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
        self._tracker = tracker or self._restore_tracker()
        self._sltp = SLTPCalculator(
            max_stop_distance_pct=self._settings.risk.max_stop_distance_pct,
            reward_risk_ratio=self._settings.risk.take_profit_risk_multiple,
        )
        self._fees = FeeCalculator()

    def _restore_tracker(self, symbol: str | None = None) -> PnLTracker:
        """Restore PnLTracker from DB on restart (open + closed positions)."""
        closed = self._repos.positions.list_closed(symbol=symbol, limit=10000)
        realized_pnl = sum(
            (p.pnl_pct or 0.0) / 100.0 * (p.entry_price * p.size)
            for p in closed if p.pnl_pct is not None
        )
        initial = 10000.0
        equity = initial + realized_pnl
        peak = max(initial, equity)

        tracker = PnLTracker(initial_equity=initial, current_equity=equity, peak_equity=peak)

        # Restore open positions so SL/TP checking works after restart
        open_positions = self._repos.positions.list_open(symbol=symbol)
        for p in open_positions:
            pp = PaperPosition(
                symbol=p.symbol,
                timeframe=p.timeframe,
                side=p.side,
                size=p.size,
                entry_price=p.entry_price,
                stop_loss=p.stop,
                take_profit=p.take,
                entry_time=p.opened_at or datetime.now(tz=UTC),
                status=p.status,
            )
            tracker.add_position(pp)

        if closed or open_positions:
            logger.info(
                "paper: restored %d closed (pnl=%.2f) + %d open positions, equity=%.2f",
                len(closed), realized_pnl, len(open_positions), equity,
            )

        return tracker

    @property
    def tracker(self) -> PnLTracker:
        return self._tracker

    def handle_selected(self, report: DecisionReport) -> ExecutionResult:
        """Process an accepted decision: journal + optional paper position."""
        self._persist_decision(report, accepted=True)
        tf: str = str(report.features.get("timeframe", ""))
        self._repos.signals.insert(
            symbol=report.symbol,
            signal=report.signal,
            confidence=report.confidence,
            side=report.side,
            score=report.total_score,
            timeframe=tf,
            reason=report.explanation[:500] if report.explanation else "",
            ts_ms=int(report.timestamp.timestamp() * 1000),
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
        tf: str = str(report.features.get("timeframe", ""))
        record = DecisionRecord(
            timestamp=report.timestamp,
            symbol=report.symbol,
            timeframe=tf,
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

        tf: str = str(report.features.get("timeframe", ""))
        if self._repos.positions.open_exists(report.symbol, timeframe=tf):
            return ExecutionResult(
                handled=False,
                message=f"position already open for {report.symbol} {tf}",
            )

        entry = report.features.get("last_close") or report.features.get("close", 0.0)
        if entry <= 0:
            entry = report.features.get("ema_fast", 0.0)
        if entry <= 0:
            return ExecutionResult(handled=False, message="invalid entry price")

        atr_pct = report.features.get("atr_pct", self._settings.risk.max_stop_distance_pct)
        levels = self._sltp.calculate(entry, report.side, atr_pct)

        # Score-based position sizing: higher score = larger position
        score_pct = min(1.0, max(0.1, report.total_score / 100.0))
        equity = self._tracker.current_equity
        risk_amount = equity * (self._settings.risk.risk_per_trade_pct / 100.0) * score_pct
        stop_distance = abs(entry - levels.stop_loss)
        if stop_distance <= 0:
            return ExecutionResult(handled=False, message="invalid stop distance")

        size = risk_amount / stop_distance
        fee = self._fees.calculate(size, entry, is_maker=False)

        position = PaperPosition(
            symbol=report.symbol,
            timeframe=tf,
            side=report.side,
            size=size,
            entry_price=entry,
            stop_loss=levels.stop_loss,
            take_profit=levels.take_profit,
        )
        self._tracker.add_position(position)

        position_id = self._repos.positions.insert(
            symbol=report.symbol,
            timeframe=tf,
            side=report.side,
            size=size,
            entry_price=entry,
            stop=levels.stop_loss,
            take=levels.take_profit,
            mode=Mode.PAPER,
            opened_at_ms=int(report.timestamp.timestamp() * 1000),
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
                f"paper {report.side.value} {report.symbol} {tf} "
                f"entry={entry:.4f} stop={levels.stop_loss:.4f} "
                f"take={levels.take_profit:.4f} score_pct={score_pct:.2f} fee={fee.fee_abs:.4f}"
            ),
            position=position,
        )

    def check_positions(
        self, current_prices: dict[str, dict[str, float]]
    ) -> dict[str, Any]:
        """Check all open paper positions against current prices for SL/TP.

        Args:
            current_prices: {symbol: {timeframe: price}} — latest prices per tf.

        Returns:
            Dict with counts/pnl of closed positions.
        """
        stats = {
            "closed_by_sl": 0,
            "closed_by_tp": 0,
            "closed_by_sl_pnl": 0.0,
            "closed_by_tp_pnl": 0.0,
        }

        open_positions = self._repos.positions.list_open()
        open_by_key: dict[tuple[str, str, str], int] = {}
        for pos in open_positions:
            open_by_key[(pos.symbol, pos.timeframe, pos.side.value)] = pos.id

        for paper_pos in list(self._tracker.positions):
            if not paper_pos.is_open:
                continue

            price = (
                current_prices.get(paper_pos.symbol, {})
                .get(paper_pos.timeframe)
            )
            if price is None or price <= 0:
                continue

            triggered = False
            closed_by: str | None = None

            if paper_pos.check_stop_loss(price):
                exit_price = paper_pos.stop_loss
                closed_by = "stop_loss"
                triggered = True
            elif paper_pos.check_take_profit(price):
                exit_price = paper_pos.take_profit
                closed_by = "take_profit"
                triggered = True

            if triggered:
                self._tracker.close_position(paper_pos, exit_price, closed_by=closed_by)

                key = (paper_pos.symbol, paper_pos.timeframe, paper_pos.side.value)
                db_id = open_by_key.get(key)
                if db_id:
                    self._repos.positions.close(
                        position_id=db_id,
                        exit_price=exit_price,
                        pnl_pct=paper_pos.pnl_pct or 0.0,
                        closed_by=closed_by,
                    )

                pnl = paper_pos.pnl_abs or 0.0
                if closed_by == "stop_loss":
                    stats["closed_by_sl"] += 1
                    stats["closed_by_sl_pnl"] += pnl
                else:
                    stats["closed_by_tp"] += 1
                    stats["closed_by_tp_pnl"] += pnl

                logger.info(
                    "paper: %s closed by %s %s %s pnl=%.2f",
                    paper_pos.symbol, closed_by, paper_pos.timeframe,
                    paper_pos.side.value, pnl,
                )

        return stats

    def check_positions_range(
        self,
        symbol: str,
        timeframe: str,
        low: float,
        high: float,
        *,
        open_price: float | None = None,
        conflict_resolution: str = "pessimistic",
        bar_timestamp_ms: int | None = None,
    ) -> dict[str, Any]:
        """Check open positions against an OHLC bar's ``[low, high]`` range.

        This is the backtest-safe counterpart of ``check_positions``: it
        evaluates whether the stop-loss and/or take-profit of each open
        position lies *inside* the bar's full price range, not just at its
        close.

        Intrabar conflict resolution (both SL and TP inside the same bar):

        * ``"pessimistic"`` (default) — SL wins.  Conservative, never overstates
          results.
        * ``"open_proximity"`` — whichever level is closer to the bar's
          ``open_price`` triggers first.

        Args:
            symbol: The trading pair.
            timeframe: The bar's timeframe.
            low: Bar's low price.
            high: Bar's high price.
            open_price: Bar's open (needed for ``open_proximity``).
            conflict_resolution: ``"pessimistic"`` or ``"open_proximity"``.
            bar_timestamp_ms: Bar's timestamp (ms epoch) used as closed_at_ms in DB.

        Returns:
            Dict with counts/pnl of positions closed on this bar.
        """
        stats: dict[str, Any] = {
            "closed_by_sl": 0,
            "closed_by_tp": 0,
            "closed_by_sl_pnl": 0.0,
            "closed_by_tp_pnl": 0.0,
        }

        for paper_pos in list(self._tracker.positions):
            if not paper_pos.is_open:
                continue
            if paper_pos.symbol != symbol or paper_pos.timeframe != timeframe:
                continue

            in_range_sl = low <= paper_pos.stop_loss <= high
            in_range_tp = low <= paper_pos.take_profit <= high

            if not in_range_sl and not in_range_tp:
                continue

            # Resolve conflicts when both levels are inside the bar
            if in_range_sl and in_range_tp:
                if conflict_resolution == "open_proximity" and open_price is not None:
                    dist_sl = abs(paper_pos.stop_loss - open_price)
                    dist_tp = abs(paper_pos.take_profit - open_price)
                    if dist_tp < dist_sl:
                        in_range_sl = False  # TP closer to open → TP triggers first
                    else:
                        in_range_tp = False  # SL closer or equal → SL wins
                else:
                    # pessimistic: SL wins
                    in_range_tp = False

            # Close the position
            triggered = False
            if in_range_sl:
                exit_price = paper_pos.stop_loss
                closed_by = "stop_loss"
                triggered = True
            elif in_range_tp:
                exit_price = paper_pos.take_profit
                closed_by = "take_profit"
                triggered = True
            else:
                continue

            if triggered:
                self._tracker.close_position(paper_pos, exit_price, closed_by=closed_by)

                # Persist to DB
                open_positions = self._repos.positions.list_open()
                for db_pos in open_positions:
                    if (db_pos.symbol == symbol and db_pos.timeframe == timeframe
                            and db_pos.side == paper_pos.side):
                        self._repos.positions.close(
                            position_id=db_pos.id,
                            exit_price=exit_price,
                            pnl_pct=paper_pos.pnl_pct or 0.0,
                            closed_by=closed_by,
                            closed_at_ms=bar_timestamp_ms,
                        )
                        break

                pnl = paper_pos.pnl_abs or 0.0
                if closed_by == "stop_loss":
                    stats["closed_by_sl"] += 1
                    stats["closed_by_sl_pnl"] += pnl
                else:
                    stats["closed_by_tp"] += 1
                    stats["closed_by_tp_pnl"] += pnl

                logger.info(
                    "paper: %s closed by %s %s %s (intrabar range [%.4f, %.4f]) pnl=%.2f",
                    symbol, closed_by, timeframe, paper_pos.side.value,
                    low, high, pnl,
                )

        return stats
