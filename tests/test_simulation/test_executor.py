"""Tests for signal executor."""
from __future__ import annotations

from datetime import UTC, datetime

from crypto_bot.config.env import Config
from crypto_bot.config.schemas import Settings
from crypto_bot.core.enums import Mode, Side, Signal
from crypto_bot.decision.decision_report import DecisionReport
from crypto_bot.simulation.executor import SignalExecutor
from crypto_bot.storage.db import Database, Repositories


def _config(mode: Mode) -> Config:
    settings = Settings.model_validate(
        {**Settings().model_dump(), "runtime": {"mode": mode.value, "loop_interval_seconds": 60, "timezone": "UTC"}}
    )
    from crypto_bot.config.env import EnvConfig

    return Config(settings=settings, env=EnvConfig())


def test_signal_only_logs_without_opening_position(tmp_path):
    db = Database(tmp_path / "test.db")
    repos = Repositories(db)
    executor = SignalExecutor(_config(Mode.SIGNAL_ONLY), repos)
    report = DecisionReport(
        symbol="BTC/USDT",
        signal=Signal.BUY,
        side=Side.LONG,
        confidence=0.9,
        total_score=80.0,
        features={"last_close": 100.0, "atr_pct": 1.5},
        explanation="test",
        timestamp=datetime.now(tz=UTC),
    )
    result = executor.handle_selected(report)
    assert result.handled is True
    assert result.position is None
    row = db.conn.execute("SELECT COUNT(*) AS c FROM signals").fetchone()
    assert int(row["c"]) == 1
    db.close()
