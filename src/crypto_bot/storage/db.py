"""SQLite storage layer: connection management + typed repositories.

Design goals:
  * Swappable backend. All SQL lives here; the rest of the app talks to
    repository methods returning core domain types. Replacing SQLite with
    Postgres means implementing the same repository protocols elsewhere.
  * Safety. ``foreign_keys`` and ``WAL`` are enabled per-connection; schema
    migrations run in a transaction and stamp a version row.
  * Testability. The repositories take an open ``sqlite3.Connection`` (sync) so
    unit tests can pass an in-memory DB without spinning up files or network.

Synchronous deliberately: storage calls happen off the hot async path (the
orchestrator persists after the scan), and a sync sqlite3 binding keeps the
dependency surface to the stdlib.
"""
from __future__ import annotations

import sqlite3
import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from ..core import policy
from ..core.enums import Mode, OrderStatus, OrderType, RejectReason, Side, Signal, TradeStatus
from ..core.exceptions import StorageError
from ..core.types import Candle, DecisionRecord, Position, Trade

_MIGRATIONS_FILE = Path(__file__).parent / "migrations.sql"


def _now_ms() -> int:
    return int(datetime.now(tz=UTC).timestamp() * 1000)


def _utc_iso() -> str:
    return datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _reject_value(reason: RejectReason | None) -> str | None:
    return reason.value if reason else None


def _lastrowid(cur: sqlite3.Cursor) -> int:
    if cur.lastrowid is None:
        raise StorageError("SQLite did not return a lastrowid for insert.")
    return int(cur.lastrowid)


# --------------------------------------------------------------------------- #
# Connection / migration
# --------------------------------------------------------------------------- #
class Database:
    """Thin wrapper around a sqlite3 connection with PRAGMAs + migrations."""

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

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    @property
    def db_path(self) -> Path:
        return self._path

    def _migrate(self) -> None:
        try:
            sql = _MIGRATIONS_FILE.read_text(encoding="utf-8")
            self._conn.executescript(sql)
            self._conn.commit()
        except sqlite3.Error as exc:
            raise StorageError(f"migration failed: {exc}") from exc

    def schema_version(self) -> str:
        row = self._conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        return str(row["value"]) if row else "unknown"

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Context-managed transaction that rolls back on error."""
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error as exc:  # pragma: no cover - defensive
            raise StorageError(f"error closing database: {exc}") from exc


# --------------------------------------------------------------------------- #
# Candle repository
# --------------------------------------------------------------------------- #
class CandleRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    # Sync methods (for tests and other sync code)
    def upsert_many(self, symbol: str, timeframe: str, candles: list[Candle]) -> int:
        """Synchronous upsert."""
        if not candles:
            return 0
        rows = [
            (symbol, timeframe, c.timestamp, c.open, c.high, c.low, c.close, c.volume)
            for c in candles
        ]
        with self._db.transaction() as conn:
            conn.executemany(
                """INSERT INTO candles (symbol, timeframe, ts_ms, open, high, low, close, volume)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(symbol, timeframe, ts_ms) DO NOTHING""",
                rows,
            )
        return len(rows)

    def latest_ts(self, symbol: str, timeframe: str) -> int | None:
        """Synchronous latest_ts."""
        row = self._db.conn.execute(
            "SELECT MAX(ts_ms) AS m FROM candles WHERE symbol=? AND timeframe=?",
            (symbol, timeframe),
        ).fetchone()
        return int(row["m"]) if row and row["m"] is not None else None

    def fetch(self, symbol: str, timeframe: str, limit: int = 200) -> list[Candle]:
        """Synchronous fetch."""
        limit_val = max(1, min(int(limit), policy.MAX_CANDLES_LOOKBACK))
        rows = self._db.conn.execute(
            """SELECT ts_ms, open, high, low, close, volume
                 FROM candles
                WHERE symbol=? AND timeframe=?
                ORDER BY ts_ms DESC LIMIT ?""",
            (symbol, timeframe, limit_val),
        ).fetchall()
        out = [
            Candle(
                timestamp=int(r["ts_ms"]), open=float(r["open"]), high=float(r["high"]),
                low=float(r["low"]), close=float(r["close"]), volume=float(r["volume"]),
            )
            for r in rows
        ]
        out.reverse()  # ascending for indicators
        return out

    # Async methods for use in async context (use run_in_executor)
    async def upsert_many_async(self, symbol: str, timeframe: str, candles: list[Candle]) -> int:
        """Async wrapper using run_in_executor for sync SQLite operations."""
        return await asyncio.get_event_loop().run_in_executor(
            None, lambda: self.upsert_many(symbol, timeframe, candles)
        )

    async def latest_ts_async(self, symbol: str, timeframe: str) -> int | None:
        """Async wrapper using run_in_executor for sync SQLite operations."""
        return await asyncio.get_event_loop().run_in_executor(
            None, lambda: self.latest_ts(symbol, timeframe)
        )

    async def fetch_async(self, symbol: str, timeframe: str, limit: int = 200) -> list[Candle]:
        """Async wrapper using run_in_executor for sync SQLite operations."""
        return await asyncio.get_event_loop().run_in_executor(
            None, lambda: self.fetch(symbol, timeframe, limit)
        )

    async def fetch_since(self, symbol: str, timeframe: str, since_ts: int) -> list[Candle]:
        """Fetch candles with timestamp greater than since_ts (for incremental updates)."""
        def _sync_fetch_since() -> list[Candle]:
            rows = self._db.conn.execute(
                """SELECT ts_ms, open, high, low, close, volume
                     FROM candles
                    WHERE symbol=? AND timeframe=? AND ts_ms > ?
                    ORDER BY ts_ms ASC""",
                (symbol, timeframe, since_ts),
            ).fetchall()
            out = [
                Candle(
                    timestamp=int(r["ts_ms"]), open=float(r["open"]), high=float(r["high"]),
                    low=float(r["low"]), close=float(r["close"]), volume=float(r["volume"]),
                )
                for r in rows
            ]
            return out
        
        return await asyncio.get_event_loop().run_in_executor(None, _sync_fetch_since)

    async def count(self, symbol: str, timeframe: str) -> int:
        """Count candles for a symbol/timeframe pair."""
        def _sync_count() -> int:
            row = self._db.conn.execute(
                "SELECT COUNT(*) AS c FROM candles WHERE symbol=? AND timeframe=?",
                (symbol, timeframe),
            ).fetchone()
            return int(row["c"]) if row else 0
        
        return await asyncio.get_event_loop().run_in_executor(None, _sync_count)


# --------------------------------------------------------------------------- #
# Signal repository
# --------------------------------------------------------------------------- #
class SignalRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def insert(
        self,
        symbol: str,
        signal: Signal,
        confidence: float,
        side: Side | None = None,
        score: float | None = None,
        timeframe: str | None = None,
        reason: str = "",
        ts_ms: int | None = None,
    ) -> int:
        with self._db.transaction() as conn:
            cur = conn.execute(
                """INSERT INTO signals
                   (ts_ms, symbol, signal, side, confidence, score, timeframe, reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    ts_ms or _now_ms(),
                    symbol,
                    signal.value,
                    side.value if side else None,
                    float(confidence),
                    score,
                    timeframe,
                    reason,
                ),
            )
            return _lastrowid(cur)


