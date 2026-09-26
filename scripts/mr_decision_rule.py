#!/usr/bin/env python3
"""Decision rule MR цикла 2 — механически, из БД walk-forward прогона.

Буквальная формулировка — в pre-registration цикла; структура — из
docs/research/mean-reversion/cycle-2/1-signal-definition.md, п. 6.5. Скрипт считает все четыре
условия из БД, которые scripts/walk_forward.py сохраняет с --db-dir:

    1. Sharpe(test)       > 0
    2. Sharpe(validation) > 0
    3. n_trades(test)     >= 200    (закрытые позиции test-прогона)
    4. Sharpe > 0 не менее чем в 2 из 3 под-окон test-сегмента

ACCEPT, если выполнены все четыре; иначе REJECT. Код возврата 0 = ACCEPT.

Какой Sharpe. Ряд — таблица equity БД прогона: бэктестер пишет туда ровно одну
mark-to-market точку на тик. Функция — crypto_bot.simulation.pnl.
sharpe_from_returns, та же, что у PnLSummary.sharpe_ratio. Но печатаемый
walk_forward.py PnLSummary.sharpe_ratio считается по другому ряду: в
PnLTracker.equity_history кроме тиков попадают записи close_position —
с настенным временем и эквити без нереализованного PnL остальных позиций. Для
вердикта используется только ряд тиков из БД; число из вывода walk-forward —
справочное. Единицы обоих: часовые доходности с дневным множителем sqrt(365).

Под-окна — три последовательных равных календарных части test-сегмента;
доходность между соседними тиками относится к части более позднего тика
(crypto_bot.simulation.pnl.subwindow_sharpes).

Запуск (метка — production или diagnostic_widened, вердикт — только production):

    python scripts/mr_decision_rule.py --label production \
        --validation-db data/backtests/mr_cycle2_production/validation.db \
        --test-db data/backtests/mr_cycle2_production/test.db \
        --test-start "2025-11-24 00:00" --test-end "2026-09-17 00:00"
"""
from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
from collections import Counter
from contextlib import closing

from crypto_bot.simulation.pnl import equity_returns, sharpe_from_returns, subwindow_sharpes

MIN_TRADES = 200
N_WINDOWS = 3
MIN_POSITIVE_WINDOWS = 2
HOUR_MS = 3_600_000


def utc_ms(value: str) -> int:
    moment = dt.datetime.strptime(value, "%Y-%m-%d %H:%M").replace(tzinfo=dt.UTC)
    return int(moment.timestamp() * 1000)


def fmt(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, dt.UTC).strftime("%Y-%m-%d %H:%M")


def tick_equity(db_path: str) -> list[tuple[int, float]]:
    with closing(sqlite3.connect(db_path)) as con:
        points = [(int(ts), float(eq)) for ts, eq in
                  con.execute("SELECT ts_ms, equity FROM equity ORDER BY ts_ms, id")]
    if not points:
        raise SystemExit(f"{db_path}: таблица equity пуста")
    duplicated = [ts for ts, n in Counter(ts for ts, _ in points).items() if n > 1]
    if duplicated:
        raise SystemExit(f"{db_path}: {len(duplicated)} тиков с несколькими записями эквити, "
                         f"первый {fmt(duplicated[0])} — определение Sharpe предполагает одну")
    return points


def closed_trades(db_path: str) -> int:
    with closing(sqlite3.connect(db_path)) as con:
        (count,) = con.execute("SELECT COUNT(*) FROM positions WHERE status = 'closed'").fetchone()
    return int(count)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--label", required=True, choices=("production", "diagnostic_widened"))
    parser.add_argument("--validation-db", required=True)
    parser.add_argument("--test-db", required=True)
    parser.add_argument("--test-start", required=True, metavar="'YYYY-MM-DD HH:MM'")
    parser.add_argument("--test-end", required=True, metavar="'YYYY-MM-DD HH:MM'")
    args = parser.parse_args()

    start_ms, end_ms = utc_ms(args.test_start), utc_ms(args.test_end)
    validation = tick_equity(args.validation_db)
    test = tick_equity(args.test_db)

    sharpe_test = sharpe_from_returns(equity_returns([e for _, e in test]))
    sharpe_validation = sharpe_from_returns(equity_returns([e for _, e in validation]))
    n_trades = closed_trades(args.test_db)
    windows = subwindow_sharpes(test, start_ms, end_ms, n_windows=N_WINDOWS)
    positive = sum(s > 0 for s in windows)

    conditions = [
        ("1. Sharpe(test) > 0", sharpe_test > 0, f"{sharpe_test:.4f}"),
        ("2. Sharpe(validation) > 0", sharpe_validation > 0, f"{sharpe_validation:.4f}"),
        (f"3. n_trades(test) >= {MIN_TRADES}", n_trades >= MIN_TRADES, str(n_trades)),
        (f"4. Sharpe > 0 в >= {MIN_POSITIVE_WINDOWS} из {N_WINDOWS} под-окон",
         positive >= MIN_POSITIVE_WINDOWS, f"{positive} из {N_WINDOWS}"),
    ]

    print(f"метка: {args.label}")
    print(f"validation БД: {args.validation_db}; тиков {len(validation)} "
          f"({fmt(validation[0][0])} .. {fmt(validation[-1][0])})")
    print(f"test БД: {args.test_db}; тиков {len(test)} ({fmt(test[0][0])} .. {fmt(test[-1][0])})")
    print(f"test-сегмент: {fmt(start_ms)} .. {fmt(end_ms)}")
    span = end_ms - start_ms
    for j, sharpe in enumerate(windows):
        lo = start_ms + (span * j // N_WINDOWS) // HOUR_MS * HOUR_MS
        hi = end_ms if j == N_WINDOWS - 1 else \
            start_ms + (span * (j + 1) // N_WINDOWS) // HOUR_MS * HOUR_MS
        print(f"  под-окно {j + 1}: {fmt(lo)} .. {fmt(hi)}  Sharpe = {sharpe:.4f}")
    for text, ok, value in conditions:
        print(f"{text:48} {value:>12}  {'PASS' if ok else 'FAIL'}")
    accepted = all(ok for _, ok, _ in conditions)
    print(f"ВЕРДИКТ ({args.label}): {'ACCEPT' if accepted else 'REJECT'}")
    return 0 if accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
