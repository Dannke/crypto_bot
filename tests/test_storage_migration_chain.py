"""Цепочка миграций должна реально выполняться, а не только отчитываться.

Дефект, который эти тесты закрывают: ``_migrate()`` сначала прогонял
``migrations.sql``, а тот в конце безусловно штампует ``schema_version='10'``.
Затем вызывались ``_migrate_v2()``…``_migrate_v10()``, и каждая начиналась с
перечитывания версии из БД — то есть видела уже финальное значение и выходила
рано. Вся цепочка не выполнялась НИКОГДА, при этом ``schema_version()``
возвращал ``"10"``: база отчитывалась как мигрированная, не будучи ею.

На новой БД это незаметно, потому что ``CREATE TABLE`` из ``migrations.sql``
сразу создаёт целевую схему. Ломалась ровно рабочая ``data/crypto_bot.db``:
пересоздание ``positions`` с новым CHECK не происходило, и первый же выход
``reversion``/``time_stop`` падал с ``IntegrityError`` — уже ПОСЛЕ того, как
in-memory трекер закрыл позицию.

Поэтому проверка идёт не против заранее собранной целевой схемы, а против БД,
собранной как её собрал бы предыдущий релиз.
"""
from __future__ import annotations

import sqlite3

import pytest

from crypto_bot.storage.db import Database

# CHECK до v10 — без 'reversion' и 'time_stop'.
LEGACY_CLOSED_BY = (
    "CHECK (closed_by IS NULL OR closed_by IN "
    "('stop_loss','take_profit','manual','signal','emergency_drawdown',"
    "'timeout_fallback','rebalance'))"
)

NEW_CLOSE_REASONS = ("reversion", "time_stop")


def _build_legacy_db(path, *, stamped_version: str, with_trades: bool) -> None:
    """Собрать БД так, как её оставил бы предыдущий релиз."""
    conn = sqlite3.connect(path)
    conn.executescript(f"""
        CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE positions (
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
            closed_by     TEXT    {LEGACY_CLOSED_BY},
            opened_at_ms  INTEGER NOT NULL,
            closed_at_ms  INTEGER,
            exit_price    REAL,
            pnl_pct       REAL,
            mode          TEXT    NOT NULL CHECK (mode IN ('signal_only','paper','live')),
            created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            updated_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        );
        INSERT INTO positions (symbol, timeframe, side, size, entry_price, stop, take,
                               status, opened_at_ms, mode)
        VALUES ('BTC/USDT', '1h', 'LONG', 0.5, 100.0, 95.0, 110.0, 'open', 1700000000000, 'paper');
        INSERT INTO schema_meta (key, value) VALUES ('schema_version', '{stamped_version}');
    """)
    if with_trades:
        # Ровно та ссылка, из-за которой DROP TABLE positions падает при
        # PRAGMA foreign_keys = ON.
        # Колонки как в реальной схеме: migrations.sql строит по trades индексы,
        # и урезанная заглушка уронила бы прогон на "no such column: ts_ms".
        conn.executescript("""
            CREATE TABLE trades (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                position_id  INTEGER NOT NULL,
                symbol       TEXT    NOT NULL,
                side         TEXT    NOT NULL CHECK (side IN ('LONG','SHORT')),
                order_type   TEXT    NOT NULL CHECK (order_type IN ('market','limit')),
                size         REAL    NOT NULL,
                price        REAL    NOT NULL,
                status       TEXT    NOT NULL DEFAULT 'filled'
                             CHECK (status IN ('pending','filled','cancelled','rejected')),
                ts_ms        INTEGER NOT NULL,
                mode         TEXT    NOT NULL CHECK (mode IN ('signal_only','paper','live')),
                external_id  TEXT,
                created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                FOREIGN KEY (position_id) REFERENCES positions(id) ON DELETE RESTRICT
            );
            INSERT INTO trades (position_id, symbol, side, order_type, size, price, ts_ms, mode)
            VALUES (1, 'BTC/USDT', 'LONG', 'market', 0.5, 100.0, 1700000000000, 'paper');
        """)
    conn.commit()
    conn.close()


