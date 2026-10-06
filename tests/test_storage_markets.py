"""Рынок как атрибут инструмента — схема v12 (план funding/basis, Task 3).

- `positions` и `trades`: market ('linear' | 'spot'), всё прежнее — 'linear';
  `positions.leg_group`; одна открытая позиция на (symbol, timeframe, market) — в БД.
- `candles`: market в первичном ключе, без значения по умолчанию; запись без рынка падает,
  чтение без рынка — только при единственном ряде.
- Свечи до v12 — замороженные рыночные данные (поправка 2 Task 0' funding/basis): БД с ними
  не мигрирует, а открывается только на чтение; v12 пересоздаёт лишь пустую таблицу.
"""
from __future__ import annotations

import hashlib
import sqlite3

import pytest

from crypto_bot.core.enums import Mode, Side, TradeStatus
from crypto_bot.core.exceptions import StorageError
from crypto_bot.core.types import Candle
from crypto_bot.storage.db import Database, Repositories

T0 = 1_735_689_600_000  # 2025-01-01 00:00 UTC
HOUR = 3_600_000


def _bars(price: float, n: int = 3) -> list[Candle]:
    return [
        Candle(timestamp=T0 + i * HOUR, open=price, high=price, low=price, close=price, volume=1.0)
        for i in range(n)
    ]


def _open_position(repos: Repositories, symbol: str = "BTC/USDT", market: str = "linear") -> int:
    return repos.positions.insert(
        symbol=symbol, timeframe="1h", side=Side.SHORT, size=1.0, entry_price=100.0, stop=110.0,
        take=90.0, mode=Mode.PAPER, opened_at_ms=T0, status=TradeStatus.OPEN, market=market,
    )