# --------------------------------------------------------------------------- #
# Decision repository
# --------------------------------------------------------------------------- #
class DecisionRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def insert(self, decision: DecisionRecord, ts_ms: int | None = None) -> int:
        with self._db.transaction() as conn:
            cur = conn.execute(
                """INSERT INTO decisions
                   (ts_ms, symbol, accepted, reject_reason, detail, score, signal)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    ts_ms or _now_ms(),
                    decision.symbol,
                    1 if decision.accepted else 0,
                    _reject_value(decision.reason),
                    decision.detail,
                    decision.score,
                    decision.signal.value if decision.signal else None,
                ),
            )
            return _lastrowid(cur)

    def count_recent_for_symbol(self, symbol: str, within_minutes: int) -> int:
        """Number of decisions (any kind) for a symbol within the window.

        Used by the cooldown filter to detect recent activity.
        """
        cutoff = _now_ms() - within_minutes * 60 * 1000
        row = self._db.conn.execute(
            "SELECT COUNT(*) AS c FROM decisions WHERE symbol=? AND ts_ms >= ?",
            (symbol, cutoff),
        ).fetchone()
        return int(row["c"]) if row else 0


# --------------------------------------------------------------------------- #
# Position repository
# --------------------------------------------------------------------------- #
class PositionRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def open_exists(self, symbol: str) -> bool:
        row = self._db.conn.execute(
            "SELECT 1 FROM positions WHERE symbol=? AND status='open' LIMIT 1",
            (symbol,),
        ).fetchone()
        return row is not None

    def list_open(self) -> list[Position]:
        rows = self._db.conn.execute(
            """SELECT symbol, side, size, entry_price, stop, take,
                      opened_at_ms, status, closed_at_ms, exit_price, pnl_pct
                 FROM positions WHERE status='open'
                ORDER BY opened_at_ms"""
        ).fetchall()
        return [self._row_to_position(r) for r in rows]

    def insert(
        self,
        symbol: str,
        side: Side,
        size: float,
        entry_price: float,
        stop: float,
        take: float,
        mode: Mode,
        opened_at_ms: int | None = None,
        status: TradeStatus = TradeStatus.OPEN,
    ) -> int:
        with self._db.transaction() as conn:
            cur = conn.execute(
                """INSERT INTO positions
                   (symbol, side, size, entry_price, stop, take, status,
                    opened_at_ms, mode)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    symbol, side.value, float(size), float(entry_price),
                    float(stop), float(take), status.value,
                    opened_at_ms or _now_ms(), mode.value,
                ),
            )
            return _lastrowid(cur)

    def close(
        self,
        position_id: int,
        exit_price: float,
        pnl_pct: float,
        closed_at_ms: int | None = None,
    ) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                """UPDATE positions
                      SET status='closed', exit_price=?, pnl_pct=?,
                          closed_at_ms=?, updated_at=?
                    WHERE id=?""",
                (
                    float(exit_price), float(pnl_pct),
                    closed_at_ms or _now_ms(), _utc_iso(), position_id,
                ),
            )

    def _row_to_position(self, r: sqlite3.Row) -> Position:
        opened = datetime.fromtimestamp(r["opened_at_ms"] / 1000.0, tz=UTC)
        closed = (
            datetime.fromtimestamp(r["closed_at_ms"] / 1000.0, tz=UTC)
            if r["closed_at_ms"]
            else None
        )
        return Position(
            symbol=r["symbol"],
            side=Side(r["side"]),
            size=float(r["size"]),
            entry_price=float(r["entry_price"]),
            stop=float(r["stop"]),
            take=float(r["take"]),
            opened_at=opened,
            status=TradeStatus(r["status"]),
            closed_at=closed,
            exit_price=float(r["exit_price"]) if r["exit_price"] is not None else None,
            pnl_pct=float(r["pnl_pct"]) if r["pnl_pct"] is not None else None,
        )


