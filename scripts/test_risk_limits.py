"""Quick verification of the three risk-limit features in executor.py."""
from __future__ import annotations

import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from crypto_bot.config.env import Config
from crypto_bot.config.settings import load_settings
from crypto_bot.core.enums import Mode, Side, Signal
from crypto_bot.decision.decision_report import DecisionReport
from crypto_bot.simulation.executor import SignalExecutor
from crypto_bot.simulation.paper_position import PaperPosition
from crypto_bot.simulation.pnl import PnLTracker
from crypto_bot.storage.db import Database, Repositories


def _report(symbol="BTC/USDT", side=Side.LONG, tf="4h", score=75):
    return DecisionReport(
        timestamp=datetime.now(tz=UTC),
        symbol=symbol,
        signal=Signal.BUY if side == Side.LONG else Signal.SELL,
        side=side,
        features={"timeframe": tf, "last_close": 80000, "atr_pct": 2.0},
        total_score=score,
        confidence=0.7,
        explanation="test",
    )


def _executor(db, cfg, tracker=None):
    repos = Repositories(db)
    cfg.settings.runtime.mode = Mode.PAPER
    if tracker is None:
        tracker = PnLTracker(initial_equity=10000, current_equity=10000, peak_equity=10000)
    return SignalExecutor(cfg, repos, tracker)


def test_max_open_positions():
    print("=== max_open_positions ===")
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    try:
        db = Database(tmp.name)
        cfg = load_settings(yaml_path=Path("config/settings.yaml"))
        cfg.settings.risk.max_open_positions = 2
        ex = _executor(db, cfg)

        pos = PaperPosition(symbol="BTC/USDT", timeframe="4h", side=Side.LONG,
                            size=0.1, entry_price=80000, stop_loss=78000, take_profit=84000)
        ex.tracker.add_position(pos)

        r1 = ex.handle_selected(_report("ETH/USDT", Side.LONG))
        print(f"  1st ETH/USDT: handled={r1.handled}")  # should be True

        r2 = ex.handle_selected(_report("SOL/USDT", Side.LONG))
        print(f"  2nd SOL/USDT: handled={r2.handled}, msg={r2.message}")  # should be False (max=2)
    finally:
        db.close()
        os.unlink(tmp.name)


def test_emergency_drawdown():
    print("\n=== emergency_drawdown_pct ===")
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    try:
        db = Database(tmp.name)
        cfg = load_settings(yaml_path=Path("config/settings.yaml"))
        cfg.settings.risk.emergency_drawdown_pct = 5.0
        tracker = PnLTracker(initial_equity=10000, current_equity=9400, peak_equity=10000)
        ex = _executor(db, cfg, tracker)

        r = ex.handle_selected(_report())
        print(f"  handled={r.handled}, msg={r.message}")  # drawdown 6% >= 5%

        row = db.conn.execute(
            "SELECT accepted, reject_reason, detail FROM decisions ORDER BY ts_ms DESC LIMIT 1"
        ).fetchone()
        print(f"  journal: accepted={row[0]}, reject_reason={row[1]}")
    finally:
        db.close()
        os.unlink(tmp.name)


def test_daily_drawdown():
    print("\n=== max_daily_drawdown_pct ===")
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    try:
        db = Database(tmp.name)
        cfg = load_settings(yaml_path=Path("config/settings.yaml"))
        cfg.settings.risk.max_daily_drawdown_pct = 3.0
        tracker = PnLTracker(initial_equity=10000, current_equity=9600, peak_equity=10000)
        ex = _executor(db, cfg, tracker)

        r = ex.handle_selected(_report())
        print(f"  handled={r.handled}, msg={r.message}")  # 4% > 3% daily limit

        row = db.conn.execute(
            "SELECT accepted, reject_reason FROM decisions ORDER BY ts_ms DESC LIMIT 1"
        ).fetchone()
        print(f"  journal: accepted={row[0]}, reject_reason={row[1]}")
    finally:
        db.close()
        os.unlink(tmp.name)


def test_side_awareness():
    print("\n=== side-awareness ===")
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    try:
        db = Database(tmp.name)
        # Insert LONG BTC/USDT into DB so open_exists finds it
        ts = int(datetime.now(tz=UTC).timestamp() * 1000)
        db.conn.execute(
            "INSERT INTO positions "
            "(symbol,timeframe,side,size,entry_price,stop,take,status,mode,opened_at_ms) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("BTC/USDT", "4h", "LONG", 0.1, 80000, 78000, 84000, "open", "paper", ts),
        )
        db.conn.commit()

        cfg = load_settings(yaml_path=Path("config/settings.yaml"))
        cfg.settings.risk.max_open_positions = 5
        ex = _executor(db, cfg)

        r_long = ex.handle_selected(_report("BTC/USDT", Side.LONG))
        print(f"  2nd LONG same sym/TF: handled={r_long.handled}")  # False (same side)

        r_short = ex.handle_selected(_report("BTC/USDT", Side.SHORT))
        print(f"  SHORT same sym/TF:   handled={r_short.handled}")  # True (opposite side)
    finally:
        db.close()
        os.unlink(tmp.name)


if __name__ == "__main__":
    test_max_open_positions()
    test_emergency_drawdown()
    test_daily_drawdown()
    test_side_awareness()
    print("\nALL PASSED")
