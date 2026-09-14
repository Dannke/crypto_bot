"""Signal execution for signal_only and paper trading modes.

Stage 3 scope: compute virtual positions and journal decisions.
No real exchange orders are placed.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..config.env import Config
from ..core.enums import ExecutorOutcome, Mode, OrderType, RejectReason, Side, Signal, TradeStatus
from ..core.logging_setup import get_logger
from ..core.types import DecisionRecord
from ..data.funding import HistoricalFundingSource
from ..decision.decision_report import DecisionReport
from ..execution.costs import ExecutionCostModel
from ..simulation.fees import FeeCalculator
from ..simulation.paper_position import PaperPosition
from ..simulation.pnl import PnLTracker
from ..simulation.sl_tp import SLTPCalculator
from ..storage.db import Repositories

_RESOLVE_TF_FLOAT_DEFAULT = 2.0


def _resolve_tf_tp(val: float | dict[str, float], tf: str) -> float:
    """Resolve per-TF take_profit_risk_multiple, matching ATR pattern."""
    if isinstance(val, dict):
        return val.get(tf, _RESOLVE_TF_FLOAT_DEFAULT)
    return val


logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """Outcome of handling a selected decision."""

    handled: bool
    message: str
    position: PaperPosition | None = None
    outcome: ExecutorOutcome | None = None


class SignalExecutor:
    """Handles selected decisions according to runtime mode.

    Single source of truth: all position mutations go to the DB first,
    then the in-memory tracker is refreshed from the DB. This guarantees
    the DB and tracker stay consistent even after restart.
    """

    # Slippage factor: entry price adjusts by this fraction of half-spread
    _SLIPPAGE_FACTOR: float = 0.5

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
        )
        self._take_profit_risk_multiple = self._settings.risk.take_profit_risk_multiple
        self._fees = FeeCalculator()
        # Emergency drawdown halt flag (sticky - set by backtester when 6% DD hit)
        self._emergency_halt = False

    def _restore_tracker(self, symbol: str | None = None) -> PnLTracker:
        """Restore PnLTracker from DB on restart (open + closed positions).

        DB is the single source of truth — the tracker is rebuilt from it.
        Entry fees are recalculated from the current fee schedule since they
        are not persisted to the DB schema.
        """
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
            entry_fee = self._fees.calculate(p.size, p.entry_price, is_maker=False).fee_abs
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
                entry_fee_abs=entry_fee,
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

    @property
    def emergency_halt(self) -> bool:
        return self._emergency_halt

    @emergency_halt.setter
    def emergency_halt(self, value: bool) -> None:
        self._emergency_halt = value

    def handle_selected(
        self,
        report: DecisionReport,
        *,
        current_prices: dict[tuple[str, str], float] | None = None,
    ) -> ExecutionResult:
        """Process an accepted decision: journal + optional paper position."""
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
            self._persist_decision(report, accepted=True, outcome="no_position")
            return ExecutionResult(
                handled=True,
                message=f"signal logged: {report.signal.value} {report.symbol}",
            )

        if mode == Mode.PAPER:
            result = self._open_paper_position(report, current_prices=current_prices)
            # Determine outcome: prefer explicit enum from gate, fall back to
            # legacy string matching for older ExecutionResult call-sites.
            if result.outcome is not None:
                outcome = result.outcome.value
            elif result.handled:
                outcome = ExecutorOutcome.POSITION_OPENED.value
            else:
                msg = result.message.lower()
                if "slot_taken" in msg or "position already open" in msg:
                    outcome = ExecutorOutcome.SLOT_TAKEN.value
                elif "drawdown" in msg:
                    outcome = ExecutorOutcome.DRAWDOWN_HALT.value
                elif "max open positions" in msg or "max positions reached" in msg:
                    outcome = ExecutorOutcome.MAX_POSITIONS_REACHED.value
                else:
                    outcome = ExecutorOutcome.NO_POSITION.value
            self._persist_decision(report, accepted=result.handled, outcome=outcome)
            return result

        return ExecutionResult(
            handled=False,
            message="live execution is disabled until a later stage",
        )

    def handle_rejected(self, report: DecisionReport) -> None:
        """Journal a rejected candidate."""
        # Rejected by pipeline — no executor outcome
        self._persist_decision(report, accepted=False, outcome="no_position")

    def handle_rejected_many(self, reports: list) -> None:
        """Journal many rejected candidates with a single DB transaction."""
        if not reports:
            return
        records: list = []
        for report in reports:
            reason = report.reject_reason or RejectReason.LOW_SCORE
            detail = report.explanation or (report.rejected_by or "")
            tf: str = str(report.features.get("timeframe", ""))
            records.append(
                DecisionRecord(
                    timestamp=report.timestamp,
                    symbol=report.symbol,
                    timeframe=tf,
                    accepted=False,
                    reason=reason,
                    detail=detail[:1000],
                    score=report.total_score if report.total_score > 0 else None,
                    signal=report.signal if report.signal != Signal.HOLD else None,
                    outcome="no_position",
                )
            )
        self._repos.decisions.insert_many(records)

    def _persist_decision(
        self,
        report: DecisionReport,
        *,
        accepted: bool,
        outcome: str = "no_position",
    ) -> None:
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
            outcome=outcome,
        )
        self._repos.decisions.insert(record)

    @staticmethod
    def _apply_slippage(price: float, side, spread_pct: float, factor: float) -> float:
        """Adjust entry/exit price by a fraction of half the spread to model slippage.

        For LONG market entry: price moves up (worse for buyer).
        For SHORT market entry: price moves down (worse for seller).
        """
        slippage = spread_pct / 100.0 * 0.5 * factor
        if side.value == "LONG":
            return price * (1.0 + slippage)
        return price * (1.0 - slippage)

    def _open_paper_position(
        self,
        report: DecisionReport,
        *,
        current_prices: dict[tuple[str, str], float] | None = None,
    ) -> ExecutionResult:
        if report.side is None:
            return ExecutionResult(handled=False, message="missing side")
        tf: str = str(report.features.get("timeframe", ""))

        # Emergency drawdown halt - no new positions allowed
        if self._emergency_halt:
            return ExecutionResult(
                handled=False,
                message="emergency drawdown halt active - no new positions"
            )

        # Unrealized P&L drawdown gate — block new positions if open losses exceed threshold.
        # If caller did not supply current_prices (live mode), fall back to latest
        # candle close from the DB for each open position.
        threshold = self._settings.risk.max_open_unrealized_drawdown_pct
        if threshold > 0:
            if current_prices is None:
                current_prices = {}
                for pos in self._tracker.positions:
                    if not pos.is_open:
                        continue
                    close = self._repos.candles.latest_close(pos.symbol, pos.timeframe)
                    if close is not None:
                        current_prices[(pos.symbol, pos.timeframe)] = close
            if current_prices:
                open_upl = self._tracker.unrealized_pnl(current_prices)
                open_upl_pct = open_upl / self._tracker.current_equity * 100.0 if self._tracker.current_equity > 0 else 0.0
                if open_upl_pct < -abs(threshold):
                    return ExecutionResult(
                        handled=False,
                        message=(
                            f"open unrealized drawdown {open_upl_pct:.1f}% "
                            f"exceeds threshold {threshold:.1f}%"
                        ),
                        outcome=ExecutorOutcome.OPEN_UNREALIZED_DRAWDOWN,
                    )

        open_positions = [p for p in self._tracker.positions if p.is_open]
        if len(open_positions) >= self._settings.risk.max_open_positions:
            return ExecutionResult(
                handled=False,
                message=(
                    f"max positions reached ({len(open_positions)} >= "
                    f"{self._settings.risk.max_open_positions})"
                ),
            )

        if self._repos.positions.open_exists(report.symbol, timeframe=tf):
            return ExecutionResult(
                handled=False,
                message=f"position already open for {report.symbol} {tf}",
            )

        # Apply slippage to entry price using spread_pct from feature context
        raw_entry = report.features.get("last_close") or report.features.get("close", 0.0)
        if raw_entry <= 0:
            raw_entry = report.features.get("ema_fast", 0.0)
        if raw_entry <= 0:
            return ExecutionResult(handled=False, message="invalid entry price")

        spread_pct = report.features.get("spread_pct", 0.0)
        entry = self._apply_slippage(raw_entry, report.side, spread_pct, self._SLIPPAGE_FACTOR)

        atr_pct = report.features.get("atr_pct", self._settings.risk.max_stop_distance_pct)
        tp_multiple = _resolve_tf_tp(self._take_profit_risk_multiple, tf)
        levels = self._sltp.calculate(entry, report.side, atr_pct, reward_risk_ratio=tp_multiple)

        # Score-based position sizing: higher score = larger position
        score_pct = min(1.0, max(0.1, report.total_score / 100.0))
        equity = self._tracker.current_equity
        risk_amount = equity * (self._settings.risk.risk_per_trade_pct / 100.0) * score_pct
        stop_distance = abs(entry - levels.stop_loss)
        if stop_distance <= 0:
            return ExecutionResult(handled=False, message="invalid stop distance")

        size = risk_amount / stop_distance
        entry_fee = self._fees.calculate(size, entry, is_maker=False)
        entry_fee_abs = entry_fee.fee_abs

        # DB first — single source of truth on restart
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

        # Then update in-memory tracker (live cache)
        position = PaperPosition(
            symbol=report.symbol,
            timeframe=tf,
            side=report.side,
            size=size,
            entry_price=entry,
            stop_loss=levels.stop_loss,
            take_profit=levels.take_profit,
            entry_fee_abs=entry_fee_abs,
        )
        self._tracker.add_position(position)

        return ExecutionResult(
            handled=True,
            message=(
                f"paper {report.side.value} {report.symbol} {tf} "
                f"entry={entry:.4f} (raw={raw_entry:.4f} spread={spread_pct:.2f}%) "
                f"stop={levels.stop_loss:.4f} take={levels.take_profit:.4f} "
                f"score_pct={score_pct:.2f} fee={entry_fee_abs:.4f}"
            ),
        )

    def check_positions(
        self, current_prices: dict[str, dict[str, float]]
    ) -> dict[str, Any]:
        """Check all open paper positions against current prices for SL/TP.

        DB-first closing: positions are closed in the database first,
        then the tracker is rebuilt for consistency.

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

            if paper_pos.check_stop_loss(price):
                exit_price = paper_pos.stop_loss
                closed_by = "stop_loss"
            elif paper_pos.check_take_profit(price):
                exit_price = paper_pos.take_profit
                closed_by = "take_profit"
            else:
                continue

            # Close tracker first (with fees), then persist fee-adjusted P&L to DB
            exit_fee = self._fees.calculate(paper_pos.size, exit_price, is_maker=False)
            exit_fee_abs = exit_fee.fee_abs

            self._tracker.close_position(paper_pos, exit_price, closed_by=closed_by,
                                         exit_fee_abs=exit_fee_abs)

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
                "paper: %s closed by %s %s %s pnl=%.2f (fee=%.4f)",
                paper_pos.symbol, closed_by, paper_pos.timeframe,
                paper_pos.side.value, pnl, exit_fee_abs,
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

            if in_range_sl:
                exit_price = paper_pos.stop_loss
                closed_by = "stop_loss"
            elif in_range_tp:
                exit_price = paper_pos.take_profit
                closed_by = "take_profit"
            else:
                continue

            # Close tracker first (with fees), then persist fee-adjusted P&L to DB
            exit_fee = self._fees.calculate(paper_pos.size, exit_price, is_maker=False)
            exit_fee_abs = exit_fee.fee_abs

            self._tracker.close_position(paper_pos, exit_price, closed_by=closed_by,
                                         exit_fee_abs=exit_fee_abs)

            # Persist to DB with the fee-adjusted P&L from the tracker
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
                "paper: %s closed by %s %s %s (intrabar range [%.4f, %.4f]) pnl=%.2f (fee=%.4f)",
                symbol, closed_by, timeframe, paper_pos.side.value,
                low, high, pnl, exit_fee_abs,
            )

        return stats

    def close_all_positions(
        self,
        reason: str,
        current_prices: dict[str, dict[str, float]] | None = None,
        closed_at_ms: int | None = None,
    ) -> dict[str, Any]:
        """Emergency close all open positions (e.g., on drawdown halt).

        Args:
            reason: Reason for emergency close (logged).
            current_prices: {symbol: {timeframe: price}} — latest prices per tf.
                If not provided, uses entry_price as fallback.
            closed_at_ms: Simulation timestamp for DB bookkeeping.  When None
                (live mode) the repo falls back to wall-clock time.

        Returns stats dict with counts and P&L.
        """
        stats = {
            "closed": 0,
            "closed_pnl": 0.0,
        }

        for paper_pos in list(self._tracker.positions):
            if not paper_pos.is_open:
                continue

            # Determine exit price
            exit_price = paper_pos.entry_price
            if current_prices:
                exit_price = current_prices.get(paper_pos.symbol, {}).get(paper_pos.timeframe)
            if exit_price is None or exit_price <= 0:
                exit_price = paper_pos.entry_price  # fallback

            exit_fee = self._fees.calculate(paper_pos.size, exit_price, is_maker=False)
            exit_fee_abs = exit_fee.fee_abs

            self._tracker.close_position(paper_pos, exit_price,
                                         closed_by=reason, exit_fee_abs=exit_fee_abs)

            # Persist to DB
            open_positions = self._repos.positions.list_open()
            for db_pos in open_positions:
                if (db_pos.symbol == paper_pos.symbol
                    and db_pos.timeframe == paper_pos.timeframe
                    and db_pos.side == paper_pos.side):
                    self._repos.positions.close(
                        position_id=db_pos.id,
                        exit_price=exit_price,
                        pnl_pct=paper_pos.pnl_pct or 0.0,
                        closed_by=reason,
                        closed_at_ms=closed_at_ms,
                    )
                    break

            pnl = paper_pos.pnl_abs or 0.0
            stats["closed"] += 1
            stats["closed_pnl"] += pnl
            logger.warning(
                "paper: EMERGENCY CLOSE %s %s %s @ %.4f by %s pnl=%.2f",
                paper_pos.symbol, paper_pos.timeframe, paper_pos.side.value,
                exit_price, reason, pnl,
            )

        return stats

    def accrue_funding(
        self,
        funding_source: HistoricalFundingSource,
        bar_timestamp_ms: int,
        cost_model: ExecutionCostModel | None = None,
    ) -> dict[str, Any]:
        """Accrue funding for all open positions at a bar timestamp.

        Called on each bar close in backtest to apply funding payments
        for positions held across funding timestamps.

        Args:
            funding_source: HistoricalFundingSource with loaded funding events
            bar_timestamp_ms: Current bar close timestamp (ms epoch)
            cost_model: Optional cost model with funding support (defaults to legacy)

        Returns:
            Dict with funding stats per symbol and total.
        """
        stats: dict[str, Any] = {
            "accrued_count": 0,
            "total_funding": 0.0,
            "by_symbol": {},
        }

        for paper_pos in list(self._tracker.positions):
            if not paper_pos.is_open:
                continue

            symbol = paper_pos.symbol
            events = funding_source.events_up_to(bar_timestamp_ms, symbol)
            if not events:
                continue

            # Filter to events that occurred since position open
            position_opened_ms = int(paper_pos.entry_time.timestamp() * 1000)
            relevant_events = [
                e for e in events
                if e.funding_time_ms > position_opened_ms and e.funding_time_ms <= bar_timestamp_ms
            ]
            if not relevant_events:
                continue

            # Use provided cost model or fall back to simple funding accrual
            if cost_model and hasattr(cost_model, 'accrue_funding'):
                funding_result = cost_model.accrue_funding(
                    side=paper_pos.side,
                    weight=paper_pos.size / self._tracker.current_equity * paper_pos.entry_price,
                    entry_price=paper_pos.entry_price,
                    funding_events=relevant_events,
                )
                funding_amount = funding_result.net_amount
            else:
                # Simple funding accrual using legacy fee calculator
                notional = paper_pos.size * paper_pos.entry_price
                funding_amount = 0.0
                for event in relevant_events:
                    if paper_pos.side == Side.LONG:
                        funding_amount += notional * event.funding_rate
                    else:
                        funding_amount -= notional * event.funding_rate
                funding_amount = -funding_amount  # negative = cost to P&L

            if funding_amount != 0:
                paper_pos.apply_funding(funding_amount)
                stats["total_funding"] += funding_amount
                stats["accrued_count"] += 1
                stats["by_symbol"][symbol] = stats["by_symbol"].get(symbol, 0.0) + funding_amount

        return stats
