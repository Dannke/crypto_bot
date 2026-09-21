"""Database layer: SQLite persistence for candles, signals, positions, and state."""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

from ..core import policy
from ..core.enums import Mode, OrderStatus, OrderType, Side, TradeStatus
from ..core.exceptions import StorageError
from .migrations import _MIGRATIONS_FILE
from .models import (
    Candle,
    Position,
    Trade,
)


class Database:
    """SQLite-backed storage with auto-migration."""

    def __init__(self, db_path: str | Path = "data/crypto_bot.db") -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # check_same_thread=False: persisted to from the orchestrator's thread.
            self._conn = sqlite3.connect(self._path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys = ON;")
            self._conn.execute("PRAGMA journal_mode = WAL;")
        except sqlite3.Error as exc:
            raise StorageError(f"cannot open database {self._path}: {exc}") from exc
        self._migrate()

    def _read_schema_version(self) -> int:
        """Текущая версия схемы, 0 если БД ещё пуста."""
        try:
            row = self._conn.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
        except sqlite3.OperationalError:
            return 0  # schema_meta ещё не создана — новая БД
        return int(row["value"]) if row else 0

    def _migrate(self) -> None:
        # Версия фиксируется ДО применения migrations.sql. Сам скрипт в конце
        # безусловно штампует последнюю версию, поэтому каждая _migrate_vN,
        # перечитывая версию после executescript, видела уже финальное значение
        # и выходила рано — вся цепочка v2..v10 не выполнялась никогда, а БД
        # при этом отчитывалась как мигрированная.
        self._schema_version_at_open = self._read_schema_version()
        try:
            sql = _MIGRATIONS_FILE.read_text(encoding="utf-8")
            self._conn.executescript(sql)
            self._conn.commit()
        except sqlite3.Error as exc:
            raise StorageError(f"migration failed: {exc}") from exc

        # v2: add timeframe + closed_by columns to existing databases
        self._migrate_v2()
        # v3: add timeframe column to decisions
        self._migrate_v3()
        # v4: add outcome column to decisions
        self._migrate_v4()
        # v5: add 'open_unrealized_drawdown' outcome to decisions CHECK
        self._migrate_v5()
        # v6: add funding_rates and funding_payments tables
        self._migrate_v6()
        # v7: add state table for scheduler persistence (R8)
        self._migrate_v7()
        # v8: add timeout_fallback to positions.closed_by CHECK constraint
        self._migrate_v8()
        # v9: add state table for orchestrator persistence (R8)
        self._migrate_v9()
        # v10: add 'reversion' and 'time_stop' to positions.closed_by CHECK constraint
        self._migrate_v10()

    def _migrate_v2(self) -> None:
        if self._schema_version_at_open >= 2:
            return
        try:
            self._conn.executescript("""
                ALTER TABLE positions ADD COLUMN timeframe TEXT NOT NULL DEFAULT '';
                ALTER TABLE positions ADD COLUMN closed_by TEXT;
                CREATE INDEX IF NOT EXISTS idx_positions_symbol_tf_status
                    ON positions (symbol, timeframe, status);
                INSERT INTO schema_meta (key, value) VALUES ('schema_version', '2')
                    ON CONFLICT(key) DO UPDATE SET value='2';
            """)
            self._conn.commit()
        except sqlite3.OperationalError:
            pass

    def _migrate_v3(self) -> None:
        if self._schema_version_at_open >= 3:
            return
        try:
            self._conn.executescript("""
                ALTER TABLE decisions ADD COLUMN timeframe TEXT NOT NULL DEFAULT '';
                INSERT INTO schema_meta (key, value) VALUES ('schema_version', '3')
                    ON CONFLICT(key) DO UPDATE SET value='3';
            """)
            self._conn.commit()
        except sqlite3.OperationalError:
            pass

    def _migrate_v4(self) -> None:
        if self._schema_version_at_open >= 4:
            return
        try:
            self._conn.executescript("""
                ALTER TABLE decisions ADD COLUMN outcome TEXT
                    CHECK (outcome IS NULL OR outcome IN ('position_opened','drawdown_halt','slot_taken','max_positions_reached','no_position','open_unrealized_drawdown'));
                INSERT INTO schema_meta (key, value) VALUES ('schema_version', '4')
                    ON CONFLICT(key) DO UPDATE SET value='4';
            """)
            self._conn.commit()
        except sqlite3.OperationalError:
            pass

    def _migrate_v5(self) -> None:
        if self._schema_version_at_open >= 5:
            return
        try:
            self._conn.executescript("""
                ALTER TABLE decisions ADD COLUMN outcome TEXT
                    CHECK (outcome IS NULL OR outcome IN ('position_opened','drawdown_halt','slot_taken','max_positions_reached','no_position','open_unrealized_drawdown'));
                INSERT INTO schema_meta (key, value) VALUES ('schema_version', '5')
                    ON CONFLICT(key) DO UPDATE SET value='5';
            """)
            self._conn.commit()
        except sqlite3.OperationalError:
            pass

    def _migrate_v6(self) -> None:
        if self._schema_version_at_open >= 6:
            return
        try:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS funding_rates (
                    symbol           TEXT    NOT NULL,
                    funding_time_ms  INTEGER NOT NULL,
                    funding_rate     REAL    NOT NULL,
                    mark_price       REAL,
                    PRIMARY KEY (symbol, funding_time_ms)
                );
                CREATE INDEX IF NOT EXISTS idx_funding_rates_symbol_time
                    ON funding_rates (symbol, funding_time_ms);
                CREATE TABLE IF NOT EXISTS funding_payments (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    position_id     INTEGER NOT NULL,
                    funding_time_ms INTEGER NOT NULL,
                    amount          REAL    NOT NULL,
                    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                    FOREIGN KEY (position_id) REFERENCES positions(id) ON DELETE RESTRICT
                );
                CREATE INDEX IF NOT EXISTS idx_funding_payments_position
                    ON funding_payments (position_id);
                CREATE INDEX IF NOT EXISTS idx_funding_payments_time
                    ON funding_payments (funding_time_ms);
                INSERT INTO schema_meta (key, value) VALUES ('schema_version', '6')
                    ON CONFLICT(key) DO UPDATE SET value='6';
            """)
            self._conn.commit()
        except sqlite3.OperationalError:
            pass

    def _migrate_v7(self) -> None:
        if self._schema_version_at_open >= 7:
            return
        try:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS state (
                    key           TEXT    PRIMARY KEY,
                    value         TEXT    NOT NULL,
                    updated_at    INTEGER NOT NULL
                );
                INSERT INTO schema_meta (key, value) VALUES ('schema_version', '7')
                    ON CONFLICT(key) DO UPDATE SET value='7';
            """)
            self._conn.commit()
        except sqlite3.OperationalError:
            pass

    def _migrate_v8(self) -> None:
        if self._schema_version_at_open >= 8:
            return
        try:
            # Recreate positions table with updated closed_by CHECK constraint
            self._conn.executescript("""
                -- Create new positions table with updated closed_by CHECK constraint
                CREATE TABLE positions_new (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol        TEXT    NOT NULL,
                    timeframe     TEXT    NOT NULL,
                    side          TEXT    NOT NULL CHECK (side IN ('LONG','SHORT')),
                    size          REAL    NOT NULL,
                    entry_price   REAL    NOT NULL,
                    stop          REAL    NOT NULL,
                    take          REAL    NOT NULL,
                    status        TEXT    NOT NULL DEFAULT 'open'
                      CHECK (status IN ('proposed','open','closed','rejected','cancelled')),
                    closed_by     TEXT        CHECK (closed_by IS NULL OR closed_by IN ('stop_loss','take_profit','manual','signal','emergency_drawdown','rebalance','timeout_fallback')),
                    opened_at_ms  INTEGER NOT NULL,
                    closed_at_ms  INTEGER,
                    exit_price    REAL,
                    pnl_pct       REAL,
                    mode          TEXT    NOT NULL CHECK (mode IN ('signal_only','paper','live')),
                    created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                    updated_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
                );

                -- Copy data from old table
                INSERT INTO positions_new (id, symbol, timeframe, side, size, entry_price, stop, take, status, closed_by, opened_at_ms, closed_at_ms, exit_price, pnl_pct, mode, created_at, updated_at)
                SELECT id, symbol, timeframe, side, size, entry_price, stop, take, status, closed_by, opened_at_ms, closed_at_ms, exit_price, pnl_pct, mode, created_at, updated_at
                FROM positions;

                -- Drop old table and rename new
                DROP TABLE positions;
                ALTER TABLE positions_new RENAME TO positions;

                -- Recreate indexes
                CREATE INDEX IF NOT EXISTS idx_positions_status_symbol ON positions (status, symbol);
                CREATE INDEX IF NOT EXISTS idx_positions_symbol_opened ON positions (symbol, opened_at_ms);
                CREATE INDEX IF NOT EXISTS idx_positions_open ON positions (status);
                CREATE INDEX IF NOT EXISTS idx_positions_symbol_tf_status ON positions (symbol, timeframe, status);

                -- Update schema version
                INSERT INTO schema_meta (key, value) VALUES ('schema_version', '8')
                    ON CONFLICT(key) DO UPDATE SET value='8';
            """)
            self._conn.commit()
        except sqlite3.OperationalError:
            pass

    def _migrate_v9(self) -> None:
        if self._schema_version_at_open >= 9:
            return
        try:
            # Add state table for orchestrator persistence (R8)
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS state (
                    key           TEXT    PRIMARY KEY,
                    value         TEXT    NOT NULL,
                    updated_at    INTEGER NOT NULL
                );
                INSERT INTO schema_meta (key, value) VALUES ('schema_version', '9')
                    ON CONFLICT(key) DO UPDATE SET value='9';
            """)
            self._conn.commit()
        except sqlite3.OperationalError:
            pass

    def _migrate_v10(self) -> None:
        if self._schema_version_at_open >= 10:
            return
        # Пересоздание таблицы требует снять проверку внешних ключей: trades и
        # decisions ссылаются на positions(id) с ON DELETE RESTRICT, и DROP TABLE
        # иначе падает на любой БД, где уже есть сделки. PRAGMA нельзя менять
        # внутри транзакции, поэтому выставляем её до executescript.
        self._conn.commit()
        self._conn.execute("PRAGMA foreign_keys = OFF;")
        try:
            # Recreate positions table with updated closed_by CHECK constraint
            # Added 'reversion' and 'time_stop' as valid close reasons for MeanReversionStrategy
            self._conn.executescript("""
                -- Create new positions table with updated closed_by CHECK constraint
                CREATE TABLE positions_new (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol        TEXT    NOT NULL,
                    timeframe     TEXT    NOT NULL,
                    side          TEXT    NOT NULL CHECK (side IN ('LONG','SHORT')),
                    size          REAL    NOT NULL,
                    entry_price   REAL    NOT NULL,
                    stop          REAL    NOT NULL,
                    take          REAL    NOT NULL,
                    status        TEXT    NOT NULL DEFAULT 'open'
                      CHECK (status IN ('proposed','open','closed','rejected','cancelled')),
                    closed_by     TEXT        CHECK (closed_by IS NULL OR closed_by IN ('stop_loss','take_profit','manual','signal','emergency_drawdown','rebalance','timeout_fallback','reversion','time_stop')),
                    opened_at_ms  INTEGER NOT NULL,
                    closed_at_ms  INTEGER,
                    exit_price    REAL,
                    pnl_pct       REAL,
                    mode          TEXT    NOT NULL CHECK (mode IN ('signal_only','paper','live')),
                    created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                    updated_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
                );

                -- Copy data from old table
                INSERT INTO positions_new (id, symbol, timeframe, side, size, entry_price, stop, take, status, closed_by, opened_at_ms, closed_at_ms, exit_price, pnl_pct, mode, created_at, updated_at)
                SELECT id, symbol, timeframe, side, size, entry_price, stop, take, status, closed_by, opened_at_ms, closed_at_ms, exit_price, pnl_pct, mode, created_at, updated_at
                FROM positions;

                -- Drop old table and rename new
                DROP TABLE positions;
                ALTER TABLE positions_new RENAME TO positions;

                -- Recreate indexes
                CREATE INDEX IF NOT EXISTS idx_positions_status_symbol ON positions (status, symbol);
                CREATE INDEX IF NOT EXISTS idx_positions_symbol_opened ON positions (symbol, opened_at_ms);
                CREATE INDEX IF NOT EXISTS idx_positions_open ON positions (status);
                CREATE INDEX IF NOT EXISTS idx_positions_symbol_tf_status ON positions (symbol, timeframe, status);

                -- Update schema version
                INSERT INTO schema_meta (key, value) VALUES ('schema_version', '10')
                    ON CONFLICT(key) DO UPDATE SET value='10';
            """)
            self._conn.commit()
        except sqlite3.Error as exc:
            # Раньше здесь стоял `except sqlite3.OperationalError: pass`: любой
            # сбой пересоздания таблицы гасился, а БД оставалась на старом CHECK,
            # отчитываясь как мигрированная. Первый же выход reversion/time_stop
            # падал бы с IntegrityError уже после того, как трекер закрыл позицию.
            self._conn.rollback()
            raise StorageError(f"migration v10 (positions.closed_by CHECK) failed: {exc}") from exc
        finally:
            self._conn.execute("PRAGMA foreign_keys = ON;")

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    @property
    def path(self) -> Path:
        return self._path

    @property
    def db_path(self) -> Path:
        return self._path

    def schema_version(self) -> str:
        row = self._conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        return row["value"] if row else "0"

    def close(self) -> None:
        self._conn.close()

    def transaction(self):
        """Return a context manager for database transactions."""
        return self._conn

    # ---- Candles ----

    def candles_upsert(
        self,
        symbol: str,
        timeframe: str,
        candles: list[Candle],
    ) -> int:
        """Insert or replace candles (by symbol, timeframe, timestamp)."""
        if not candles:
            return 0
        with self._conn:
            self._conn.executemany(
                """INSERT OR REPLACE INTO candles
                   (symbol, timeframe, timestamp, open, high, low, close, volume)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [(c.symbol, c.timeframe, c.timestamp, c.open, c.high, c.low, c.close, c.volume)
                 for c in candles],
            )
        return len(candles)

    def candles_fetch(
        self,
        symbol: str,
        timeframe: str,
        limit: int = 500,
        since_ms: int | None = None,
    ) -> list[Candle]:
        """Fetch candles for a symbol/timeframe, newest first."""
        sql = "SELECT symbol, timeframe, timestamp, open, high, low, close, volume FROM candles WHERE symbol=? AND timeframe=?"
        params: list = [symbol, timeframe]
        if since_ms is not None:
            sql += " AND ts_ms >= ?"
            params.append(since_ms)
        sql += " ORDER BY ts_ms DESC LIMIT ?"
        params = [symbol, timeframe, limit] if since_ms is None else [symbol, timeframe, limit, since_ms]
        rows = self._conn.execute(sql, params).fetchall()
        return [Candle(**row) for row in rows]

    def candles_latest(self, symbol: str, timeframe: str) -> Candle | None:
        row = self._conn.execute(
            "SELECT symbol, timeframe, timestamp, open, high, low, close, volume FROM candles WHERE symbol=? AND timeframe=? ORDER BY ts_ms DESC LIMIT 1",
            (symbol, timeframe),
        ).fetchone()
        return Candle(**row) if row else None

    # ---- Signals ----

    def signals_fetch(
        self,
        symbol: str,
        timeframe: str,
        limit: int = 100,
        since_ms: int | None = None,
    ) -> list:
        sql = "SELECT ts_ms, symbol, signal, side, confidence, score, timeframe, reason FROM signals WHERE symbol=? AND timeframe=?"
        params: list = [symbol, timeframe]
        if since_ms is not None:
            sql += " AND ts_ms >= ?"
            params.append(since_ms)
        sql += " ORDER BY ts_ms DESC LIMIT ?"
        params.append(limit)
        return self._conn.execute(sql, params).fetchall()

    # ---- Decisions ----

    def decisions_upsert(self, decision) -> int:
        with self._conn:
            self._conn.execute(
                """INSERT OR REPLACE INTO decisions
                   (ts_ms, symbol, timeframe, accepted, reject_reason, score, signal, outcome, detail)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    decision.ts_ms,
                    decision.symbol,
                    decision.timeframe,
                    int(decision.accepted),
                    decision.reject_reason,
                    decision.score,
                    decision.signal,
                    decision.outcome,
                    decision.detail,
                ),
            )
        return 1

    def decisions_fetch(
        self,
        symbol: str | None = None,
        since_ms: int | None = None,
        limit: int = 100,
    ) -> list:
        sql = "SELECT ts_ms, symbol, timeframe, accepted, reject_reason, score, signal, outcome, detail FROM decisions WHERE 1=1"
        params: list = []
        if symbol:
            sql += " AND symbol=?"
            params.append(symbol)
        if since_ms is not None:
            sql += " AND ts_ms >= ?"
            params.append(since_ms)
        sql += " ORDER BY ts_ms DESC LIMIT ?"
        params.append(limit)
        return self._conn.execute(sql, tuple(params)).fetchall()

    # ---- Positions ----

    def positions_upsert(self, position) -> int:
        with self._conn:
            self._conn.execute(
                """INSERT OR REPLACE INTO positions
                   (symbol, timeframe, side, size, entry_price, stop, take, status, closed_by, opened_at_ms, closed_at_ms, exit_price, pnl_pct, mode)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    position.symbol,
                    position.timeframe,
                    position.side.value,
                    position.size,
                    position.entry_price,
                    position.stop,
                    position.take,
                    position.status.value,
                    position.closed_by,
                    position.opened_at_ms,
                    position.closed_at_ms,
                    position.exit_price,
                    position.pnl_pct,
                    position.mode.value,
                ),
            )
        return 1

    def positions_fetch(
        self,
        symbol: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list:
        sql = "SELECT * FROM positions WHERE 1=1"
        params: list = []
        if symbol:
            sql += " AND symbol=?"
            params.append(symbol)
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY opened_at_ms DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [Position(**row) for row in rows]

    # ---- Trades ----

    def trades_insert(self, trade) -> int:
        with self._conn:
            cur = self._conn.execute(
                """INSERT INTO trades
                   (position_id, symbol, side, order_type, size, price, status, ts_ms, mode)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    trade.position_id,
                    trade.symbol,
                    trade.side.value,
                    trade.order_type.value,
                    trade.size,
                    trade.price,
                    trade.status.value,
                    trade.ts_ms,
                    trade.mode.value,
                ),
            )
        return cur.lastrowid

    # ---- Equity ----

    def equity_upsert(self, ts_ms: int, currency: str, equity: float, drawdown_pct: float) -> int:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO equity (ts_ms, currency, equity, drawdown_pct) VALUES (?, ?, ?, ?)",
                (ts_ms, currency, equity, drawdown_pct),
            )
        return 1

    def equity_latest(self) -> tuple[int, str, float, float] | None:
        row = self._conn.execute(
            "SELECT ts_ms, currency, equity, drawdown_pct FROM equity ORDER BY ts_ms DESC LIMIT 1"
        ).fetchone()
        return tuple(row) if row else None

    # ---- State (R8) ----

    def state_get(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM state WHERE key=?", (key,)
        ).fetchone()
        return row[0] if row else None


class CandleRepository:
    """Read-only access to candles for backtesting."""

    def __init__(self, db: Database):
        self._db = db

    _SELECT = (
        "SELECT symbol, timeframe, ts_ms as timestamp, open, high, low, close, volume "
        "FROM candles WHERE symbol=? AND timeframe=? AND ts_ms >= ? "
    )

    def fetch(
        self,
        symbol: str,
        timeframe: str,
        since_ms: int = 0,
        limit: int = 400,
    ) -> list[Candle]:
        """Последние ``limit`` свечей начиная с ``since_ms``, в порядке возрастания.

        Ограниченное чтение для живого фида: ``limit`` зажимается политикой
        ``policy.MAX_CANDLES_LOOKBACK``. Бэктесту нужна вся история — он
        обязан использовать :meth:`fetch_since`, а не увеличивать limit здесь.

        Берутся именно ПОСЛЕДНИЕ свечи (ORDER BY DESC + разворот), а не первые:
        фиду нужен свежий хвост истории, а не её начало.
        """
        limit_val = max(1, min(int(limit), policy.MAX_CANDLES_LOOKBACK))
        rows = self._db._conn.execute(
            self._SELECT + "ORDER BY ts_ms DESC LIMIT ?",
            (symbol, timeframe, since_ms, limit_val),
        ).fetchall()
        return [Candle(**row) for row in reversed(rows)]

    def fetch_since(
        self,
        symbol: str,
        timeframe: str,
        since_ms: int,
        limit: int | None = None,
    ) -> list[Candle]:
        """ВСЕ свечи начиная с ``since_ms``, в порядке возрастания.

        Путь воспроизведения истории для бэктеста, намеренно НЕ ограниченный
        ``policy.MAX_CANDLES_LOOKBACK``: эта политика защищает частоту обращений
        к бирже, а не чтение локальной БД.

        Ранее здесь стоял дефолт ``limit=400``, из-за чего вызовы вида
        ``fetch_since(sym, tf, since_ms=0)`` — а так его зовут и
        HistoricalCandleSource.load_all_async, и walk_forward — молча получали
        первые 400 баров вместо всей истории. Бэктест на реальных данных читал
        ~1.7% запрошенного и почти всегда промахивался мимо нужного окна.
        """
        if limit is None:
            rows = self._db._conn.execute(
                self._SELECT + "ORDER BY ts_ms ASC",
                (symbol, timeframe, since_ms),
            ).fetchall()
        else:
            rows = self._db._conn.execute(
                self._SELECT + "ORDER BY ts_ms ASC LIMIT ?",
                (symbol, timeframe, since_ms, max(1, int(limit))),
            ).fetchall()
        return [Candle(**row) for row in rows]

    def upsert_many(
        self,
        symbol: str,
        timeframe: str,
        candles: list[Candle],
    ) -> int:
        """Insert many candles, ignoring conflicts (ON CONFLICT DO NOTHING)."""
        if not candles:
            return 0
        with self._db._conn:
            self._db._conn.executemany(
                """INSERT OR IGNORE INTO candles
                   (symbol, timeframe, ts_ms, open, high, low, close, volume)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [(symbol, timeframe, c.timestamp, c.open, c.high, c.low, c.close, c.volume)
                 for c in candles],
            )
        return len(candles)

    def latest_ts(self, symbol: str, timeframe: str) -> int | None:
        """Get the latest candle timestamp for a symbol/timeframe."""
        row = self._db._conn.execute(
            "SELECT MAX(ts_ms) FROM candles WHERE symbol=? AND timeframe=?",
            (symbol, timeframe),
        ).fetchone()
        return row[0] if row and row[0] is not None else None

    def latest_close(self, symbol: str, timeframe: str) -> float | None:
        """Get the latest candle close price for a symbol/timeframe."""
        row = self._db._conn.execute(
            "SELECT close FROM candles WHERE symbol=? AND timeframe=? ORDER BY ts_ms DESC LIMIT 1",
            (symbol, timeframe),
        ).fetchone()
        return row[0] if row else None

    async def latest_ts_async(self, symbol: str, timeframe: str) -> int | None:
        """Get the latest candle timestamp for a symbol/timeframe."""
        row = self._db._conn.execute(
            "SELECT MAX(ts_ms) FROM candles WHERE symbol=? AND timeframe=?",
            (symbol, timeframe),
        ).fetchone()
        return row[0] if row and row[0] is not None else None

    async def fetch_async(
        self,
        symbol: str,
        timeframe: str,
        limit: int = 400,
        since_ms: int = 0,
    ) -> list[Candle]:
        """Async-обёртка над :meth:`fetch` через run_in_executor.

        Делегирует, а не дублирует SQL: иначе политика ``MAX_CANDLES_LOOKBACK``
        и порядок выборки расходятся между синхронным и асинхронным путём.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, lambda: self.fetch(symbol, timeframe, since_ms, limit)
        )

    async def upsert_many_async(
        self,
        symbol: str,
        timeframe: str,
        candles: list[Candle],
    ) -> int:
        """Insert or replace many candles asynchronously."""
        if not candles:
            return 0
        with self._db._conn:
            self._db._conn.executemany(
                """INSERT OR REPLACE INTO candles
                   (symbol, timeframe, ts_ms, open, high, low, close, volume)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [(symbol, timeframe, c.timestamp, c.open, c.high, c.low, c.close, c.volume)
                 for c in candles],
            )
        return len(candles)