def _v11_database(path, *, candles: int) -> None:
    """БД, какой её оставляет код до v12: свечи без рынка, позиции и сделки без market."""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO schema_meta (key, value) VALUES ('schema_version', '11');
        CREATE TABLE candles (
            symbol TEXT NOT NULL, timeframe TEXT NOT NULL, ts_ms INTEGER NOT NULL,
            open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
            volume REAL NOT NULL, PRIMARY KEY (symbol, timeframe, ts_ms)
        );
        CREATE TABLE positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, timeframe TEXT NOT NULL,
            side TEXT NOT NULL CHECK (side IN ('LONG','SHORT')), size REAL NOT NULL,
            entry_price REAL NOT NULL, stop REAL NOT NULL, take REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'open'
              CHECK (status IN ('proposed','open','closed','rejected','cancelled')),
            closed_by TEXT CHECK (closed_by IS NULL OR closed_by IN ('stop_loss','take_profit','manual',
              'signal','emergency_drawdown','rebalance','timeout_fallback','reversion','time_stop')),
            opened_at_ms INTEGER NOT NULL, closed_at_ms INTEGER, exit_price REAL, pnl_pct REAL,
            mode TEXT NOT NULL CHECK (mode IN ('signal_only','paper','live')),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        );
        INSERT INTO positions (symbol, timeframe, side, size, entry_price, stop, take, status,
                               opened_at_ms, mode)
        VALUES ('BTC/USDT', '1h', 'SHORT', 1.0, 100.0, 110.0, 90.0, 'open', 1735689600000, 'paper');
    """)
    conn.executemany(
        "INSERT INTO candles (symbol, timeframe, ts_ms, open, high, low, close, volume) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [("BTC/USDT", "1h", T0 + i * HOUR, 100.0, 100.0, 100.0, 100.0, 1.0) for i in range(candles)],
    )
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------- позиции и сделки


def test_positions_and_trades_default_to_linear(tmp_path) -> None:
    db = Database(tmp_path / "run.db")
    repos = Repositories(db)
    position_id = repos.positions.insert(
        symbol="BTC/USDT", timeframe="1h", side=Side.SHORT, size=1.0, entry_price=100.0,
        stop=110.0, take=90.0, mode=Mode.PAPER, opened_at_ms=T0, status=TradeStatus.OPEN,
    )
    repos.trades.insert(
        position_id=position_id, symbol="BTC/USDT", side=Side.SHORT, order_type=__import__(
            "crypto_bot.core.enums", fromlist=["OrderType"]).OrderType.MARKET,
        size=1.0, price=100.0, mode=Mode.PAPER,
    )

    position = repos.positions.list_open()[0]
    assert (position.market, position.leg_group) == ("linear", None)
    assert repos.trades.list_for_position(position_id)[0].market == "linear"
    with pytest.raises(sqlite3.IntegrityError):  # CHECK: только 'linear' и 'spot'
        db.conn.execute("UPDATE positions SET market='perp' WHERE id=?", (position_id,))
    db.close()


def test_one_open_position_per_instrument_and_market(tmp_path) -> None:
    db = Database(tmp_path / "run.db")
    repos = Repositories(db)
    _open_position(repos, market="linear")
    _open_position(repos, market="spot")  # нога другого рынка того же символа — можно

    with pytest.raises(sqlite3.IntegrityError):
        _open_position(repos, market="linear")
    db.conn.execute("UPDATE positions SET status='closed' WHERE market='linear'")
    _open_position(repos, market="linear")  # закрытая не мешает новой
    db.close()


# --------------------------------------------------------------------------- свечи


def test_candle_write_always_names_the_market(tmp_path) -> None:
    db = Database(tmp_path / "data.db")
    repos = Repositories(db)

    with pytest.raises(TypeError):
        repos.candles.upsert_many("BTC/USDT", "1h", _bars(100.0))  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        repos.candles.upsert_many("BTC/USDT", "1h", _bars(100.0), market="perp")
    with pytest.raises(sqlite3.IntegrityError):  # код до v12 пишет без столбца — громко падает
        db.conn.execute(
            "INSERT INTO candles (symbol, timeframe, ts_ms, open, high, low, close, volume) "
            "VALUES ('BTC/USDT', '1h', 0, 1, 1, 1, 1, 1)"
        )
    db.close()


def test_two_markets_of_one_pair_are_never_read_mixed(tmp_path) -> None:
    db = Database(tmp_path / "data.db")
    repos = Repositories(db)
    repos.candles.upsert_many("BTC/USDT", "1h", _bars(100.0), market="spot")
    assert [c.close for c in repos.candles.fetch_since("BTC/USDT", "1h", 0)] == [100.0] * 3  # один ряд

    repos.candles.upsert_many("BTC/USDT", "1h", _bars(101.0), market="linear")  # те же метки времени

    with pytest.raises(StorageError, match="name the market"):
        repos.candles.fetch_since("BTC/USDT", "1h", 0)
    assert [c.close for c in repos.candles.fetch_since("BTC/USDT", "1h", 0, market="linear")] == [101.0] * 3
    assert repos.candles.latest_close("BTC/USDT", "1h", market="spot") == 100.0
    db.close()


# --------------------------------------------------------------------------- замороженные данные и миграция


def test_candles_before_v12_are_not_migrated(tmp_path) -> None:
    path = tmp_path / "frozen.db"
    _v11_database(path, candles=5)
    before = hashlib.sha256(path.read_bytes()).hexdigest()

    with pytest.raises(StorageError, match="read_only=True"):
        Database(path)

    assert hashlib.sha256(path.read_bytes()).hexdigest() == before  # ни байта не записано


def test_frozen_database_is_read_as_it_is(tmp_path) -> None:
    path = tmp_path / "frozen.db"
    _v11_database(path, candles=5)
    before = hashlib.sha256(path.read_bytes()).hexdigest()

    db = Database(path, read_only=True)
    repos = Repositories(db)
    assert db.schema_version() == "11"
    assert len(repos.candles.fetch_since("BTC/USDT", "1h", 0)) == 5  # единственный ряд, без рынка
    with pytest.raises(StorageError):
        repos.candles.fetch_since("BTC/USDT", "1h", 0, market="spot")  # до v12 рынка нет
    with pytest.raises(sqlite3.OperationalError):
        db.conn.execute("DELETE FROM candles")
    db.close()

    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_v11_database_without_candles_reaches_v12(tmp_path) -> None:
    path = tmp_path / "run_v11.db"
    _v11_database(path, candles=0)

    db = Database(path)
    try:
        assert db.schema_version() == "12"
        position = Repositories(db).positions.list_open()[0]
        assert (position.symbol, position.market, position.leg_group) == ("BTC/USDT", "linear", None)
        market = next(row for row in db.conn.execute("PRAGMA table_info(candles)") if row[1] == "market")
        assert (market[3], market[4]) == (1, None)  # NOT NULL, значения по умолчанию нет
    finally:
        db.close()


def test_v12_runs_again_after_older_code_stamps_lower(tmp_path) -> None:
    """Код до v12 при открытии штампует версию ниже — повторная v12 ничего не меняет."""
    path = tmp_path / "shared.db"
    db = Database(path)
    repos = Repositories(db)
    repos.candles.upsert_many("BTC/USDT", "1h", _bars(101.0), market="linear")
    _open_position(repos, market="spot")
    db.conn.execute("UPDATE schema_meta SET value='11' WHERE key='schema_version'")
    db.conn.commit()
    db.close()

    db = Database(path)
    try:
        assert db.schema_version() == "12"
        rows = db.conn.execute("SELECT market, close FROM candles").fetchall()
        assert [tuple(row) for row in rows] == [("linear", 101.0)] * 3  # не пересобрана, метки целы
        assert Repositories(db).positions.list_open()[0].market == "spot"
    finally:
        db.close()
