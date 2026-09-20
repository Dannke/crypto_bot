"""Tests for the portfolio executor (weight-based simulated positions)."""
from __future__ import annotations

import pytest

from crypto_bot.config.env import Config, EnvConfig
from crypto_bot.config.schemas import Settings
from crypto_bot.core.enums import Mode, Side
from crypto_bot.portfolio import PositionIntent
from crypto_bot.simulation.portfolio_executor import PortfolioExecutor
from crypto_bot.storage.db import Database, Repositories


def _config(**overrides) -> Config:
    """Default config with market execution for backward compatibility with tests.
    
    MR v4 explicitly overrides to post_only in its pre-registration config.
    """
    base = Settings().model_dump()
    base.setdefault("portfolio", {}).setdefault("mean_reversion", {})
    base["portfolio"]["mean_reversion"]["entry_execution"] = "market"
    base["portfolio"]["mean_reversion"]["exit_execution"] = "market"
    settings = Settings.model_validate({**base, **overrides})
    return Config(settings=settings, env=EnvConfig())


def _config_post_only(**overrides) -> Config:
    """Config with post-only execution enabled for MR v3."""
    base = Settings().model_dump()
    base.update(overrides)
    base.setdefault("portfolio", {}).setdefault("mean_reversion", {})
    base["portfolio"]["strategy_name"] = "mean_reversion_v0"
    base["portfolio"]["mean_reversion"]["entry_execution"] = "post_only"
    base["portfolio"]["mean_reversion"]["exit_execution"] = "post_only"
    settings = Settings.model_validate({**base, **overrides})
    return Config(settings=settings, env=EnvConfig())


def _intent(
    symbol: str = "BTC/USDT",
    weight: float = 0.5,
    side: Side = Side.LONG,
    timeframe: str = "1h",
) -> PositionIntent:
    return PositionIntent(symbol=symbol, side=side, target_weight=weight, timeframe=timeframe)


@pytest.fixture()
def executor(tmp_path) -> tuple[PortfolioExecutor, Database]:
    db = Database(tmp_path / "portfolio_executor.db")
    repos = Repositories(db)
    ex = PortfolioExecutor(_config(), repos)
    yield ex, db
    db.close()


@pytest.fixture()
def executor_post_only(tmp_path) -> tuple[PortfolioExecutor, Database]:
    """Executor with post_only entry/exit execution enabled."""
    db = Database(tmp_path / "portfolio_executor_post_only.db")
    repos = Repositories(db)
    ex = PortfolioExecutor(_config_post_only(), repos)
    yield ex, db
    db.close()


