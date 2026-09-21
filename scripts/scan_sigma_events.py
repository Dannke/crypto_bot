#!/usr/bin/env python3
"""Частота срабатывания сигма-порога и масштабное соотношение std(Nh)/std(1h).

Отвечает на два вопроса, обязательных для любой фичи, выраженной в сигмах
(см. `.claude/skills/research-methodology/SKILL.md`, раздел про статистическую
валидацию):

1. Соответствует ли реализация заявленной статистике. Текущий код mean_reversion
   стандартизует N-часовую доходность по std ОДНОБАРНЫХ доходностей, что завышает
   z примерно в sqrt(N) раз. Скрипт печатает измеренное отношение рядом с sqrt(N).
2. Как часто порог реально срабатывает — в обеих версиях z, на сетке ребалансировки.

ВАЖНО, чем результат НЕ является. Считаются СЫРЫЕ пересечения порога по каждому
символу независимо. Ни селекционного капа, ни risk.max_positions, ни
min_expected_edge_bps, ни режимного гейтинга, ни исключения уже открытых позиций
здесь нет. Это верхняя граница числа возможностей, а не оценка n_trades, и
сравнивать её с порогом decision rule напрямую нельзя.

Отдельная оговорка про сам порог: окно WINDOW баров при горизонте LOOKBACK даёт
лишь ~WINDOW/LOOKBACK эффективно независимых наблюдений, поэтому оценка sigma
зашумлена и порог «две сигмы» бьётся чаще теоретического даже после исправления
sqrt(N)-эффекта.

Запуск (из корня репозитория, читает data/crypto_bot.db):

    python scripts/scan_sigma_events.py
"""
from __future__ import annotations

import datetime as dt
import statistics as st

from crypto_bot.simulation.walk_forward import fetch_all_candles

SYMS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT",
        "ADA/USDT", "DOGE/USDT", "POL/USDT", "AVAX/USDT"]
WINDOW = 48       # zscore_window_bars
LOOKBACK = 8      # signal_lookback 8h при timeframe 1h
ENTRY = 2.0       # зарегистрированный порог входа
TICK = 12         # rebalance_hours

TRAIN_LO = dt.datetime(2025, 1, 1, tzinfo=dt.UTC)
TRAIN_HI = dt.datetime(2025, 7, 2, tzinfo=dt.UTC)

WINDOWS = {
    "вся история": (None, None),
    "train v4 (2025-01-01..2025-07-02)": (TRAIN_LO, TRAIN_HI),
    "test v4 (2025-09-12..2025-12-31)": (
        dt.datetime(2025, 9, 12, 19, tzinfo=dt.UTC),
        dt.datetime(2025, 12, 31, tzinfo=dt.UTC)),
}


def zseries(closes: list[float]) -> list[tuple[int, float, float]]:
    """(индекс бара, z как в коде сейчас, z при согласованном горизонте).

    Окно 8-часовых доходностей берётся строго до текущего бара, без заглядывания
    вперёд: последний элемент `w8` опирается на закрытие бара i-1.
    """
    out: list[tuple[int, float, float]] = []
    r1 = [closes[i]/closes[i-1] - 1.0 for i in range(1, len(closes))]
    r8 = [closes[i]/closes[i-LOOKBACK] - 1.0 for i in range(LOOKBACK, len(closes))]
    start = WINDOW + LOOKBACK + 1
    for i in range(start, len(closes)):
        cur = closes[i]/closes[i-LOOKBACK] - 1.0
        w1 = r1[i-WINDOW:i]                      # как в коде: часовые доходности
        w8 = r8[i-LOOKBACK-WINDOW:i-LOOKBACK]    # согласованно: 8-часовые
        if len(w1) < WINDOW or len(w8) < WINDOW:
            continue
        s1, s8 = st.pstdev(w1), st.pstdev(w8)
        if s1 <= 0 or s8 <= 0:
            continue
        out.append((i, (cur - st.mean(w1))/s1, (cur - st.mean(w8))/s8))
    return out