# --------------------------------------------------------------------------- #
# Trade repository
# --------------------------------------------------------------------------- #
class TradeRepository:
    def __init__(self, db: Database) -> None:
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
        ts_ms: int | None = None,
        external_id: str | None = None,
    ) -> int:
        with self._db.transaction() as conn:
            cur = conn.execute(
                """INSERT INTO trades
                   (position_id, symbol, side, order_type, size, price, status,
                    ts_ms, mode, external_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    int(position_id),
                    symbol,
                    side.value,
                    order_type.value,
                    float(size),
                    float(price),
                    status.value,
                    ts_ms or _now_ms(),
                    mode.value,
                    external_id,
                ),
            )
            return _lastrowid(cur)

    def list_for_position(self, position_id: int) -> list[Trade]:
        rows = self._db.conn.execute(
            """SELECT id, position_id, symbol, side, order_type, size, price,
                      status, ts_ms, mode, external_id
                 FROM trades
                WHERE position_id=?
                ORDER BY ts_ms, id""",
            (int(position_id),),
        ).fetchall()
        return [self._row_to_trade(r) for r in rows]

    def latest_for_symbol(self, symbol: str, limit: int = 20) -> list[Trade]:
        limit = max(1, min(int(limit), 1000))
        rows = self._db.conn.execute(
            """SELECT id, position_id, symbol, side, order_type, size, price,
                      status, ts_ms, mode, external_id
                 FROM trades
                WHERE symbol=?
                ORDER BY ts_ms DESC, id DESC
                LIMIT ?""",
            (symbol, limit),
        ).fetchall()
        return [self._row_to_trade(r) for r in rows]

    @staticmethod
    def _row_to_trade(r: sqlite3.Row) -> Trade:
        return Trade(
            id=int(r["id"]),
            position_id=int(r["position_id"]),
            symbol=r["symbol"],
            side=Side(r["side"]),
            order_type=OrderType(r["order_type"]),
            size=float(r["size"]),
            price=float(r["price"]),
            status=OrderStatus(r["status"]),
            ts_ms=int(r["ts_ms"]),
            mode=Mode(r["mode"]),
            external_id=r["external_id"],
        )


# --------------------------------------------------------------------------- #
# Equity repository
# --------------------------------------------------------------------------- #
class EquityRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def insert(
        self,
        currency: str,
        equity: float,
        drawdown_pct: float | None,
        mode: Mode,
        ts_ms: int | None = None,
    ) -> int:
        with self._db.transaction() as conn:
            cur = conn.execute(
                """INSERT INTO equity (ts_ms, currency, equity, drawdown_pct, mode)
                   VALUES (?, ?, ?, ?, ?)""",
                (ts_ms or _now_ms(), currency, float(equity), drawdown_pct, mode.value),
            )
            return _lastrowid(cur)

    def latest(self, mode: Mode) -> tuple[float, float] | None:
        """Return (equity, drawdown_pct) of the most recent record for a mode."""
        row = self._db.conn.execute(
            "SELECT equity, drawdown_pct FROM equity WHERE mode=? ORDER BY ts_ms DESC LIMIT 1",
            (mode.value,),
        ).fetchone()
        if not row:
            return None
        return float(row["equity"]), (
            float(row["drawdown_pct"]) if row["drawdown_pct"] is not None else 0.0
        )


# --------------------------------------------------------------------------- #
# Repository registry — one entry point to all repositories
# --------------------------------------------------------------------------- #
class Repositories:
    """Convenience aggregate of all repositories over one Database."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.candles = CandleRepository(db)
        self.signals = SignalRepository(db)
        self.decisions = DecisionRepository(db)
        self.positions = PositionRepository(db)
        self.trades = TradeRepository(db)
        self.equity = EquityRepository(db)