class TestOpenPosition:
    def test_sizes_by_weight_times_equity(self, executor) -> None:
        ex, db = executor
        result = ex.open_position(
            _intent(weight=0.5),
            entry_price=100.0,
            atr_pct=1.0,
            timestamp_ms=1_700_000_000_000,
        )
        assert result.handled
        assert result.size == pytest.approx(50.0)
        assert result.entry_price == pytest.approx(100.0)
        assert len([p for p in ex.tracker.positions if p.is_open]) == 1

        position = db.conn.execute("SELECT * FROM positions").fetchone()
        trade = db.conn.execute("SELECT * FROM trades").fetchone()
        assert position is not None
        assert trade is not None
        assert float(position["size"]) == pytest.approx(50.0)
        assert float(position["entry_price"]) == pytest.approx(100.0)

    def test_entry_fee_recorded(self, executor) -> None:
        ex, _ = executor
        ex.open_position(
            _intent(weight=0.5),
            entry_price=100.0,
            atr_pct=1.0,
            timestamp_ms=1_700_000_000_000,
        )
        position = ex.tracker.positions[0]
        assert position.entry_fee_abs == pytest.approx(50.0 * 100.0 * 0.001)

    def test_slippage_applied_to_entry(self, executor) -> None:
        ex, _ = executor
        result = ex.open_position(
            _intent(weight=0.5),
            entry_price=100.0,
            atr_pct=1.0,
            spread_pct=0.4,
            timestamp_ms=1_700_000_000_000,
        )
        assert result.entry_price == pytest.approx(100.1)
        assert result.size == pytest.approx(0.5 * 10_000.0 / 100.1)

    def test_short_position_side_preserved(self, executor) -> None:
        ex, _ = executor
        ex.open_position(
            _intent(side=Side.SHORT),
            entry_price=100.0,
            atr_pct=1.0,
            timestamp_ms=1_700_000_000_000,
        )
        assert ex.tracker.positions[0].side == Side.SHORT

    def test_rejects_duplicate_symbol_timeframe(self, executor) -> None:
        ex, _ = executor
        ex.open_position(
            _intent(),
            entry_price=100.0,
            atr_pct=1.0,
            timestamp_ms=1_700_000_000_000,
        )
        result = ex.open_position(
            _intent(),
            entry_price=100.0,
            atr_pct=1.0,
            timestamp_ms=1_700_000_000_000,
        )
        assert not result.handled
        assert "already open" in result.message

    def test_emergency_halt_blocks(self, executor) -> None:
        ex, _ = executor
        ex.emergency_halt = True
        result = ex.open_position(
            _intent(),
            entry_price=100.0,
            atr_pct=1.0,
            timestamp_ms=1_700_000_000_000,
        )
        assert not result.handled
        assert "halt" in result.message

    def test_max_open_positions_guard(self, tmp_path) -> None:
        settings = Settings.model_validate(
            {
                **Settings().model_dump(),
                "risk": {**Settings().model_dump()["risk"], "max_open_positions": 1},
            }
        )
        settings.portfolio.mean_reversion.entry_execution = "market"
        settings.portfolio.mean_reversion.exit_execution = "market"
        config = Config(settings=settings, env=EnvConfig())
        db = Database(tmp_path / "maxpos.db")
        ex = PortfolioExecutor(config, Repositories(db))
        assert ex.open_position(
            _intent("BTC/USDT"),
            entry_price=100.0,
            atr_pct=1.0,
            timestamp_ms=1_700_000_000_000,
        ).handled
        result = ex.open_position(
            _intent("ETH/USDT"),
            entry_price=100.0,
            atr_pct=1.0,
            timestamp_ms=1_700_000_000_000,
        )
        assert not result.handled
        assert "max positions" in result.message
        db.close()

    def test_invalid_entry_price(self, executor) -> None:
        ex, _ = executor
        result = ex.open_position(
            _intent(),
            entry_price=0.0,
            atr_pct=1.0,
            timestamp_ms=1_700_000_000_000,
        )
        assert not result.handled


class TestClosing:
    def test_close_by_tp_range(self, executor) -> None:
        ex, db = executor
        ex.open_position(
            _intent(),
            entry_price=100.0,
            atr_pct=1.0,
            timestamp_ms=1_700_000_000_000,
        )
        position = ex.tracker.positions[0]
        stats = ex.check_positions_range(
            "BTC/USDT", "1h",
            low=position.stop_loss + 0.01,
            high=position.take_profit + 1.0,
            bar_timestamp_ms=1_700_003_600_000,
        )
        assert stats["closed_by_tp"] == 1
        assert len([p for p in ex.tracker.positions if p.is_open]) == 0

        row = db.conn.execute("SELECT status, closed_by, exit_price FROM positions").fetchone()
        assert row["status"] == "closed"
        assert row["closed_by"] == "take_profit"
        assert float(row["exit_price"]) == pytest.approx(position.take_profit)

    def test_pessimistic_conflict_sl_wins(self, executor) -> None:
        ex, _ = executor
        ex.open_position(
            _intent(),
            entry_price=100.0,
            atr_pct=1.0,
            timestamp_ms=1_700_000_000_000,
        )
        position = ex.tracker.positions[0]
        stats = ex.check_positions_range(
            "BTC/USDT", "1h",
            low=min(position.stop_loss, position.take_profit),
            high=max(position.stop_loss, position.take_profit),
            bar_timestamp_ms=1_700_003_600_000,
        )
        assert stats["closed_by_sl"] == 1
        assert stats["closed_by_tp"] == 0

    def test_close_all_positions_emergency(self, executor) -> None:
        ex, db = executor
        for symbol, weight in (("BTC/USDT", 0.4), ("ETH/USDT", 0.4)):
            ex.open_position(
                _intent(symbol, weight),
                entry_price=100.0,
                atr_pct=1.0,
                timestamp_ms=1_700_000_000_000,
            )
        stats = ex.close_all_positions("emergency_drawdown", closed_at_ms=1_700_003_600_000)
        assert stats["closed"] == 2
        open_count = db.conn.execute(
            "SELECT COUNT(*) AS c FROM positions WHERE status='open'"
        ).fetchone()
        assert int(open_count["c"]) == 0


