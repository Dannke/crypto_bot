"""
check_validation_progress.py

Диагностика прогресса накопления decisions-журнала для будущей
кросс-валидации Backtester'а (Stage 2 из ТЗ Backtest Engine).

Работает только с тем, что уже есть на диске — реальным crypto_bot.db и
логом бота. Никаких внешних зависимостей, только stdlib — можно запускать
сразу, без pip install и без установки самого пакета crypto_bot.

Схема таблицы decisions заранее не предполагается жёстко: скрипт сначала
читает PRAGMA table_info и подстраивается (например, если колонки
timeframe нет вовсе, или timestamp называется ts_ms) — вместо того чтобы
упасть с невнятной sqlite3.OperationalError.

Запуск:
    python check_validation_progress.py
    python check_validation_progress.py --db data/crypto_bot.db --log logs/crypto_bot.log
    python check_validation_progress.py --min-bars 50 --loop-interval 60

Результат печатается в консоль И сохраняется в
validation_report_<timestamp>.txt рядом со скриптом — этот файл удобно
целиком прислать для разбора.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
# Константы
# --------------------------------------------------------------------------- #

TF_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "12h": 43200,
    "1d": 86400,
}

READY_THRESHOLD_DEFAULT = 50   # используется только если --min-bars передан явно
GETTING_THERE = 20              # >= это -> "рано, но не пусто" (при отсутствии TF в таблице ниже)

# Для проверки КОРРЕКТНОСТИ (совпадает ли решение бэктеста с живым ботом на
# том же баре) не нужна статистическая выборка — нужно всего несколько
# честных баров, чтобы либо увидеть расхождение, либо получить разумную
# уверенность в его отсутствии. Пороги ниже — минимально достаточные для
# старта первой сверки, не "достаточно для расчёта Sharpe".
TF_MIN_BARS = {
    "1m": 30, "3m": 30, "15m": 15, "30m": 10,
    "1h": 8, "2h": 6, "4h": 5, "6h": 4, "12h": 3, "1d": 3,
}
TF_MIN_BARS_FALLBACK = 20

TIMESTAMP_CANDIDATES = ["timestamp", "ts_ms", "ts"]
TIMEFRAME_CANDIDATES = ["timeframe", "tf"]
REASON_CANDIDATES = ["reason", "reject_reason"]

CYCLE_RE = re.compile(r"Starting scan cycle at (\S+)")


def tf_seconds(tf: str) -> int | None:
    return TF_SECONDS.get((tf or "").strip().lower())


def required_bars_for(timeframe: str, override: int | None) -> int:
    if override is not None:
        return override
    return TF_MIN_BARS.get((timeframe or "").strip().lower(), TF_MIN_BARS_FALLBACK)


def verdict(distinct_bars: int, required: int) -> str:
    if distinct_bars >= required:
        return f"ГОТОВО для первой сверки (>= {required})"
    if distinct_bars >= max(3, required // 2):
        return "рано, но не пусто"
    return "слишком рано"


def fmt_ts_ms(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def pick_column(columns: list[str], candidates: list[str]) -> str | None:
    for c in candidates:
        if c in columns:
            return c
    return None


def get_columns(con: sqlite3.Connection, table: str) -> list[str]:
    try:
        cur = con.execute(f"PRAGMA table_info({table})")
        return [row[1] for row in cur.fetchall()]
    except sqlite3.OperationalError:
        return []


# --------------------------------------------------------------------------- #
# Запросы к decisions (адаптивные под реальную схему)
# --------------------------------------------------------------------------- #

@dataclass
class SymbolTfRow:
    symbol: str
    timeframe: str
    raw_rows: int
    distinct_bars: int
    accepted_n: int
    first_ts_ms: int
    last_ts_ms: int


@dataclass
class DecisionsQueryResult:
    table_found: bool = False
    columns: list[str] | None = None
    ts_col: str | None = None
    tf_col: str | None = None
    reason_col: str | None = None
    rows: list[SymbolTfRow] = field(default_factory=list)
    reject_reasons: list[tuple[str, str, int]] = field(default_factory=list)
    accepted: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


def query_decisions(db_path: Path, accepted_limit: int = 20) -> DecisionsQueryResult:
    result = DecisionsQueryResult()
    if not db_path.exists():
        result.error = f"файл БД не найден: {db_path}"
        return result

    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        columns = get_columns(con, "decisions")
        if not columns:
            result.error = "таблица 'decisions' не найдена (или БД пустая/повреждена)"
            return result

        result.table_found = True
        result.columns = columns

        ts_col = pick_column(columns, TIMESTAMP_CANDIDATES)
        tf_col = pick_column(columns, TIMEFRAME_CANDIDATES)
        result.ts_col, result.tf_col = ts_col, tf_col

        if ts_col is None:
            result.error = (
                f"не нашёл колонку с таймстемпом среди {columns} "
                f"(искал одну из {TIMESTAMP_CANDIDATES})"
            )
            return result

        has_accepted = "accepted" in columns
        reason_col = pick_column(columns, REASON_CANDIDATES)
        result.reason_col = reason_col
        has_reason = reason_col is not None
        has_score = "score" in columns
        has_signal = "signal" in columns

        select_tf = f"{tf_col} AS timeframe" if tf_col else "NULL AS timeframe"
        group_tf = f", {tf_col}" if tf_col else ""
        accepted_expr = f"SUM(CASE WHEN {'accepted' if has_accepted else '1'} THEN 1 ELSE 0 END)" if has_accepted else "NULL"

        query = f"""
            SELECT symbol, {select_tf},
                   COUNT(*) AS raw_rows,
                   COUNT(DISTINCT {ts_col}) AS distinct_bars,
                   {accepted_expr} AS accepted_n,
                   MIN({ts_col}) AS first_ts,
                   MAX({ts_col}) AS last_ts
            FROM decisions
            GROUP BY symbol{group_tf}
            ORDER BY symbol, distinct_bars DESC
        """
        rows_raw: list[sqlite3.Row] = con.execute(query).fetchall()
        rows = [
            SymbolTfRow(
                symbol=str(r["symbol"]),
                timeframe=str(r["timeframe"] or "?"),
                raw_rows=int(r["raw_rows"]),
                distinct_bars=int(r["distinct_bars"]),
                accepted_n=int(r["accepted_n"] or 0),
                first_ts_ms=int(r["first_ts"]),
                last_ts_ms=int(r["last_ts"]),
            )
            for r in rows_raw
        ]
        result.rows = rows

        if has_reason:
            rq = f"""
                SELECT {select_tf}, COALESCE({reason_col}, 'none') AS reason, COUNT(*) AS n
                FROM decisions
                WHERE {'accepted = 0' if has_accepted else '1=1'}
                GROUP BY timeframe, reason
                ORDER BY timeframe, n DESC
            """
            reject_raw: list[sqlite3.Row] = con.execute(rq).fetchall()
            result.reject_reasons = [
                (str(row["timeframe"] or "?"), str(row["reason"]), int(row["n"]))
                for row in reject_raw
            ]

        if has_accepted:
            score_expr = "score" if has_score else "NULL AS score"
            signal_expr = "signal" if has_signal else "NULL AS signal"
            aq = f"""
                SELECT symbol, {select_tf}, {ts_col} AS ts, {score_expr}, {signal_expr}
                FROM decisions
                WHERE accepted = 1
                ORDER BY {ts_col} DESC
                LIMIT ?
            """
            accepted_raw: list[sqlite3.Row] = con.execute(aq, (accepted_limit,)).fetchall()
            result.accepted = [dict(r) for r in accepted_raw]

    except sqlite3.OperationalError as exc:
        result.error = f"ошибка SQL: {exc}"
    finally:
        con.close()

    return result


# --------------------------------------------------------------------------- #
# Анализ лога: регулярность циклов + ошибки/предупреждения
# --------------------------------------------------------------------------- #

@dataclass
class LogAnalysis:
    found: bool = False
    cycle_count: int = 0
    last_cycle_ts: datetime | None = None
    gaps: list[tuple[datetime, datetime, float]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def analyze_log(log_path: Path, expected_interval_s: float) -> LogAnalysis:
    result = LogAnalysis()
    if not log_path.exists():
        return result

    result.found = True
    cycle_times: list[datetime] = []

    with log_path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            m = CYCLE_RE.search(line)
            if m:
                raw = m.group(1)
                try:
                    dt = datetime.fromisoformat(raw)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=UTC)
                    cycle_times.append(dt)
                except ValueError:
                    pass

            if "ERROR" in line:
                result.errors.append(line.rstrip())
            elif "WARNING" in line:
                result.warnings.append(line.rstrip())

    result.cycle_count = len(cycle_times)
    if cycle_times:
        result.last_cycle_ts = cycle_times[-1]

    threshold = expected_interval_s * 2.5  # запас на джиттер / rate-limit
    for prev, cur in zip(cycle_times, cycle_times[1:], strict=False):
        gap = (cur - prev).total_seconds()
        if gap > threshold:
            result.gaps.append((prev, cur, gap))

    result.errors = result.errors[-30:]
    result.warnings = result.warnings[-30:]
    return result


# --------------------------------------------------------------------------- #
# Сборка отчёта
# --------------------------------------------------------------------------- #

def build_report(
    db_path: Path,
    log_path: Path,
    min_bars_override: int | None,
    loop_interval: float,
) -> str:
    now = datetime.now(tz=UTC)
    lines: list[str] = []
    w: Callable[[str], None] = lines.append

    w("=" * 78)
    w(f"ОТЧЁТ О НАКОПЛЕНИИ ДАННЫХ ДЛЯ КРОСС-ВАЛИДАЦИИ  ({now.strftime('%Y-%m-%d %H:%M:%S UTC')})")
    w(f"БД:  {db_path}")
    w(f"Лог: {log_path}")
    w("=" * 78)

    dq = query_decisions(db_path)

    w("")
    w("--- 0. Реальная схема таблицы decisions ---")
    if dq.error and not dq.table_found:
        w(f"  !! {dq.error}")
    else:
        w(f"  Колонки: {dq.columns}")
        w(f"  Колонка времени: {dq.ts_col!r}   Колонка таймфрейма: {dq.tf_col!r}   Колонка причины: {dq.reason_col!r}")
        if dq.tf_col is None:
            w("  !! В decisions нет отдельной колонки таймфрейма — группировка только по symbol.")

    w("")
    w("--- 1. Накопление по (symbol, timeframe) ---")
    rows = dq.rows
    if dq.error and dq.table_found is False:
        w("  (см. ошибку выше)")
    elif not rows:
        w("  (нет строк в decisions — бот ещё не писал, либо путь к БД неверный)")
    else:
        header = (
            f"{'symbol':<10} {'tf':<6} {'raw':>6} {'distinct':>9} {'accepted':>9} "
            f"{'first_bar':<22} {'last_bar':<22} {'age':>9}  verdict"
        )
        w(header)
        w("-" * len(header))
        for r in rows:
            age_s = (now - datetime.fromtimestamp(r.last_ts_ms / 1000, tz=UTC)).total_seconds()
            age_str = f"{age_s/60:.0f}м" if age_s < 3600 else f"{age_s/3600:.1f}ч"
            required = required_bars_for(r.timeframe, min_bars_override)
            v = verdict(r.distinct_bars, required)
            stale_flag = ""
            tfs = tf_seconds(r.timeframe)
            if tfs and age_s > tfs * 3:
                stale_flag = "  !! ПОСЛЕДНИЙ БАР УСТАРЕЛ — проверьте, не встал ли бот"

            bar_math_flag = ""
            if tfs:
                elapsed_s = max((r.last_ts_ms - r.first_ts_ms) / 1000.0, 0.0)
                max_possible_bars = int(elapsed_s // tfs) + 2  # +2 запас на граничные эффекты
                if r.distinct_bars > max_possible_bars:
                    bar_math_flag = (
                        f"  !! distinct_bars ({r.distinct_bars}) БОЛЬШЕ физически возможного "
                        f"числа закрытий {r.timeframe}-бара за это окно (~{max_possible_bars}) — "
                        f"ts_ms похоже отражает время ЦИКЛА, а не время закрытия бара"
                    )
            w(
                f"{r.symbol:<10} {r.timeframe:<6} {r.raw_rows:>6} {r.distinct_bars:>9} "
                f"{r.accepted_n:>9} {fmt_ts_ms(r.first_ts_ms):<22} {fmt_ts_ms(r.last_ts_ms):<22} "
                f"{age_str:>9}  {v}{stale_flag}{bar_math_flag}"
            )

    w("")
    w("--- 2. Разнообразие причин отказа (accepted=0) ---")
    reasons = dq.reject_reasons
    if not reasons:
        w("  (нет данных, либо в схеме нет колонки reason/accepted)")
    else:
        cur_tf = None
        for tf, reason, n in reasons:
            if tf != cur_tf:
                w(f"  [{tf}]")
                cur_tf = tf
            w(f"    {reason:<28} {n}")

    w("")
    w("--- 3. Последние принятые сигналы (accepted=1) ---")
    accepted = dq.accepted
    if not accepted:
        w("  (пока ни одного — не тревога, min_score порог обычно строгий)")
    else:
        for row in accepted:
            ts_val = row.get("ts")
            sym_val = str(row.get("symbol", ""))
            tf_val = str(row.get("timeframe") or "")
            sig_val = str(row.get("signal") or "")
            score_val = row.get("score")
            score_str = f"{score_val}" if score_val is not None else "None"
            ts_str = str(ts_val) if ts_val is not None else ""
            formatted_ts = fmt_ts_ms(int(ts_str)) if ts_str else ""
            w(
                f"    {formatted_ts}  {sym_val:<10} "
                f"{tf_val:<6} signal={sig_val:<6} score={score_str}"
            )

    w("")
    w("--- 4. Анализ лога: регулярность циклов и ошибки ---")
    log_result = analyze_log(log_path, loop_interval)
    if not log_result.found:
        w(f"  (лог не найден: {log_path})")
    else:
        w(f"  Обнаружено циклов сканирования: {log_result.cycle_count}")
        if log_result.last_cycle_ts:
            age = (now - log_result.last_cycle_ts.astimezone(UTC)).total_seconds()
            w(f"  Последний цикл начат: {log_result.last_cycle_ts}  ({age/60:.1f} мин назад)")
            if age > loop_interval * 3:
                w("  !! Последний цикл был давно — похоже, бот сейчас не крутится (проверьте процесс)")

        if log_result.gaps:
            w(f"  Разрывы между циклами длиннее {loop_interval*2.5:.0f}с (ожидался ~{loop_interval:.0f}с):")
            for prev, cur, gap in log_result.gaps[-10:]:
                w(f"    {prev} -> {cur}   разрыв {gap:.0f}с")
        else:
            w("  Разрывов между циклами не обнаружено — цикл идёт стабильно.")

        w(f"  ERROR-строк (последние {len(log_result.errors)}, показаны до 10):")
        for e in log_result.errors[-10:]:
            w(f"    {e}")
        if not log_result.errors:
            w("    (нет)")

        w(f"  WARNING-строк (последние {len(log_result.warnings)}, показаны до 10):")
        for wl in log_result.warnings[-10:]:
            w(f"    {wl}")
        if not log_result.warnings:
            w("    (нет)")

    w("")
    w("--- 5. Итог ---")
    ready = [r for r in rows if r.distinct_bars >= required_bars_for(r.timeframe, min_bars_override)]
    if ready:
        w("  Готовы для первой кросс-сверки (порог зависит от TF, см. секцию 1):")
        for r in ready:
            w(f"    {r.symbol} {r.timeframe}: {r.distinct_bars} баров")
    else:
        w("  Пока ни одна пара (symbol, timeframe) не набрала нужный минимум баров.")

    w("=" * 78)
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/crypto_bot.db", help="путь к SQLite БД")
    parser.add_argument("--log", default="logs/crypto_bot.log", help="путь к лог-файлу")
    parser.add_argument(
        "--min-bars", type=int, default=None,
        help="явный порог 'готово' для ВСЕХ TF одинаково. Без флага — разумные дефолты по каждому TF (15m=15, 1h=8, 4h=5 и т.д.)",
    )
    parser.add_argument("--loop-interval", type=float, default=60.0, help="ожидаемый loop_interval_seconds из конфига")
    parser.add_argument("--out", default=None, help="куда сохранить отчёт (по умолчанию validation_report_<ts>.txt)")
    args = parser.parse_args()

    db_path = Path(args.db)
    log_path = Path(args.log)

    report = build_report(db_path, log_path, args.min_bars, args.loop_interval)
    print(report)

    out_path = Path(args.out) if args.out else Path(
        f"validation_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    )
    out_path.write_text(report, encoding="utf-8")
    print(f"\nОтчёт сохранён в: {out_path.resolve()}")


if __name__ == "__main__":
    main()