class PositionRepository:
    """Repository for position operations."""

    def __init__(self, db: Database):
        self._db = db

    def list_open(self, symbol: str | None = None) -> list[Position]:
        sql = "SELECT * FROM positions WHERE status='open'"
        params = []
        if symbol:
            sql += " AND symbol=?"
            params.append(symbol)
        sql += " ORDER BY opened_at_ms DESC"
        rows = self._db._conn.execute(sql, tuple(params)).fetchall()
        return [Position(**row) for row in rows]

    def list_closed(self, symbol: str | None = None, limit: int = 1000) -> list[Position]:
        sql = "SELECT * FROM positions WHERE status='closed'"
        params = []
        if symbol:
            sql += " AND symbol=?"
            params.append(symbol)
        sql += " ORDER BY closed_at_ms DESC LIMIT ?"
        params.append(limit)
        rows = self._db._conn.execute(sql, tuple(params)).fetchall()
        return [Position(**row) for row in rows]

    def open_exists(self, symbol: str, timeframe: str | None = None) -> bool:
        if timeframe:
            row = self._db._conn.execute(
                "SELECT 1 FROM positions WHERE symbol=? AND timeframe=? AND status='open' LIMIT 1",
                (symbol, timeframe),
            ).fetchone()
        else:
            row = self._db._conn.execute(
                "SELECT 1 FROM positions WHERE symbol=? AND status='open' LIMIT 1",
                (symbol,),
            ).fetchone()
        return row is not None

    def insert(
        self,
        symbol: str,
        timeframe: str,
        side: Side,
        size: float,
        entry_price: float,
        stop: float,
        take: float,
        mode: Mode,
        opened_at_ms: int | None = None,
        status: TradeStatus = TradeStatus.OPEN,
    ) -> int:
        import time
        if opened_at_ms is None:
            opened_at_ms = int(time.time() * 1000)
        with self._db._conn:
            cur = self._db._conn.execute(
                """INSERT INTO positions
                   (symbol, timeframe, side, size, entry_price, stop, take, status, opened_at_ms, mode)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    symbol,
                    timeframe,
                    side.value,
                    size,
                    entry_price,
                    stop,
                    take,
                    status.value,
                    opened_at_ms,
                    mode.value,
                ),
            )
        return cur.lastrowid

    def close(
        self,
        position_id: int,
        *,
        reason: str = "manual",
        closed_by: str | None = None,
        exit_price: float | None = None,
        pnl_pct: float | None = None,
        closed_at_ms: int | None = None,
    ) -> bool:
        import time
        # Support both `reason` and `closed_by` for backward compatibility
        close_reason = closed_by or reason
        with self._db._conn:
            row = self._db._conn.execute(
                "SELECT id FROM positions WHERE id=? AND status='open'",
                (position_id,),
            ).fetchone()
            if not row:
                return False
            pid = row[0]
            closed_at_ms = closed_at_ms or int(time.time() * 1000)
            self._db._conn.execute(
                "UPDATE positions SET status='closed', closed_by=?, exit_price=?, pnl_pct=?, closed_at_ms=? WHERE id=?",
                (close_reason, exit_price, pnl_pct, closed_at_ms, pid),
            )
        return True

    def close_by_symbol(
        self,
        symbol: str,
        timeframe: str,
        *,
        reason: str,
        exit_price: float | None = None,
        pnl_pct: float | None = None,
        closed_at_ms: int | None = None,
    ) -> bool:
        import time
        with self._db._conn:
            row = self._db._conn.execute(
                "SELECT id, size, entry_price, side FROM positions WHERE symbol=? AND timeframe=? AND status='open'",
                (symbol, timeframe),
            ).fetchone()
            if not row:
                return False
            pid, size, entry_price, side = row
            exit_price = exit_price or 0.0
            pnl_pct = pnl_pct or 0.0
            closed_at_ms = closed_at_ms or int(time.time() * 1000)
            self._db._conn.execute(
                "UPDATE positions SET status='closed', closed_by=?, exit_price=?, pnl_pct=?, closed_at_ms=? WHERE id=?",
                (reason, exit_price, pnl_pct, closed_at_ms, pid),
            )
        return True

    def delete_all(self) -> int:
        with self._db._conn:
            cur = self._db._conn.execute("DELETE FROM positions")
        return cur.rowcount

    def close_all_open(self, exit_price: float, closed_by: str) -> int:
        import time
        closed_at_ms = int(time.time() * 1000)
        with self._db._conn:
            cur = self._db._conn.execute(
                "UPDATE positions SET status='closed', closed_by=?, exit_price=?, closed_at_ms=? WHERE status='open'",
                (closed_by, exit_price, closed_at_ms),
            )
        return cur.rowcount

    def delete_for_symbol(self, symbol: str) -> int:
        with self._db._conn:
            cur = self._db._conn.execute("DELETE FROM positions WHERE symbol=?", (symbol,))
        return cur.rowcount


class SignalRepository:
    """Repository for signal operations."""

    def __init__(self, db: Database):
        self._db = db

    def insert(
        self,
        symbol: str,
        signal: str,
        confidence: float,
        side: str | None,
        score: float | None,
        timeframe: str,
        reason: str,
        ts_ms: int | None = None,
    ) -> int:
        import time
        if ts_ms is None:
            ts_ms = int(time.time() * 1000)
        with self._db._conn:
            self._db._conn.execute(
                """INSERT OR REPLACE INTO signals
                   (ts_ms, symbol, signal, side, confidence, score, timeframe, reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (ts_ms, symbol, signal, side, confidence, score or 0.0, timeframe, reason),
            )
        return 1


class TradeRepository:
    """Repository for trade operations."""

    def __init__(self, db: Database):
        self._db = db

    def insert(
        self,
        position_id: int,
        symbol: str,
        side: Side,
        order_type: OrderType,
        size: float,
        price: float,
        mode: Mode,
        status: OrderStatus = OrderStatus.FILLED,
        external_id: str | None = None,
    ) -> int:
        import time
        ts_ms = int(time.time() * 1000)
        with self._db._conn:
            cur = self._db._conn.execute(
                """INSERT INTO trades
                   (position_id, symbol, side, order_type, size, price, status, ts_ms, mode, external_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    position_id,
                    symbol,
                    side.value,
                    order_type.value,
                    size,
                    price,
                    status.value,
                    ts_ms,
                    mode.value,
                    external_id,
                ),
            )
        return cur.lastrowid

    def list_for_position(self, position_id: int) -> list:
        rows = self._db._conn.execute(
            "SELECT * FROM trades WHERE position_id=? ORDER BY ts_ms",
            (position_id,),
        ).fetchall()
        return [Trade(**row) for row in rows]

    def latest_for_symbol(self, symbol: str, limit: int = 1) -> list:
        rows = self._db._conn.execute(
            "SELECT * FROM trades WHERE symbol=? ORDER BY ts_ms DESC LIMIT ?",
            (symbol, limit),
        ).fetchall()
        return [Trade(**row) for row in rows]


class EquityRepository:
    """Repository for equity operations."""

    def __init__(self, db: Database):
        self._db = db

    def insert(
        self,
        currency: str,
        equity: float,
        drawdown_pct: float,
        mode: Mode,
        ts_ms: int | None = None,
    ) -> int:
        import time
        if ts_ms is None:
            ts_ms = int(time.time() * 1000)
        with self._db._conn:
            self._db._conn.execute(
                "INSERT OR REPLACE INTO equity (ts_ms, currency, equity, drawdown_pct, mode) VALUES (?, ?, ?, ?, ?)",
                (ts_ms, currency, equity, drawdown_pct, mode.value),
            )
        return 1

    def latest(self, mode: Mode) -> tuple[float, float] | None:
        """Get latest equity for mode. Returns (equity, drawdown_pct) or None."""
        row = self._db._conn.execute(
            "SELECT equity, drawdown_pct FROM equity WHERE mode=? ORDER BY ts_ms DESC LIMIT 1",
            (mode.value,),
        ).fetchone()
        return (row[0], row[1]) if row else None


class DecisionRepository:
    """Repository for decision operations."""

    def __init__(self, db: Database):
        self._db = db

    def insert(self, decision) -> int:
        with self._db._conn:
            self._db._conn.execute(
                """INSERT OR REPLACE INTO decisions
                   (ts_ms, symbol, timeframe, accepted, reject_reason, score, signal, outcome, detail)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    int(decision.timestamp.timestamp() * 1000) if hasattr(decision, 'timestamp') else decision.ts_ms,
                    decision.symbol,
                    decision.timeframe,
                    int(decision.accepted),
                    decision.reason.value if hasattr(decision.reason, 'value') else decision.reason,
                    decision.score,
                    decision.signal.value if hasattr(decision.signal, 'value') else decision.signal,
                    decision.outcome,
                    decision.detail,
                ),
            )
        return 1

    def insert_many(self, decisions) -> int:
        with self._db._conn:
            self._db._conn.executemany(
                """INSERT OR REPLACE INTO decisions
                   (ts_ms, symbol, timeframe, accepted, reject_reason, score, signal, outcome, detail)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        int(d.timestamp.timestamp() * 1000) if hasattr(d, 'timestamp') else d.ts_ms,
                        d.symbol,
                        d.timeframe,
                        int(d.accepted),
                        d.reason.value if hasattr(d.reason, 'value') else d.reason,
                        d.score,
                        d.signal.value if hasattr(d.signal, 'value') else d.signal,
                        d.outcome,
                        d.detail,
                    )
                    for d in decisions
                ],
            )
        return len(decisions)

    def count_recent_for_symbol(self, symbol: str, within_minutes: int) -> int:
        import time
        since_ms = int(time.time() * 1000) - within_minutes * 60 * 1000
        row = self._db._conn.execute(
            "SELECT COUNT(*) FROM decisions WHERE symbol=? AND ts_ms >= ?",
            (symbol, since_ms),
        ).fetchone()
        return row[0] if row else 0


class Repositories:
    """Aggregate repositories for dependency injection."""

    def __init__(self, db: Database):
        self._db = db
        self.candles = CandleRepository(db)
        self.positions = PositionRepository(db)
        self.signals = SignalRepository(db)
        self.trades = TradeRepository(db)
        self.equity = EquityRepository(db)
        self.decisions = DecisionRepository(db)

    @property
    def db(self):
        return self._db