class TestPostOnlyExecution:
    """Tests for post-only order execution with fill/timeout/fallback logic."""

    def test_post_only_entry_fills_when_bar_touches_limit(self, executor_post_only) -> None:
        """Post-only entry fills when bar's low <= limit (long) or high >= limit (short)."""
        ex, db = executor_post_only
        # Place post-only entry
        result = ex.open_position(
            _intent("BTC/USDT", weight=0.5, side=Side.LONG),
            entry_price=100.0,
            atr_pct=1.0,
            spread_pct=0.1,
            timestamp_ms=1_700_000_000_000,
        )
        assert result.handled
        assert "placed post-only" in result.message

        # Bar touches limit (low <= 100.0 for LONG)
        results = ex.process_post_only_entries(
            "BTC/USDT", "1h", low=99.9, high=101.0, timestamp_ms=1_700_003_600_000
        )
        assert len(results) == 1
        assert results[0].handled
        assert "filled post-only" in results[0].message
        # Fill price includes spread (maker fee + slippage)
        assert results[0].entry_price == pytest.approx(100.0, abs=0.1)

        # Position should now be open
        assert len([p for p in ex.tracker.positions if p.is_open]) == 1

    def test_halt_cancels_pending_post_only_entry(self, executor_post_only) -> None:
        """Halt обязан блокировать исполнение УЖЕ размещённых заявок.

        В цикле бэктестера _process_post_only стоит до гейта
        `if not self._emergency_halt_triggered`, поэтому заявка с прошлого бара
        иначе откроет позицию после объявления аварийной остановки.
        """
        ex, _db = executor_post_only
        result = ex.open_position(
            _intent("BTC/USDT", weight=0.5, side=Side.LONG),
            entry_price=100.0,
            atr_pct=1.0,
            timestamp_ms=1_700_000_000_000,
        )
        assert result.handled

        ex.emergency_halt = True

        # Бар касается лимита — без гейта заявка исполнилась бы.
        results = ex.process_post_only_entries(
            "BTC/USDT", "1h", low=99.9, high=101.0, timestamp_ms=1_700_003_600_000
        )
        assert results == []
        assert [p for p in ex.tracker.positions if p.is_open] == []
        assert ex.pending_post_only == (), "заявка должна быть снята, а не висеть"

    def test_close_all_positions_clears_pending_post_only(self, executor_post_only) -> None:
        """Аварийное закрытие снимает висящие заявки.

        Иначе post-only выход на следующем баре возьмёт из pending уже закрытую
        позицию и упадёт с "Position is already closed".
        """
        ex, _db = executor_post_only
        ex.open_position(
            _intent("BTC/USDT", weight=0.5, side=Side.LONG),
            entry_price=100.0,
            atr_pct=1.0,
            timestamp_ms=1_700_000_000_000,
        )
        assert ex.pending_post_only != ()

        ex.close_all_positions("emergency_drawdown", closed_at_ms=1_700_003_600_000)

        assert ex.pending_post_only == ()

    def test_post_only_entry_timeout_cancels_order(self, executor_post_only) -> None:
        """Post-only entry cancels after timeout (1h) if not filled."""
        ex, db = executor_post_only
        # Place post-only entry
        result = ex.open_position(
            _intent("BTC/USDT", weight=0.5, side=Side.LONG),
            entry_price=100.0,
            atr_pct=1.0,
            spread_pct=0.1,
            timestamp_ms=1_700_000_000_000,
        )
        assert result.handled

        # Advance time beyond 1h timeout, bar never touches limit
        results = ex.process_post_only_entries(
            "BTC/USDT", "1h", low=100.5, high=101.0, timestamp_ms=1_700_003_600_000 + 7200_000  # 2 hours later
        )
        assert len(results) == 0  # Order cancelled, no fill
        # No position should be open
        assert len([p for p in ex.tracker.positions if p.is_open]) == 0

    def test_post_only_exit_fills_when_bar_touches_target(self, executor_post_only) -> None:
        """Post-only exit fills when bar touches limit, then closes with maker fee."""
        ex, db = executor_post_only
        # First place a post-only entry and fill it
        entry_result = ex.open_position(
            _intent("BTC/USDT", weight=0.5, side=Side.LONG),
            entry_price=100.0,
            atr_pct=1.0,
            spread_pct=0.1,
            timestamp_ms=1_700_000_000_000,
        )
        assert entry_result.handled
        
        # Fill the entry
        entry_results = ex.process_post_only_entries(
            "BTC/USDT", "1h", low=99.9, high=101.0, timestamp_ms=1_700_003_600_000
        )
        assert len(entry_results) == 1
        assert entry_results[0].handled

        # Now request post-only exit at target price
        result = ex.close_position_for_symbol(
            "BTC/USDT", "1h",
            reason="rebalance",
            exit_price=101.0,  # limit price
            closed_at_ms=1_700_003_600_000,
        )
        assert result["closed"] == 0  # Not closed immediately, order placed

        # Bar touches target (high >= 101.0 for LONG exit)
        results = ex.process_post_only_exits(
            "BTC/USDT", "1h", low=100.5, high=101.5, timestamp_ms=1_700_007_200_000,
            close=101.2,
        )
        assert len(results) == 1
        assert results[0].handled
        assert "filled post-only exit" in results[0].message
        # Fill price includes spread (maker fee + slippage)
        assert results[0].entry_price == pytest.approx(101.0, abs=0.1)

        # Position should be closed
        assert len([p for p in ex.tracker.positions if p.is_open]) == 0

    def test_post_only_exit_timeout_fallback_to_market(self, executor_post_only) -> None:
        """Post-only exit times out (4h) and falls back to market order."""
        ex, db = executor_post_only
        # First place a post-only entry and fill it
        entry_result = ex.open_position(
            _intent("BTC/USDT", weight=0.5, side=Side.LONG),
            entry_price=100.0,
            atr_pct=1.0,
            timestamp_ms=1_700_000_000_000,
        )
        assert entry_result.handled
        
        # Fill the entry
        entry_results = ex.process_post_only_entries(
            "BTC/USDT", "1h", low=99.9, high=101.0, timestamp_ms=1_700_003_600_000
        )
        assert len(entry_results) == 1
        assert entry_results[0].handled

        # Request post-only exit
        result = ex.close_position_for_symbol(
            "BTC/USDT", "1h",
            reason="rebalance",
            exit_price=101.0,
            closed_at_ms=1_700_003_600_000,
        )
        assert result["closed"] == 0

        # Advance time beyond 4h timeout, bar never touches target
        results = ex.process_post_only_exits(
            "BTC/USDT", "1h", low=100.0, high=100.5, timestamp_ms=1_700_018_001_000,  # 4h + 1s later
            close=100.2,
        )
        assert len(results) == 1  # Fallback executed
        assert results[0].handled

        # Position should be closed via market fallback
        assert len([p for p in ex.tracker.positions if p.is_open]) == 0

    def test_post_only_short_entry_fills_on_high(self, executor_post_only) -> None:
        """Post-only SHORT entry fills when bar's high >= limit."""
        ex, db = executor_post_only
        result = ex.open_position(
            _intent("BTC/USDT", weight=0.5, side=Side.SHORT),
            entry_price=100.0,
            atr_pct=1.0,
            spread_pct=0.1,
            timestamp_ms=1_700_000_000_000,
        )
        assert result.handled

        # Bar touches limit (high >= 100.0 for SHORT)
        results = ex.process_post_only_entries(
            "BTC/USDT", "1h", low=99.0, high=100.5, timestamp_ms=1_700_003_600_000
        )
        assert len(results) == 1
        assert results[0].handled
        # Fill price includes maker fee (2 bps) + slippage (1 bps) = 3 bps total
        # For SHORT: fill_price = limit * (1 - 3bps) = 100.0 * 0.9997 = 99.97
        assert results[0].entry_price == pytest.approx(100.0, abs=0.05)
        assert ex.tracker.positions[0].side == Side.SHORT
