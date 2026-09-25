#!/usr/bin/env python3
"""Теория и синтетика для решения об определении z-score (Mean Reversion, цикл 2, Task 0).

Скрипт НЕ читает рыночные данные: всё, что он печатает, получено из формул или
из симуляции на синтетических рядах с фиксированным seed. Поэтому его вывод
можно использовать для выбора определения сигнала ДО любого измерения на
данных проекта — порядок «сначала решение, потом измерение» не нарушается.

Разделы вывода:

1. Хвост Стьюдента при эвристике n_eff = W/h — воспроизведение таблицы из
   docs/research/handoff_mr_v1v4_to_task0.md, раздел 3 (там числа приведены
   без команды; здесь они получают команду).
2. Эффективный размер выборки для оценки дисперсии: перекрывающиеся
   h-доходности, непересекающиеся h-доходности, однобарные доходности с
   масштабированием sqrt(h).
3. Монте-Карло: частота |z| >= k у разных определений z на трёх синтетических
   нулевых моделях (гауссов iid, Стьюдент iid, GARCH). Нулевая модель = ряд
   без какой-либо реверсии; «правильно откалиброванный» z на нём обязан
   давать частоту, близкую к теоретической.
4. Плановые границы сегментов нового цикла — реальная функция
   calendar_split на синтетической часовой сетке закреплённого окна
   (без цен: границы зависят только от временных меток).

Запуск (из корня репозитория):

    python scripts/sigma_definition_theory.py
"""
from __future__ import annotations

import datetime as dt
import math

import numpy as np

from crypto_bot.core.types import Candle
from crypto_bot.simulation.walk_forward import calendar_split

SEED = 20260925
N_HOURS = 300_000          # длина каждого синтетического ряда, часов
SIGMA_1H = 0.005           # масштаб часовой лог-доходности (z от него не зависит)
KS = (2.0, 2.5, 3.0)

# Закреплённое окно данных нового цикла и доли 3-way сплита.
WINDOW_START = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
WINDOW_END = dt.datetime(2026, 8, 7, tzinfo=dt.UTC)
TRAIN_FRACTION = 0.5
VALIDATION_FRACTION = 0.2


# --------------------------------------------------------------------------- #
# Хвосты распределений
# --------------------------------------------------------------------------- #
def normal_two_sided(k: float) -> float:
    return math.erfc(k / math.sqrt(2.0))


def _betacf(a: float, b: float, x: float) -> float:
    """Цепная дробь для неполной бета-функции (метод Лентца)."""
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 10_000):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-15:
            return h
    raise RuntimeError("betacf did not converge")


def betainc_regularized(a: float, b: float, x: float) -> float:
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    log_front = (math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
                 + a * math.log(x) + b * math.log1p(-x))
    front = math.exp(log_front)
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def student_two_sided(k: float, df: float) -> float:
    """P(|T_df| >= k) = I_{df/(df+k^2)}(df/2, 1/2)."""
    return betainc_regularized(df / 2.0, 0.5, df / (df + k * k))


# --------------------------------------------------------------------------- #
# Раздел 1–2: аналитика
# --------------------------------------------------------------------------- #
def section_student_table() -> None:
    print("=== 1. Хвост Стьюдента при эвристике n_eff = W/h (h = 8), как в handoff ===")
    p_norm = normal_two_sided(2.0)
    print(f"нормальное P(|Z|>=2) = {p_norm:.2%}")
    print(f"{'W, баров':>9} {'n_eff':>6} {'df':>4} {'P(|T|>=2)':>10} {'избыток':>8}")
    for window in (48, 96, 192, 336, 480):
        n_eff = window // 8
        df = n_eff - 1
        p = student_two_sided(2.0, df)
        print(f"{window:>9} {n_eff:>6} {df:>4} {p:>10.2%} {p / p_norm:>7.2f}x")


