"""Portfolio-intent execution: weight-based simulated positions.

Sister of :class:`SignalExecutor` for the portfolio decision layer: it takes
an already risk-adjusted :class:`PositionIntent` and opens a simulated
position sized by ``weight * equity / entry_price`` instead of score-based
risk sizing.  It keeps the same DB-first discipline (position written to
SQLite before the in-memory tracker is updated) and the same SL/TP and
emergency-close semantics, so backtests of both decision layers share one
equity regime.

R0.4: Integrates instrument specs for qty rounding (qtyStep) and minNotionalValue checks.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..config.env import Config
from ..core.enums import Mode, OrderType, Side, TradeStatus
from ..core.logging_setup import get_logger
from ..data.funding import FundingEvent, HistoricalFundingSource
from ..data.instruments import InstrumentCache
from ..execution.costs import CompositeCostModel, ExecutionCostModel
from ..portfolio import PositionIntent
from ..simulation.executor import _resolve_tf_tp
from ..simulation.paper_position import PaperPosition
from ..simulation.pnl import PnLTracker
from ..simulation.sl_tp import SLTPCalculator
from ..storage.db import Repositories

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PortfolioExecutionResult:
    """Outcome of executing one portfolio position intent."""

    handled: bool
    message: str
    symbol: str
    size: float = 0.0
    entry_price: float = 0.0


class PortfolioExecutor:
    """Opens and manages simulated positions from portfolio intents."""

    def __init__(
        self,
        config: Config,
        repos: Repositories,
        tracker: PnLTracker | None = None,
        cost_model: ExecutionCostModel | None = None,
        instrument_cache: InstrumentCache | None = None,
    ) -> None:
        self._config = config
        self._settings = config.settings
        self._repos = repos
        self._tracker = tracker or PnLTracker()
        self._sltp = SLTPCalculator(
            max_stop_distance_pct=self._settings.risk.max_stop_distance_pct,
        )
        self._take_profit_risk_multiple = self._settings.risk.take_profit_risk_multiple
        # Defaults to the legacy-equivalent fee + slippage model (TASK 5).
        self._costs: ExecutionCostModel = cost_model or CompositeCostModel.legacy_default()
        self._emergency_halt = False
        self._instrument_cache = instrument_cache
        # Track rejected intents for renormalization
        self._rejected_intents: list[PositionIntent] = []
        
        # Post-only execution config (MR v3).
        # entry_execution/exit_execution принадлежат блоку mean_reversion, поэтому
        # применяются только когда активна именно эта стратегия. Без этого условия
        # MR-настройка протекает в CSM/momentum/baselines и уводит их в ветку
        # post-only, которая не исполняет заявки (см. walk_forward.py:217 — там
        # такой же гейт по strategy_name).
        mr = getattr(self._settings.portfolio, 'mean_reversion', None)
        mr_is_active = (
            getattr(self._settings.portfolio, 'strategy_name', None) == 'mean_reversion_v0'
        )
        self._post_only_entry = mr_is_active and getattr(mr, 'entry_execution', None) == 'post_only'
        self._post_only_exit = mr_is_active and getattr(mr, 'exit_execution', None) == 'post_only'
        # Зарегистрированная модель исполнения mean_reversion_v0 знает только три
        # выхода: реверсия (|z| <= exit_threshold), time-stop (max_holding_bars) и
        # рыночный fallback по таймауту. ATR-стопы навешивались общим портфельным
        # слоем и перехватывали 97% выходов (47 stop_loss + 21 take_profit из 70
        # сделок на реальных данных), подменяя maker-экономику на taker: 43.8%/год
        # против 131.4%/год. Уровни продолжают храниться — колонки positions.stop
        # и positions.take объявлены NOT NULL, — но для MR не проверяются.
        self._sltp_exits_enabled = not mr_is_active
        self._post_only_entry_timeout_hours = 1
        self._post_only_exit_timeout_hours = 4
        self._pending_post_only_entries: dict[str, dict] = {}  # symbol -> {limit_price, side, timestamp, intent}

    @property
    def tracker(self) -> PnLTracker:
        return self._tracker

    @property
    def emergency_halt(self) -> bool:
        return self._emergency_halt

    @emergency_halt.setter
    def emergency_halt(self, value: bool) -> None:
        self._emergency_halt = value

    @property
    def rejected_intents(self) -> list[PositionIntent]:
        """Get intents rejected during this rebalance cycle."""
        return self._rejected_intents

    def clear_rejected_intents(self) -> None:
        """Clear rejected intents for the next rebalance cycle."""
        self._rejected_intents.clear()

    def handle_selected(self, report, *, current_prices: dict | None = None):
        """Compatibility stub for candidate-mode interface (not used in portfolio mode)."""
        pass

    def handle_rejected_many(self, reports: list) -> None:
        """Compatibility stub for candidate-mode interface (not used in portfolio mode)."""
        pass

    def open_position(
        self,
        intent: PositionIntent,
        *,
        entry_price: float,
        atr_pct: float,
        spread_pct: float = 0.0,
        timestamp_ms: int,
    ) -> PortfolioExecutionResult:
        """Open one simulated position for an accepted intent (DB first).

        Supports post-only entry: if configured, places a post-only limit order
        instead of immediate market execution.
        """
        if not isinstance(intent, PositionIntent):
            raise ValueError("intent must be a PositionIntent")
        if entry_price <= 0:
            return PortfolioExecutionResult(False, "invalid entry price", intent.symbol)
        if atr_pct <= 0:
            atr_pct = self._settings.risk.max_stop_distance_pct

        if self._emergency_halt:
            return PortfolioExecutionResult(
                False, "emergency drawdown halt active - no new positions", intent.symbol
            )

        open_positions = [p for p in self._tracker.positions if p.is_open]
        if len(open_positions) >= self._settings.risk.max_open_positions:
            return PortfolioExecutionResult(
                False,
                f"max positions reached ({len(open_positions)} >= "
                f"{self._settings.risk.max_open_positions})",
                intent.symbol,
            )

        tf = intent.timeframe or "1h"
        if self._repos.positions.open_exists(intent.symbol, timeframe=tf):
            return PortfolioExecutionResult(
                False, f"position already open for {intent.symbol} {tf}", intent.symbol
            )

        # R0.4: Fail closed for symbols without instrument specs
        if self._instrument_cache is not None and not self._instrument_cache.is_tradable_linear_perpetual(intent.symbol):
            self._rejected_intents.append(intent)
            return PortfolioExecutionResult(
                False, f"symbol {intent.symbol} not tradable or missing instrument spec", intent.symbol
            )

        # Post-only entry: place limit order at entry_price, track for future fill
        if self._post_only_entry:
            key = f"{intent.symbol}:{tf}"
            self._pending_post_only_entries[key] = {
                'intent': intent,
                'limit_price': entry_price,
                'side': intent.side,
                'size': None,  # Will be calculated on fill
                'atr_pct': atr_pct,
                'spread_pct': spread_pct,
                'timestamp_ms': timestamp_ms,
            }
            logger.info(
                "portfolio: placed post-only %s %s %s limit=%.4f",
                intent.side.value, intent.symbol, tf, entry_price
            )
            return PortfolioExecutionResult(
                True,
                f"placed post-only {intent.side.value} {intent.symbol} {tf} limit={entry_price:.4f}",
                intent.symbol,
            )

        # Immediate market execution (legacy path)
        costs = self._costs.calculate(
            1.0, entry_price, intent.side, is_maker=False, spread_pct=spread_pct
        )
        entry = costs.adjusted_price

        equity = self._tracker.current_equity
        size = intent.target_weight * equity / entry
        if size <= 0:
            return PortfolioExecutionResult(False, "invalid size", intent.symbol)

        # R0.4: Round quantity DOWN to qtyStep
        if self._instrument_cache is not None:
            size = self._instrument_cache.round_qty_down(intent.symbol, size)
            if size <= 0:
                self._rejected_intents.append(intent)
                return PortfolioExecutionResult(
                    False, f"qty rounded to zero by qtyStep for {intent.symbol}", intent.symbol
                )

            # Check minNotionalValue
            if not self._instrument_cache.check_min_notional(intent.symbol, size, entry):
                self._rejected_intents.append(intent)
                return PortfolioExecutionResult(
                    False,
                    f"notional {size * entry:.2f} below minNotionalValue for {intent.symbol}",
                    intent.symbol,
                )

        tp_multiple = _resolve_tf_tp(self._take_profit_risk_multiple, tf)
        levels = self._sltp.calculate(entry, intent.side, atr_pct, reward_risk_ratio=tp_multiple)
        entry_fee_abs = self._costs.calculate(
            size, entry, intent.side, is_maker=False, spread_pct=0.0
        ).fee_abs

        position_id = self._repos.positions.insert(
            symbol=intent.symbol,
            timeframe=tf,
            side=intent.side,
            size=size,
            entry_price=entry,
            stop=levels.stop_loss,
            take=levels.take_profit,
            mode=Mode.PAPER,
            opened_at_ms=timestamp_ms,
            status=TradeStatus.OPEN,
        )
        self._repos.trades.insert(
            position_id=position_id,
            symbol=intent.symbol,
            side=intent.side,
            order_type=OrderType.MARKET,
            size=size,
            price=entry,
            mode=Mode.PAPER,
        )

        position = PaperPosition(
            symbol=intent.symbol,
            timeframe=tf,
            side=intent.side,
            size=size,
            entry_price=entry,
            stop_loss=levels.stop_loss,
            take_profit=levels.take_profit,
            entry_time=datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC),
            entry_fee_abs=entry_fee_abs,
        )
        self._tracker.add_position(position)

        logger.info(
            "portfolio: opened %s %s %s entry=%.4f size=%.6f weight=%.4f fee=%.4f",
            intent.side.value, intent.symbol, tf, entry, size, intent.target_weight,
            entry_fee_abs,
        )
        return PortfolioExecutionResult(
            True,
            f"opened {intent.side.value} {intent.symbol} {tf}",
            intent.symbol,
            size=size,
            entry_price=entry,
        )

    @property
    def pending_post_only(self) -> tuple[tuple[str, str], ...]:
        """(symbol, timeframe) pairs that still have an unfilled post-only order.

        The backtester needs this to know which bars to feed back in, without
        reaching into the executor's private pending dicts.
        """
        keys = set(self._pending_post_only_entries) | set(
            getattr(self, "_pending_post_only_exits", {})
        )
        return tuple(
            (sym, tf) for sym, _, tf in (k.partition(":") for k in sorted(keys))
        )

    def process_post_only_entries(
        self,
        symbol: str,
        timeframe: str,
        low: float,
        high: float,
        timestamp_ms: int,
    ) -> list[PortfolioExecutionResult]:
        """Check and fill pending post-only entry orders.

        Fill logic: for LONG, fill if bar's low <= limit_price.
        For SHORT, fill if bar's high >= limit_price.
        Timeout: cancel if not filled within post_only_entry_timeout_hours.
        """
        results = []
        key = f"{symbol}:{timeframe}"
        pending = self._pending_post_only_entries.get(key)
        if not pending:
            return results

        intent = pending['intent']
        limit_price = pending['limit_price']
        side = pending['side']
        # Время размещения заявки. Раньше оно записывалось в timestamp_ms и
        # затирало параметр функции, из-за чего проверка таймаута сравнивала
        # число с самим собой и не срабатывала никогда.
        order_timestamp_ms = pending['timestamp_ms']

        # Check timeout
        timeout_ms = self._post_only_entry_timeout_hours * 3600 * 1000
        if timestamp_ms - order_timestamp_ms > timeout_ms:
            # Timeout - cancel post-only order
            logger.info(
                "portfolio: cancelled post-only %s %s %s (timeout)",
                side.value, symbol, timeframe
            )
            del self._pending_post_only_entries[key]
            return results

        # Check fill condition
        filled = side == Side.LONG and low <= limit_price or side == Side.SHORT and high >= limit_price

        if not filled:
            return results

        # open_position() выходит для post-only до проверки лимита позиций,
        # поэтому к моменту фактического исполнения книга может быть уже полна —
        # проверяем ещё раз здесь, иначе post-only обходит max_open_positions.
        open_positions = [p for p in self._tracker.positions if p.is_open]
        if len(open_positions) >= self._settings.risk.max_open_positions:
            logger.info(
                "portfolio: cancelled post-only %s %s %s — max positions reached (%d >= %d)",
                side.value, symbol, timeframe,
                len(open_positions), self._settings.risk.max_open_positions,
            )
            del self._pending_post_only_entries[key]
            return results

        # Fill the order - execute at limit price (maker)
        logger.info(
            "portfolio: filled post-only %s %s %s limit=%.4f",
            side.value, symbol, timeframe, limit_price
        )

        # Remove from pending
        del self._pending_post_only_entries[key]

        # Execute with maker fees (post-only filled)
        costs = self._costs.calculate(
            1.0, limit_price, side, is_maker=True, spread_pct=pending['spread_pct']
        )
        entry = costs.adjusted_price

        equity = self._tracker.current_equity
        size = pending['intent'].target_weight * equity / entry
        if size <= 0:
            return results

        # R0.4: Round quantity DOWN to qtyStep
        if self._instrument_cache is not None:
            size = self._instrument_cache.round_qty_down(symbol, size)
            if size <= 0:
                return results

            if not self._instrument_cache.check_min_notional(symbol, size, entry):
                return results

        intent = pending['intent']
        atr_pct = pending['atr_pct']
        tf = timeframe

        tp_multiple = _resolve_tf_tp(self._take_profit_risk_multiple, tf)
        levels = self._sltp.calculate(entry, side, atr_pct, reward_risk_ratio=tp_multiple)
        entry_fee_abs = self._costs.calculate(
            size, entry, side, is_maker=True, spread_pct=0.0
        ).fee_abs

        position_id = self._repos.positions.insert(
            symbol=symbol,
            timeframe=tf,
            side=side,
            size=size,
            entry_price=entry,
            stop=levels.stop_loss,
            take=levels.take_profit,
            mode=Mode.PAPER,
            opened_at_ms=timestamp_ms,
            status=TradeStatus.OPEN,
        )
        self._repos.trades.insert(
            position_id=position_id,
            symbol=symbol,
            side=side,
            order_type=OrderType.LIMIT,
            size=size,
            price=entry,
            mode=Mode.PAPER,
        )

        position = PaperPosition(
            symbol=symbol,
            timeframe=tf,
            side=side,
            size=size,
            entry_price=entry,
            stop_loss=levels.stop_loss,
            take_profit=levels.take_profit,
            entry_time=datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC),
            entry_fee_abs=entry_fee_abs,
        )
        self._tracker.add_position(position)

        logger.info(
            "portfolio: filled post-only %s %s %s entry=%.4f size=%.6f weight=%.4f fee=%.4f",
            side.value, symbol, tf, entry, size, intent.target_weight,
            entry_fee_abs,
        )
        results.append(PortfolioExecutionResult(
            True,
            f"filled post-only {side.value} {symbol} {tf}",
            symbol,
            size=size,
            entry_price=entry,
        ))
        return results

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
        """Close open positions whose SL/TP lies inside an OHLC bar's range.

        Не применяется к стратегиям, которые не регистрируют SL/TP как способ
        выхода (см. ``_sltp_exits_enabled``).
        """
        stats: dict[str, Any] = {
            "closed_by_sl": 0,
            "closed_by_tp": 0,
            "closed_by_sl_pnl": 0.0,
            "closed_by_tp_pnl": 0.0,
        }
        if not self._sltp_exits_enabled:
            return stats

        for paper_pos in list(self._tracker.positions):
            if not paper_pos.is_open:
                continue
            if paper_pos.symbol != symbol or paper_pos.timeframe != timeframe:
                continue

            in_range_sl = low <= paper_pos.stop_loss <= high
            in_range_tp = low <= paper_pos.take_profit <= high
            if not in_range_sl and not in_range_tp:
                continue

            if in_range_sl and in_range_tp:
                if conflict_resolution == "open_proximity" and open_price is not None:
                    dist_sl = abs(paper_pos.stop_loss - open_price)
                    dist_tp = abs(paper_pos.take_profit - open_price)
                    if dist_tp < dist_sl:
                        in_range_sl = False
                    else:
                        in_range_tp = False
                else:
                    in_range_tp = False

            if in_range_sl:
                exit_price, closed_by = paper_pos.stop_loss, "stop_loss"
            elif in_range_tp:
                exit_price, closed_by = paper_pos.take_profit, "take_profit"
            else:
                continue

            exit_fee_abs = self._costs.calculate(
                paper_pos.size, exit_price, paper_pos.side, is_maker=False, spread_pct=0.0
            ).fee_abs

            self._tracker.close_position(
                paper_pos, exit_price, closed_by=closed_by, exit_fee_abs=exit_fee_abs
            )

            for db_pos in self._repos.positions.list_open():
                if (
                    db_pos.symbol == symbol
                    and db_pos.timeframe == timeframe
                    and db_pos.side == paper_pos.side
                ):
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

        return stats

    def close_position_for_symbol(
        self,
        symbol: str,
        timeframe: str,
        *,
        reason: str = "rebalance",
        exit_price: float | None = None,
        closed_at_ms: int | None = None,
    ) -> dict[str, Any]:
        """Close the single open position for (symbol, timeframe).

        Used by the portfolio backtester on rebalance ticks to drop
        positions that are no longer part of the target book.  Mirrors
        ``close_all_positions`` (DB first, fees charged) for one position.

        If post_only_exit is configured, places a post-only limit order
        at the current price (or provided exit_price) instead of immediate
        market execution.
        """
        stats: dict[str, Any] = {"closed": 0, "closed_pnl": 0.0}
        paper_pos = next(
            (
                p for p in self._tracker.positions
                if p.is_open and p.symbol == symbol and p.timeframe == timeframe
            ),
            None,
        )
        if paper_pos is None:
            return stats

        # Post-only exit: place limit order instead of immediate market execution
        if self._post_only_exit:
            key = f"{symbol}:{timeframe}"
            limit_price = exit_price if exit_price is not None and exit_price > 0 else paper_pos.entry_price
            self._pending_post_only_exits = getattr(self, '_pending_post_only_exits', {})
            self._pending_post_only_exits[key] = {
                'paper_pos': paper_pos,
                'limit_price': limit_price,
                'side': paper_pos.side,
                'reason': reason,
                'timestamp_ms': closed_at_ms or 0,
            }
            logger.info(
                "portfolio: placed post-only exit %s %s %s limit=%.4f reason=%s",
                paper_pos.side.value, symbol, timeframe, limit_price, reason
            )
            return stats

        exit_price = exit_price if exit_price is not None and exit_price > 0 else paper_pos.entry_price

        exit_fee_abs = self._costs.calculate(
            paper_pos.size, exit_price, paper_pos.side, is_maker=False, spread_pct=0.0
        ).fee_abs
        self._tracker.close_position(
            paper_pos, exit_price, closed_by=reason, exit_fee_abs=exit_fee_abs
        )

        for db_pos in self._repos.positions.list_open():
            if (
                db_pos.symbol == symbol
                and db_pos.timeframe == timeframe
                and db_pos.side == paper_pos.side
            ):
                self._repos.positions.close(
                    position_id=db_pos.id,
                    exit_price=exit_price,
                    pnl_pct=paper_pos.pnl_pct or 0.0,
                    closed_by=reason,
                    closed_at_ms=closed_at_ms,
                )
                break

        stats["closed"] = 1
        stats["closed_pnl"] = paper_pos.pnl_abs or 0.0
        return stats

    def process_post_only_exits(
        self,
        symbol: str,
        timeframe: str,
        low: float,
        high: float,
        timestamp_ms: int,
        *,
        close: float,
    ) -> list[PortfolioExecutionResult]:
        """Check and fill pending post-only exit orders.

        Fill logic: for LONG, fill if bar's high >= limit_price.
        For SHORT, fill if bar's low <= limit_price.
        Timeout: fall back to a market exit at ``close`` (the bar's close), which
        is why the caller must supply it — closing at the entry price instead
        would force every timed-out trade to report exactly zero gross PnL.
        """
        results = []
        key = f"{symbol}:{timeframe}"
        pending_exits = getattr(self, '_pending_post_only_exits', {})
        pending = pending_exits.get(key)
        if not pending:
            return results

        paper_pos = pending['paper_pos']
        limit_price = pending['limit_price']
        side = pending['side']
        reason = pending['reason']
        order_timestamp_ms = pending['timestamp_ms']

        # Check timeout
        timeout_ms = self._post_only_exit_timeout_hours * 3600 * 1000
        if timestamp_ms - order_timestamp_ms > timeout_ms:
            # Timeout - fallback to market execution
            logger.info(
                "portfolio: post-only exit %s %s %s timeout, fallback to market",
                side.value, symbol, timeframe
            )
            del pending_exits[key]
            # Execute fallback at market price (taker). Рыночный выход исполняется
            # по цене бара, на котором обнаружен таймаут, а не по цене входа.
            fallback_fee_abs = self._costs.calculate(
                paper_pos.size, close, side, is_maker=False, spread_pct=0.0
            ).fee_abs
            self._tracker.close_position(
                paper_pos, close, closed_by="timeout_fallback", exit_fee_abs=fallback_fee_abs
            )
            for db_pos in self._repos.positions.list_open():
                if (
                    db_pos.symbol == symbol
                    and db_pos.timeframe == timeframe
                    and db_pos.side == paper_pos.side
                ):
                    self._repos.positions.close(
                        position_id=db_pos.id,
                        exit_price=close,
                        pnl_pct=paper_pos.pnl_pct or 0.0,
                        closed_by="timeout_fallback",
                        closed_at_ms=timestamp_ms,
                    )
                    break
            return [PortfolioExecutionResult(
                True,
                f"filled post-only exit {side.value} {symbol} {timeframe} (timeout fallback)",
                symbol,
                size=paper_pos.size,
                entry_price=close,
            )]

        # Check fill condition
        filled = side == Side.LONG and high >= limit_price or side == Side.SHORT and low <= limit_price

        if not filled:
            return results

        # Fill the order - execute at limit price (maker)
        logger.info(
            "portfolio: filled post-only exit %s %s %s limit=%.4f",
            side.value, symbol, timeframe, limit_price
        )

        # Remove from pending
        del pending_exits[key]

        # Execute with maker fees (post-only filled)
        exit_fee_abs = self._costs.calculate(
            paper_pos.size, limit_price, side, is_maker=True, spread_pct=0.0
        ).fee_abs
        self._tracker.close_position(
            paper_pos, limit_price, closed_by=reason, exit_fee_abs=exit_fee_abs
        )

        for db_pos in self._repos.positions.list_open():
            if (
                db_pos.symbol == symbol
                and db_pos.timeframe == timeframe
                and db_pos.side == paper_pos.side
            ):
                self._repos.positions.close(
                    position_id=db_pos.id,
                    exit_price=limit_price,
                    pnl_pct=paper_pos.pnl_pct or 0.0,
                    closed_by=reason,
                    closed_at_ms=timestamp_ms,
                )
                break

        logger.info(
            "portfolio: filled post-only exit %s %s %s exit=%.4f pnl=%.2f",
            side.value, symbol, timeframe, limit_price, paper_pos.pnl_abs or 0.0
        )
        result = PortfolioExecutionResult(
            True,
            f"filled post-only exit {side.value} {symbol} {timeframe}",
            symbol,
            size=paper_pos.size,
            entry_price=limit_price,
        )
        results.append(result)
        return results

    def close_all_positions(
        self,
        reason: str,
        current_prices: dict[str, dict[str, float]] | None = None,
        closed_at_ms: int | None = None,
    ) -> dict[str, Any]:
        """Emergency-close every open position."""
        stats: dict[str, Any] = {"closed": 0, "closed_pnl": 0.0}

        for paper_pos in list(self._tracker.positions):
            if not paper_pos.is_open:
                continue
            exit_price = paper_pos.entry_price
            if current_prices:
                exit_price = current_prices.get(paper_pos.symbol, {}).get(paper_pos.timeframe)
            if exit_price is None or exit_price <= 0:
                exit_price = paper_pos.entry_price

            exit_fee_abs = self._costs.calculate(
                paper_pos.size, exit_price, paper_pos.side, is_maker=False, spread_pct=0.0
            ).fee_abs

            self._tracker.close_position(
                paper_pos, exit_price, closed_by=reason, exit_fee_abs=exit_fee_abs
            )

            for db_pos in self._repos.positions.list_open():
                if (
                    db_pos.symbol == paper_pos.symbol
                    and db_pos.timeframe == paper_pos.timeframe
                    and db_pos.side == paper_pos.side
                ):
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
                "portfolio: EMERGENCY CLOSE %s %s @ %.4f by %s pnl=%.2f",
                paper_pos.symbol, paper_pos.timeframe, exit_price, reason, pnl,
            )

        return stats

    def accrue_funding(
        self,
        funding_source: HistoricalFundingSource,
        bar_timestamp_ms: int,
    ) -> dict[str, Any]:
        """Accrue funding for all open positions at a bar timestamp.

        Called on each bar close in backtest to apply funding payments
        for positions held across funding timestamps.

        Args:
            funding_source: HistoricalFundingSource with loaded funding events
            bar_timestamp_ms: Current bar close timestamp (ms epoch)

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

            # Filter to events that occurred since the last bar (or position open)
            # For simplicity, we check all events up to bar_timestamp_ms and
            # the tracker will handle deduplication via position state
            # In practice, we'd track last_funding_time per position
            position_opened_ms = int(paper_pos.entry_time.timestamp() * 1000)
            relevant_events = [
                e for e in events
                if e.funding_time_ms > position_opened_ms and e.funding_time_ms <= bar_timestamp_ms
            ]
            if not relevant_events:
                continue

            # Calculate funding using the composite cost model
            funding_result = self._costs.accrue_funding(
                side=paper_pos.side,
                weight=paper_pos.size / self._tracker.current_equity * paper_pos.entry_price,
                entry_price=paper_pos.entry_price,
                funding_events=relevant_events,
            )

            if funding_result.net_amount != 0:
                # Apply funding to tracker (negative net_amount = cost to P&L)
                paper_pos.apply_funding(funding_result.net_amount)
                stats["total_funding"] += funding_result.net_amount
                stats["accrued_count"] += 1
                stats["by_symbol"][symbol] = stats["by_symbol"].get(symbol, 0.0) + funding_result.net_amount

                # Persist funding payment to DB for audit trail
                self._persist_funding_payment(paper_pos, relevant_events, funding_result)

        return stats

    def _persist_funding_payment(
        self,
        paper_pos: PaperPosition,
        events: list[FundingEvent],
        funding_result,
    ) -> None:
        """Persist funding payments to funding_payments table for audit."""
        # Find the corresponding DB position
        for db_pos in self._repos.positions.list_open():
            if (
                db_pos.symbol == paper_pos.symbol
                and db_pos.timeframe == paper_pos.timeframe
                and db_pos.side == paper_pos.side
            ):
                for event in events:
                    # Calculate individual event amount
                    mark = event.mark_price if event.mark_price is not None else paper_pos.entry_price
                    notional = abs(paper_pos.size) * mark
                    if paper_pos.side.value == "LONG":
                        amount = notional * event.funding_rate
                    else:
                        amount = -notional * event.funding_rate

                    # Only persist non-zero amounts
                    if abs(amount) > 1e-10:
                        self._repos.db.conn.execute(
                            """INSERT INTO funding_payments (position_id, funding_time_ms, amount)
                               VALUES (?, ?, ?)""",
                            (db_pos.id, event.funding_time_ms, amount),
                        )
                self._repos.db.conn.commit()
                break


__all__ = ["PortfolioExecutionResult", "PortfolioExecutor"]