def print_crossings(allc: dict[str, list]) -> None:
    for label, (lo, hi) in WINDOWS.items():
        lo_ms = int(lo.timestamp()*1000) if lo else 0
        hi_ms = int(hi.timestamp()*1000) if hi else 2**63 - 1
        tot_now = tot_true = tot_ticks = 0
        rows = []
        for sym in SYMS:
            bars = [c for c in allc[sym] if lo_ms <= c.timestamp < hi_ms]
            if len(bars) < WINDOW + LOOKBACK + 2:
                rows.append((sym, 0, 0, 0))
                continue
            zs = zseries([c.close for c in bars])
            ticks = [(i, a, b) for i, a, b in zs if i % TICK == 0]
            now = sum(1 for _i, a, _b in ticks if abs(a) >= ENTRY)
            true = sum(1 for _i, _a, b in ticks if abs(b) >= ENTRY)
            rows.append((sym, len(ticks), now, true))
            tot_ticks += len(ticks)
            tot_now += now
            tot_true += true
        print(f"\n=== {label} ===")
        print(f"{'символ':12} {'тиков':>7} {'|z|>=2 как в коде':>18} {'|z|>=2 верно':>14}")
        for sym, t, n, tr in rows:
            print(f"{sym:12} {t:7} {n:18} {tr:14}")
        print(f"{'ИТОГО':12} {tot_ticks:7} {tot_now:18} {tot_true:14}")
        if tot_true:
            print(f"во сколько раз меньше настоящих событий: {tot_now/tot_true:.1f}x")
            print(f"частота настоящих: {tot_true}/{tot_ticks} = {tot_true/tot_ticks*100:.2f}%")


def print_std_ratio(allc: dict[str, list]) -> None:
    """Масштабное соотношение std(8h)/std(1h) — проверка согласованности статистики."""
    lo_ms = int(TRAIN_LO.timestamp()*1000)
    hi_ms = int(TRAIN_HI.timestamp()*1000)
    print("\n=== std(8h)/std(1h), train-сегмент, по всему окну целиком ===")
    ratios = []
    for sym in SYMS:
        cl = [c.close for c in allc[sym] if lo_ms <= c.timestamp < hi_ms]
        r1 = [cl[i]/cl[i-1] - 1.0 for i in range(1, len(cl))]
        r8 = [cl[i]/cl[i-LOOKBACK] - 1.0 for i in range(LOOKBACK, len(cl))]
        a, b = st.pstdev(r1), st.pstdev(r8)
        ratios.append(b/a)
        print(f"  {sym:12} std1h={a:.6f} std8h={b:.6f} отношение={b/a:.4f}")
    print(f"  среднее отношение = {st.mean(ratios):.4f}")
    print(f"  sqrt({LOOKBACK}) для случайного блуждания = {LOOKBACK**0.5:.4f}")

    # Альтернативная методология: усреднение отношения по каждому скользящему окну.
    # Даёт заметно меньшее значение, потому что пооконные оценки sigma зашумлены —
    # 48 перекрывающихся 8-часовых доходностей дают ~6 независимых наблюдений.
    print("\n=== то же, но усреднением по каждому скользящему окну ===")
    rolling = []
    for sym in SYMS:
        cl = [c.close for c in allc[sym] if lo_ms <= c.timestamp < hi_ms]
        r1 = [cl[i]/cl[i-1] - 1.0 for i in range(1, len(cl))]
        r8 = [cl[i]/cl[i-LOOKBACK] - 1.0 for i in range(LOOKBACK, len(cl))]
        per_window = []
        for i in range(WINDOW + LOOKBACK + 1, len(cl)):
            w1, w8 = r1[i-WINDOW:i], r8[i-LOOKBACK-WINDOW:i-LOOKBACK]
            if len(w1) < WINDOW or len(w8) < WINDOW:
                continue
            a = st.pstdev(w1)
            if a > 0:
                per_window.append(st.pstdev(w8)/a)
        if per_window:
            rolling.append(st.mean(per_window))
    print(f"  среднее отношение = {st.mean(rolling):.4f}")


def main() -> None:
    allc = fetch_all_candles("data/crypto_bot.db", SYMS, "1h")
    print_std_ratio(allc)
    print_crossings(allc)


if __name__ == "__main__":
    main()