def section_effective_n() -> None:
    print("\n=== 2. Эффективный размер выборки для оценки дисперсии h-доходности ===")
    print("Гауссов iid, асимптотика: перекрывающиеся h-суммы по N значениям дают")
    print("n_eff = N*3h/(2h^2+1); непересекающиеся — N/h; однобарные с sqrt(h) — N.")
    print(f"{'h':>3} {'N':>5} {'перекр. N*3h/(2h^2+1)':>22} {'эвристика N/h':>14} "
          f"{'однобарные N':>13} {'P(|T|>=2) при df=n_eff-1 (перекр./однобарн.)':>46}")
    for h, n in ((8, 48), (4, 48), (4, 168), (8, 336), (4, 336)):
        overlap = n * 3 * h / (2 * h * h + 1)
        p_over = student_two_sided(2.0, overlap - 1)
        p_one = student_two_sided(2.0, n - 1)
        print(f"{h:>3} {n:>5} {overlap:>22.1f} {n / h:>14.1f} {n:>13} "
              f"{p_over:>22.2%} / {p_one:.2%}")
    for window in (168, 336):
        print(f"t_{window}: " + ", ".join(
            f"P(|T|>={k}) = {student_two_sided(k, window):.3%}" for k in KS
        ) + "   (нормальное: " + ", ".join(
            f"{normal_two_sided(k):.3%}" for k in KS) + ")")


# --------------------------------------------------------------------------- #
# Раздел 3: Монте-Карло
# --------------------------------------------------------------------------- #
def _gaussian(rng: np.random.Generator) -> np.ndarray:
    return rng.standard_normal(N_HOURS) * SIGMA_1H


def _student(rng: np.random.Generator, nu: float = 5.0) -> np.ndarray:
    draws = rng.standard_t(nu, N_HOURS)
    return draws / math.sqrt(nu / (nu - 2.0)) * SIGMA_1H


def _garch(rng: np.random.Generator, alpha: float = 0.05, beta: float = 0.94,
           nu: float = 5.0) -> np.ndarray:
    """GARCH(1,1) с t-инновациями, безусловная дисперсия = SIGMA_1H^2.

    Параметры иллюстративные (типичный порядок для внутридневных данных),
    НЕ откалиброваны на данных проекта.
    """
    eps = rng.standard_t(nu, N_HOURS) / math.sqrt(nu / (nu - 2.0))
    omega = (1.0 - alpha - beta) * SIGMA_1H ** 2
    var = SIGMA_1H ** 2
    out = np.empty(N_HOURS)
    for i in range(N_HOURS):
        r = math.sqrt(var) * eps[i]
        out[i] = r
        var = omega + alpha * r * r + beta * var
    return out


def _rolling_sum(x: np.ndarray, w: int) -> np.ndarray:
    """s[j] = sum(x[j-w+1 .. j]); nan, если окно неполное или содержит nan."""
    missing = np.isnan(x)
    c = np.concatenate(([0.0], np.cumsum(np.where(missing, 0.0, x))))
    cm = np.concatenate(([0], np.cumsum(missing)))
    window_sum = c[w:] - c[:-w]
    window_sum[(cm[w:] - cm[:-w]) > 0] = np.nan
    out = np.full(x.shape, np.nan)
    out[w - 1:] = window_sum
    return out


def _lag(x: np.ndarray, k: int) -> np.ndarray:
    out = np.full(x.shape, np.nan)
    if k == 0:
        return x.copy()
    out[k:] = x[:-k]
    return out


def _rolling_median_abs(x: np.ndarray, w: int, chunk: int = 20_000) -> np.ndarray:
    """median(|x[j-w+1 .. j]|), по частям, чтобы не держать N*w в памяти."""
    a = np.abs(x)
    out = np.full(x.shape, np.nan)
    for start in range(w - 1, len(a), chunk):
        stop = min(start + chunk, len(a))
        view = np.lib.stride_tricks.sliding_window_view(a[start - w + 1:stop], w)
        out[start:stop] = np.median(view, axis=1)
    return out