def _closed_by_accepts(db: Database, reason: str) -> bool:
    """Принимает ли текущая схема это значение closed_by."""
    try:
        with db.conn:
            db.conn.execute(
                """INSERT INTO positions (symbol, timeframe, side, size, entry_price,
                                          stop, take, status, closed_by, opened_at_ms, mode)
                   VALUES ('ETH/USDT','1h','LONG',1.0,100.0,95.0,110.0,'closed',?,1700000000000,'paper')""",
                (reason,),
            )
    except sqlite3.IntegrityError:
        return False
    return True


class TestLegacyDatabaseIsActuallyMigrated:
    @pytest.mark.parametrize("reason", NEW_CLOSE_REASONS)
    def test_existing_db_accepts_new_close_reason_after_migrate(self, tmp_path, reason) -> None:
        """Главный guard: БД предыдущего релиза реально доезжает до v10."""
        path = tmp_path / "legacy.db"
        _build_legacy_db(path, stamped_version="9", with_trades=False)

        db = Database(path)
        try:
            assert db.schema_version() == "10"
            assert _closed_by_accepts(db, reason), (
                f"closed_by={reason!r} отклонён CHECK-констрейнтом: миграция v10 "
                "отчиталась об успехе, не пересоздав таблицу"
            )
        finally:
            db.close()

    def test_legacy_check_really_rejected_before_migration(self, tmp_path) -> None:
        """Контроль: до миграции старая схема эти значения действительно не принимает.

        Без него предыдущий тест мог бы проходить на схеме, которая приняла бы
        что угодно, и ничего не доказывать.
        """
        path = tmp_path / "legacy_control.db"
        _build_legacy_db(path, stamped_version="9", with_trades=False)

        conn = sqlite3.connect(path)
        try:
            with pytest.raises(sqlite3.IntegrityError):
                with conn:
                    conn.execute(
                        """INSERT INTO positions (symbol, timeframe, side, size, entry_price,
                                                  stop, take, status, closed_by, opened_at_ms, mode)
                           VALUES ('ETH/USDT','1h','LONG',1.0,100.0,95.0,110.0,'closed','reversion',1,'paper')"""
                    )
        finally:
            conn.close()

    def test_migration_survives_foreign_key_references(self, tmp_path) -> None:
        """Пересоздание positions при живых ссылках из trades (ON DELETE RESTRICT)."""
        path = tmp_path / "legacy_fk.db"
        _build_legacy_db(path, stamped_version="9", with_trades=True)

        db = Database(path)
        try:
            assert db.schema_version() == "10"
            assert _closed_by_accepts(db, "reversion")
            # Ссылающаяся строка должна пережить своп таблицы.
            assert db.conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1
            # И проверка ключей должна быть возвращена, а не оставлена выключенной.
            assert db.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        finally:
            db.close()

    def test_existing_rows_are_preserved(self, tmp_path) -> None:
        path = tmp_path / "legacy_rows.db"
        _build_legacy_db(path, stamped_version="9", with_trades=False)

        db = Database(path)
        try:
            row = db.conn.execute(
                "SELECT symbol, side, entry_price FROM positions WHERE id=1"
            ).fetchone()
            assert row is not None, "своп таблицы потерял существующие строки"
            assert row["symbol"] == "BTC/USDT"
            assert row["entry_price"] == 100.0
        finally:
            db.close()


class TestFreshDatabase:
    @pytest.mark.parametrize("reason", NEW_CLOSE_REASONS)
    def test_new_db_accepts_new_close_reasons(self, tmp_path, reason) -> None:
        db = Database(tmp_path / "fresh.db")
        try:
            assert db.schema_version() == "10"
            assert _closed_by_accepts(db, reason)
        finally:
            db.close()

    def test_reopening_is_idempotent(self, tmp_path) -> None:
        """Повторное открытие не должно заново гонять цепочку и ломать данные."""
        path = tmp_path / "reopen.db"
        first = Database(path)
        first.close()

        second = Database(path)
        try:
            assert second.schema_version() == "10"
            assert _closed_by_accepts(second, "time_stop")
        finally:
            second.close()
