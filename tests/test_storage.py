"""Storage repository tests using a temp-file SQLite database.

These verify the migrations run cleanly and that each repository round-trips
its domain type (insert -> read back) with correct status/reason fields.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from crypto_bot.core.enums import (
    Mode,
    OrderStatus,
    OrderType,
    RejectReason,
    Side,
    Signal,
    TradeStatus,
)
from crypto_bot.core.types import Candle, DecisionRecord
from crypto_bot.storage import Database, Repositories


@pytest.fixture
def repos(tmp_path):
    db = Database(db_path=tmp_path / "test.db")
    yield Repositories(db)
    db.close()


# --------------------------------------------------------------------------- #
# Schema / migration
# --------------------------------------------------------------------------- #
def test_schema_version_is_one(repos):
    assert repos.db.schema_version() == "1"


def test_migration_is_idempotent(repos):
    # Re-running migrate via a fresh Database on the same file must not error.
    db2 = Database(db_path=repos.db.db_path)
    assert db2.schema_version() == "1"
    db2.close()


# --------------------------------------------------------------------------- #
# Candles
# --------------------------------------------------------------------------- #
def test_candle_upsert_and_fetch(repos):
    c1 = Candle(timestamp=1000, open=1, high=2, low=0.5, close=1.5, volume=10)
    c2 = Candle(timestamp=2000, open=1.5, high=3, low=1.4, close=2.5, volume=20)
    n = repos.candles.upsert_many("BTC/USDT", "15m", [c1, c2])
    assert n == 2

    fetched = repos.candles.fetch("BTC/USDT", "15m", limit=10)
    assert [c.timestamp for c in fetched] == [1000, 2000]  # ascending

    assert repos.candles.latest_ts("BTC/USDT", "15m") == 2000
    assert repos.candles.latest_ts("ETH/USDT", "15m") is None


def test_candle_upsert_is_idempotent(repos):
    c = Candle(timestamp=1000, open=1, high=2, low=0.5, close=1.5, volume=10)
    repos.candles.upsert_many("BTC/USDT", "15m", [c])
    # Second insert with same key — ON CONFLICT DO NOTHING keeps original values
    c_updated = Candle(timestamp=1000, open=1, high=2, low=0.5, close=9.9, volume=99)
    repos.candles.upsert_many("BTC/USDT", "15m", [c_updated])
    fetched = repos.candles.fetch("BTC/USDT", "15m", limit=10)
    assert len(fetched) == 1
    assert fetched[0].close == 1.5   # original value preserved
    assert fetched[0].volume == 10   # original value preserved


# --------------------------------------------------------------------------- #
# Signals
# --------------------------------------------------------------------------- #
def test_signal_insert(repos):
    sid = repos.signals.insert(
        symbol="BTC/USDT", signal=Signal.BUY, confidence=0.8, side=Side.LONG,
        score=72.5, timeframe="15m", reason="confluence",
    )
    assert sid > 0


# --------------------------------------------------------------------------- #
# Decisions
# --------------------------------------------------------------------------- #
def test_decision_insert_and_cooldown_query(repos):
    d = DecisionRecord(
        timestamp=datetime.now(tz=UTC),
        symbol="BTC/USDT",
        accepted=False,
        reason=RejectReason.IN_COOLDOWN,
        detail="recent trade",
        score=70.0,
        signal=Signal.BUY,
    )
    repos.decisions.insert(d)
    count = repos.decisions.count_recent_for_symbol("BTC/USDT", within_minutes=60)
    assert count == 1
    # Different symbol -> 0
    assert repos.decisions.count_recent_for_symbol("ETH/USDT", within_minutes=60) == 0


# --------------------------------------------------------------------------- #
# Positions
# --------------------------------------------------------------------------- #
def test_position_open_and_close(repos):
    pid = repos.positions.insert(
        symbol="BTC/USDT", side=Side.LONG, size=0.1, entry_price=50000,
        stop=49000, take=52000, mode=Mode.PAPER,
    )
    assert pid > 0
    assert repos.positions.open_exists("BTC/USDT") is True
    assert repos.positions.open_exists("ETH/USDT") is False

    opens = repos.positions.list_open()
    assert len(opens) == 1
    assert opens[0].status == TradeStatus.OPEN

    repos.positions.close(pid, exit_price=51000, pnl_pct=2.0)
    assert repos.positions.open_exists("BTC/USDT") is False


# --------------------------------------------------------------------------- #
# Trades
# --------------------------------------------------------------------------- #
def test_trade_insert_and_fetch(repos):
    pid = repos.positions.insert(
        symbol="BTC/USDT", side=Side.LONG, size=0.1, entry_price=50000,
        stop=49000, take=52000, mode=Mode.PAPER,
    )
    tid = repos.trades.insert(
        position_id=pid,
        symbol="BTC/USDT",
        side=Side.LONG,
        order_type=OrderType.MARKET,
        size=0.1,
        price=50000,
        mode=Mode.PAPER,
        status=OrderStatus.FILLED,
        external_id="paper-1",
    )
    assert tid > 0

    trades = repos.trades.list_for_position(pid)
    assert len(trades) == 1
    assert trades[0].status == OrderStatus.FILLED
    assert trades[0].external_id == "paper-1"
    assert repos.trades.latest_for_symbol("BTC/USDT", limit=1)[0].id == tid


# --------------------------------------------------------------------------- #
# Equity
# --------------------------------------------------------------------------- #
def test_equity_insert_and_latest(repos):
    repos.equity.insert(currency="USDT", equity=10000, drawdown_pct=0.0, mode=Mode.PAPER)
    repos.equity.insert(currency="USDT", equity=9800, drawdown_pct=2.0, mode=Mode.PAPER)
    latest = repos.equity.latest(Mode.PAPER)
    assert latest is not None
    assert latest[0] == 9800.0
    assert latest[1] == 2.0
    # No live equity recorded yet.
    assert repos.equity.latest(Mode.LIVE) is None