def definitions(r: np.ndarray) -> dict[str, np.ndarray]:
    """z_t для каждого определения; индекс t — последний закрытый бар."""
    out: dict[str, np.ndarray] = {}

    # (a) как в коде v1–v4: простые доходности, h=8, W=48; mean/std (ddof=1)
    # однобарных доходностей, окно включает бары текущего движения.
    simple = np.expm1(r)
    r8_simple = np.expm1(_rolling_sum(r, 8))
    s1 = _rolling_sum(simple, 48)
    s2 = _rolling_sum(simple * simple, 48)
    mean48 = s1 / 48
    var48 = (s2 - 48 * mean48 ** 2) / 47
    out["v1-v4 код: 8ч / std(1ч), W=48"] = (r8_simple - mean48) / np.sqrt(var48)

    # (b) «верный» z из scan_sigma_events.py: 48 перекрывающихся 8ч-доходностей,
    # окно кончается на баре t-1 (перекрывается с текущей 8ч-доходностью),
    # вычитается среднее, pstdev.
    r8 = np.expm1(_rolling_sum(r, 8))
    w1 = _lag(_rolling_sum(r8, 48), 1)
    w2 = _lag(_rolling_sum(r8 * r8, 48), 1)
    m = w1 / 48
    out["скан v4 «верный»: 8ч / std(8ч), W=48"] = (r8 - m) / np.sqrt(w2 / 48 - m ** 2)

    h = 4
    rh = _rolling_sum(r, h)

    def sqrt_h_rms(window: int, *, disjoint: bool = True, demean: bool = False) -> np.ndarray:
        lag = h if disjoint else 0
        s_1 = _lag(_rolling_sum(r, window), lag)
        s_2 = _lag(_rolling_sum(r * r, window), lag)
        if demean:
            mean = s_1 / window
            var = (s_2 - window * mean ** 2) / (window - 1)
            return (rh - h * mean) / np.sqrt(h * var)
        return rh / np.sqrt(h * s_2 / window)

    out["ВЫБРАНО: 4ч / (sqrt(4)*RMS 1ч), W=168, до окна"] = sqrt_h_rms(168)
    out["то же, W=48"] = sqrt_h_rms(48)
    out["то же, W=336"] = sqrt_h_rms(336)
    out["то же, с вычитанием среднего"] = sqrt_h_rms(168, demean=True)
    out["то же, окно включает текущие 4 бара"] = sqrt_h_rms(168, disjoint=False)

    # Буквальная форма «std того же горизонта»: перекрывающиеся 4ч-доходности,
    # окно кончается на t-h (не пересекается с сигналом), mean=0.
    for window in (168, 336):
        ov2 = _lag(_rolling_sum(rh * rh, window), h)
        out[f"перекрыв. 4ч-доходности, W={window}, до окна"] = rh / np.sqrt(ov2 / window)

    # Непересекающиеся 4ч-доходности внутри тех же 168 часов (42 штуки).
    blocks = [_lag(rh, h + h * j) for j in range(168 // h)]
    out["непересек. 4ч-доходности, 42 шт. в 168ч"] = rh / np.sqrt(
        np.mean(np.stack(blocks) ** 2, axis=0))

    # MAD: 1.4826 * median(|r1|) по тем же 168 барам, до окна.
    mad = _lag(_rolling_median_abs(r, 168), h) * 1.4826
    out["MAD: 4ч / (sqrt(4)*1.4826*med|r1|), W=168"] = rh / (math.sqrt(h) * mad)
    return out


def section_monte_carlo() -> dict[float, float]:
    """Печатает таблицы МК; возвращает долю «начал» у ВЫБРАННОГО на гауссовом iid."""
    print("\n=== 3. Монте-Карло: частота |z| >= k на синтетических нулевых моделях ===")
    print(f"seed={SEED}, {N_HOURS} часов на модель, z считается на каждом часе.")
    print("«начала» = доля часов, где |z| >= k впервые после часа с |z| < k")
    print("(единица сырого события при каденции 1ч; соседние часы перекрываются).")
    rng = np.random.default_rng(SEED)
    models = {
        "гауссов iid": _gaussian(rng),
        "Стьюдент nu=5 iid": _student(rng),
        "GARCH(1,1) a=0.05 b=0.94, t5 (иллюстр.)": _garch(rng),
    }
    p_norm = normal_two_sided(2.0)
    chosen_onsets: dict[float, float] = {}
    for name, r in models.items():
        print(f"\n--- модель: {name} ---")
        head = f"{'определение':46} {'std(z)':>7}"
        head += "".join(f" {'P>=' + str(k):>8}" for k in KS)
        head += f" {'P>=2/норм':>9}"
        head += "".join(f" {'начала' + str(k):>9}" for k in KS)
        print(head)
        for label, z in definitions(r).items():
            z = z[1000:]                      # отбросить прогрев окон
            valid = np.isfinite(z)
            zz = z[valid]
            row = f"{label:46} {np.std(zz):>7.3f}"
            for k in KS:
                row += f" {np.mean(np.abs(zz) >= k):>8.3%}"
            row += f" {np.mean(np.abs(zz) >= 2.0) / p_norm:>8.2f}x"
            for k in KS:
                above = np.abs(z) >= k
                prev = np.concatenate(([False], above[:-1]))
                onset = above & ~prev & valid
                rate = onset.sum() / valid.sum()
                row += f" {rate:>9.3%}"
                if name == "гауссов iid" and label.startswith("ВЫБРАНО"):
                    chosen_onsets[k] = rate
            print(row)
    print("\nТеория для ВЫБРАННОГО на гауссовом iid: z ~ t_168 (числитель и")
    print("знаменатель независимы, т.к. окно оценки не пересекается с окном сигнала):")
    print("  " + ", ".join(f"P(|T_168|>={k}) = {student_two_sided(k, 168):.3%}" for k in KS))
    return chosen_onsets


# --------------------------------------------------------------------------- #
# Раздел 4: плановые границы сегментов
# --------------------------------------------------------------------------- #
def section_planned_split() -> int:
    """Печатает плановые границы; возвращает длину test-сегмента в часах."""
    print("\n=== 4. Плановые границы сегментов (calendar_split на сетке окна) ===")
    period = 3_600_000
    start = int(WINDOW_START.timestamp() * 1000)
    end = int(WINDOW_END.timestamp() * 1000)
    grid = [Candle(timestamp=t, open=1.0, high=1.0, low=1.0, close=1.0, volume=0.0)
            for t in range(start, end, period)]
    # Перегрузки calendar_split не различают three_way по Literal, поэтому
    # тип результата сужается явно: при three_way=True это 6 границ.
    bounds: list[int] = list(calendar_split(grid, period, TRAIN_FRACTION, three_way=True,
                                            validation_ratio=VALIDATION_FRACTION))
    names = ("train", "validation", "test")

    def fmt(ms: int) -> str:
        return dt.datetime.fromtimestamp(ms / 1000, dt.UTC).strftime("%Y-%m-%d %H:%M")

    print(f"окно: {fmt(start)} .. {fmt(end)} (баров 1ч: {len(grid)}, "
          f"суток: {(end - start) / 86_400_000:.2f})")
    for i, name in enumerate(names):
        lo, hi = bounds[2 * i], bounds[2 * i + 1]
        print(f"  {name:10} {fmt(lo)} .. {fmt(hi)}  = {(hi - lo) / 3_600_000:.0f} ч "
              f"= {(hi - lo) / 86_400_000:.2f} сут")
    return (bounds[5] - bounds[4]) // period


def section_gate_bounds(chosen_onsets: dict[float, float], test_hours: int) -> None:
    print("\n=== 5. Предобъявленные числовые границы (арифметика из разделов 2–4) ===")
    p_norm = normal_two_sided(2.0)
    print(f"G3: P(|z|>=2) в [0.75, 1.50] x {p_norm:.2%} = "
          f"[{0.75 * p_norm:.2%}, {1.50 * p_norm:.2%}]")
    n_symbols = 9
    symbol_hours = test_hours * n_symbols
    print("Осуществимость (гауссов ноль, ВЫБРАНО): ожидаемые сырые «начала» на test =")
    print(f"доля начал x {test_hours} ч x {n_symbols} символов = доля x {symbol_hours}")
    for k, rate in chosen_onsets.items():
        print(f"  k={k}: {rate:.3%} x {symbol_hours} = {rate * symbol_hours:.0f}")


def main() -> None:
    section_student_table()
    section_effective_n()
    chosen_onsets = section_monte_carlo()
    test_hours = section_planned_split()
    section_gate_bounds(chosen_onsets, test_hours)


if __name__ == "__main__":
    main()
