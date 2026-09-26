"""scripts/mr_decision_rule.py computes the MR cycle 2 verdict from run databases.

The script is the literal decision rule, so it is exercised end to end: real
schema (Database runs the migrations), rows written the way the backtester
writes them, the CLI run as a subprocess.
"""
from __future__ import annotations

import datetime as dt
import math
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from crypto_bot.storage.db import Database

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "mr_decision_rule.py"
HOUR = 3_600_000
T0 = 1_764_000_000_000 - 1_764_000_000_000 % HOUR
SPAN_H = 30


def _fmt(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, dt.UTC).strftime("%Y-%m-%d %H:%M")


def _make_db(path: Path, equities: list[float], n_closed: int) -> None:
    db = Database(str(path))
    db.close()
    with sqlite3.connect(path) as con:
        con.executemany(
            "INSERT INTO equity (ts_ms, currency, equity, drawdown_pct, mode) VALUES (?, 'USDT', ?, 0.0, 'paper')",
            [(T0 + i * HOUR, e) for i, e in enumerate(equities)],
        )
        con.executemany(
            "INSERT INTO positions (symbol, timeframe, side, size, entry_price, stop, take, status, "
            "closed_by, opened_at_ms, closed_at_ms, exit_price, mode) "
            "VALUES ('BTC/USDT', '1h', 'LONG', 1.0, 100.0, 0.0, 0.0, 'closed', 'time_stop', ?, ?, 100.0, 'paper')",
            [(T0, T0 + 4 * HOUR)] * n_closed,
        )


def _path(slopes: list[float]) -> list[float]:
    """Equity over SPAN_H hours, one slope per 10-hour part, small wiggle for variance."""
    out = [10_000.0]
    for i in range(1, SPAN_H + 1):
        slope = slopes[min((i - 1) // 10, 2)]
        out.append(out[-1] * (1 + slope + 0.0005 * math.sin(i)))
    return out


def _cli(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--label", "production",
         "--validation-db", str(tmp_path / "validation.db"),
         "--test-db", str(tmp_path / "test.db"),
         "--test-start", _fmt(T0), "--test-end", _fmt(T0 + SPAN_H * HOUR)],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )


def _run(tmp_path: Path, test_slopes: list[float], n_closed: int) -> subprocess.CompletedProcess[str]:
    _make_db(tmp_path / "validation.db", _path([0.001, 0.001, 0.001]), 5)
    _make_db(tmp_path / "test.db", _path(test_slopes), n_closed)
    return _cli(tmp_path)


def test_accept_when_all_four_conditions_hold(tmp_path: Path) -> None:
    result = _run(tmp_path, [0.001, -0.0005, 0.001], n_closed=200)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ВЕРДИКТ (production): ACCEPT" in result.stdout
    assert "2 из 3" in result.stdout


def test_reject_on_199_trades(tmp_path: Path) -> None:
    result = _run(tmp_path, [0.001, -0.0005, 0.001], n_closed=199)
    assert result.returncode == 1
    assert "ВЕРДИКТ (production): REJECT" in result.stdout


def test_reject_when_only_one_subwindow_is_positive(tmp_path: Path) -> None:
    # Whole test segment still up (condition 1 holds), but two parts are down.
    result = _run(tmp_path, [0.004, -0.0005, -0.0005], n_closed=250)
    assert result.returncode == 1, result.stdout
    assert "1 из 3" in result.stdout
    assert "REJECT" in result.stdout


def test_duplicate_tick_is_refused(tmp_path: Path) -> None:
    equities = _path([0.001, 0.001, 0.001])
    _make_db(tmp_path / "validation.db", equities, 5)
    _make_db(tmp_path / "test.db", equities, 200)
    with sqlite3.connect(tmp_path / "test.db") as con:
        con.execute("INSERT INTO equity (ts_ms, currency, equity, drawdown_pct, mode) "
                    "VALUES (?, 'USDT', 10000.0, 0.0, 'paper')", (T0 + HOUR,))
    result = _cli(tmp_path)
    assert result.returncode != 0
    assert "несколькими записями" in result.stderr
