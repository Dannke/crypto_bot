#!/usr/bin/env python3
"""Статистическая валидация z-score MR и сырая частота сигнала (цикл 2, шаг 2).

Гейты и их границы предобъявлены до измерения в
docs/research/mr_cycle2_signal_definition.md, раздел 6:

    G1  std(r_4h) / std(r_1h), лог-доходности, train, по КАЖДОМУ символу: [1.8, 2.2]
    G2  std(z), все символо-часы train:                                  [0.90, 1.10]
    G3  P(|z| >= 2), все символо-часы train:                              [3.41%, 6.83%]

У скрипта нет своей формулы z. z считает zscore_from_closes — та же функция,
через которую его получает стратегия (compute_zscore_snapshot), с параметрами
определения из config/settings.yaml. Границы сегментов даёт та же
calendar_split на тех же закреплённых свечах, что и walk_forward.py, — прежнее
расхождение вида «19:12 против 19:00» исключено построением.

z в момент as_of (закрытие бара) считается по закрытиям до этого бара
включительно — ровно то, что стратегия видит на тике as_of. Символо-час
относится к сегменту по as_of, полуинтервал [начало, конец). Часы бэктестера
включают обе границы окна, это расходится не больше чем на один тик на границу.

ЧЕМ ВЫВОД НЕ ЯВЛЯЕТСЯ. «Начала» (час с |z| >= k после часа с |z| < k) считаются
по каждому символу независимо: без селекционного капа, без max_positions, без
исключения удерживаемых позиций. Это верхняя граница числа возможностей, а не
оценка n_trades. На test печатаются только счётчики начал — доходности после
сигнала скрипт не считает нигде.

Запуск (из корня репозитория, читает data/crypto_bot.db):

    python scripts/scan_sigma_events.py
"""
from __future__ import annotations

import argparse
import datetime as dt
import math
import statistics as st
from dataclasses import dataclass, field
from pathlib import Path

from crypto_bot.config.settings import load_settings
from crypto_bot.core import policy
from crypto_bot.portfolio.mean_reversion_features import min_history_bars, zscore_from_closes
from crypto_bot.simulation.walk_forward import calendar_split, fetch_all_candles, pin_candles

PERIOD_MS = 3_600_000
KS = (2.0, 2.5, 3.0)
G1_BOUNDS = (1.8, 2.2)
G2_BOUNDS = (0.90, 1.10)
G3_BOUNDS = (0.0341, 0.0683)
SENSITIVITY_WINDOW = 336
# Вселенная цикла 2 после поправки 1: MARKET_QUOTE_VOLUME без AVAX/USDT, у которого
# нет спецификации инструмента, — бэктестер его не исполняет. Первый символ задаёт сплит.
UNIVERSE = ("BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT",
            "ADA/USDT", "DOGE/USDT", "BNB/USDT", "POL/USDT")


@dataclass
class Point:
    as_of: int
    z: float
    move: float        # r_h = z * s_h, лог-доходность за горизонт сигнала


@dataclass
class SymbolSeries:
    points: list[Point] = field(default_factory=list)
    z_wide: list[tuple[int, float]] = field(default_factory=list)   # (as_of, z при W=336)


def utc_ms(day: str) -> int:
    return int(dt.datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=dt.UTC).timestamp() * 1000)


def fmt(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, dt.UTC).strftime("%Y-%m-%d %H:%M")


def normal_two_sided(k: float) -> float:
    return math.erfc(k / math.sqrt(2.0))


def verdict(ok: bool) -> str:
    return "PASS" if ok else "FAIL"


def build_series(closes: list[float], opens_ms: list[int], window: int, h: int,
                 lo: int, hi: int) -> SymbolSeries:
    """z по реальной функции признака для каждого as_of в [lo, hi)."""
    out = SymbolSeries()
    need = min_history_bars(window, h)
    need_wide = min_history_bars(SENSITIVITY_WINDOW, h)
    for i in range(len(closes)):
        as_of = opens_ms[i] + PERIOD_MS
        if not lo <= as_of < hi or i + 1 < need:
            continue
        result = zscore_from_closes(closes[i + 1 - need:i + 1],
                                    window_bars=window, signal_lookback_bars=h)
        if result is not None:
            z, sigma_h = result
            out.points.append(Point(as_of, z, z * sigma_h))
        if i + 1 >= need_wide:
            wide = zscore_from_closes(closes[i + 1 - need_wide:i + 1],
                                      window_bars=SENSITIVITY_WINDOW, signal_lookback_bars=h)
            if wide is not None:
                out.z_wide.append((as_of, wide[0]))
    return out


def onsets(points: list[Point], k: float) -> list[Point]:
    """Часы с |z| >= k, которым предшествует час без z или с |z| < k."""
    out: list[Point] = []
    prev_as_of, prev_above = None, False
    for p in points:
        above = abs(p.z) >= k
        contiguous = prev_as_of is not None and p.as_of - prev_as_of == PERIOD_MS
        if above and not (contiguous and prev_above):
            out.append(p)
        prev_as_of, prev_above = p.as_of, above
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/crypto_bot.db")
    parser.add_argument("--config", default="config/settings.yaml")
    parser.add_argument("--start", default="2024-01-01")
    parser.add_argument("--end", default="2026-09-17")
    parser.add_argument("--symbols", nargs="+", default=list(UNIVERSE))
    parser.add_argument("--split", type=float, default=0.5)
    parser.add_argument("--validation-split", type=float, default=0.2)
    args = parser.parse_args()

    mr = load_settings(yaml_path=Path(args.config)).settings.portfolio.mean_reversion
    tf_seconds = policy.timeframe_to_seconds(mr.timeframe)
    if tf_seconds * 1000 != PERIOD_MS:
        raise SystemExit(f"скрипт рассчитан на 1h, в конфиге timeframe={mr.timeframe}")
    h = policy.parse_duration_seconds(mr.signal_lookback) // tf_seconds
    window = mr.zscore_window_bars

    symbols = list(args.symbols)
    candles = fetch_all_candles(args.db, symbols, mr.timeframe)
    start_ms, end_ms = utc_ms(args.start), utc_ms(args.end)
    bounds = calendar_split(pin_candles(candles[symbols[0]], start_ms, end_ms), PERIOD_MS,
                            args.split, three_way=True, validation_ratio=args.validation_split)
    segments = {
        "train": (bounds[0], bounds[1]),
        "validation": (bounds[2], bounds[3]),
        "test": (bounds[4], bounds[5]),
    }

    print("=== Определение и окно (из конфига и аргументов) ===")
    print(f"конфиг: {args.config}; timeframe={mr.timeframe}, signal_lookback={mr.signal_lookback} "
          f"= {h} бар(а) = {h} ч, zscore_window_bars={window} = {window} ч")
    print(f"окно: [{args.start}, {args.end}) UTC; split={args.split}, "
          f"validation_split={args.validation_split}; символы: {len(symbols)}")
    for name, (lo, hi) in segments.items():
        print(f"  {name:10} {fmt(lo)} .. {fmt(hi)} = {(hi - lo) // PERIOD_MS} ч "
              f"= {(hi - lo) / 86_400_000:.2f} сут")

    series: dict[str, SymbolSeries] = {}
    train_lo, train_hi = segments["train"]
    g1: dict[str, float] = {}
    print("\n=== Покрытие окна по символам (бары 1ч с открытием в окне) ===")
    for sym in symbols:
        bars = candles[sym]
        pinned = pin_candles(bars, start_ms, end_ms)
        expected = (end_ms - start_ms) // PERIOD_MS
        print(f"  {sym:10} баров={len(pinned):6} ожидалось={expected:6} "
              f"первый={fmt(pinned[0].timestamp) if pinned else '-'} "
              f"последний={fmt(pinned[-1].timestamp) if pinned else '-'}")
        closes = [c.close for c in bars]
        opens = [c.timestamp for c in bars]
        series[sym] = build_series(closes, opens, window, h, start_ms, end_ms)
        # G1: масштаб лог-доходностей на train — проверка допущения sqrt(h), не реализации.
        r1 = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))
              if train_lo <= opens[i] + PERIOD_MS < train_hi]
        rh = [math.log(closes[i] / closes[i - h]) for i in range(h, len(closes))
              if train_lo <= opens[i] + PERIOD_MS < train_hi]
        g1[sym] = st.pstdev(rh) / st.pstdev(r1)

    def in_segment(name: str, points: list[Point]) -> list[Point]:
        lo, hi = segments[name]
        return [p for p in points if lo <= p.as_of < hi]

    print(f"\n=== G1: std(r_{h}h) / std(r_1h), train, граница {list(G1_BOUNDS)} ===")
    for sym, ratio in g1.items():
        print(f"  {sym:10} {ratio:.4f}  {verdict(G1_BOUNDS[0] <= ratio <= G1_BOUNDS[1])}")
    print(f"  среднее по символам {st.mean(g1.values()):.4f}; sqrt({h}) = {math.sqrt(h):.4f}")
    g1_ok = all(G1_BOUNDS[0] <= r <= G1_BOUNDS[1] for r in g1.values())

    train_points = {sym: in_segment("train", s.points) for sym, s in series.items()}
    pooled = [p.z for pts in train_points.values() for p in pts]
    std_z = st.pstdev(pooled)
    p2 = sum(abs(z) >= 2.0 for z in pooled) / len(pooled)
    g2_ok = G2_BOUNDS[0] <= std_z <= G2_BOUNDS[1]
    g3_ok = G3_BOUNDS[0] <= p2 <= G3_BOUNDS[1]
    print(f"\n=== G2 и G3: все символо-часы train (n = {len(pooled)}) ===")
    print(f"  G2 std(z) = {std_z:.4f}, граница {list(G2_BOUNDS)}: {verdict(g2_ok)}")
    print(f"  G3 P(|z|>=2) = {p2:.3%} ({p2 / normal_two_sided(2.0):.2f}x нормального), "
          f"граница [{G3_BOUNDS[0]:.2%}, {G3_BOUNDS[1]:.2%}]: {verdict(g3_ok)}")
    print(f"\nВЕРДИКТ статистической валидации: "
          f"{verdict(g1_ok and g2_ok and g3_ok)} (G1 {verdict(g1_ok)}, "
          f"G2 {verdict(g2_ok)}, G3 {verdict(g3_ok)})")

    print("\n=== Диагностика без порога (train) ===")
    print("хвосты, все символо-часы:")
    for k in KS:
        frac = sum(abs(z) >= k for z in pooled) / len(pooled)
        print(f"  P(|z|>={k}) = {frac:.3%}   нормальное {normal_two_sided(k):.3%}   "
              f"отношение {frac / normal_two_sided(k):.2f}x")
    print("по символам:")
    for sym, pts in train_points.items():
        zs = [p.z for p in pts]
        print(f"  {sym:10} n={len(zs):6} std(z)={st.pstdev(zs):.4f} "
              f"P(|z|>=2)={sum(abs(z) >= 2.0 for z in zs) / len(zs):.3%}")
    print("по часу суток as_of (UTC), P(|z|>=2):")
    by_hour: dict[int, list[float]] = {}
    for pts in train_points.values():
        for p in pts:
            by_hour.setdefault(dt.datetime.fromtimestamp(p.as_of / 1000, dt.UTC).hour, []).append(p.z)
    for hour in sorted(by_hour):
        zs = by_hour[hour]
        print(f"  {hour:02d}:00  {sum(abs(z) >= 2.0 for z in zs) / len(zs):.3%}")
    wide = [z for s in series.values() for as_of, z in s.z_wide if train_lo <= as_of < train_hi]
    print(f"чувствительность к окну, W={SENSITIVITY_WINDOW}: std(z)={st.pstdev(wide):.4f}, "
          f"P(|z|>=2)={sum(abs(z) >= 2.0 for z in wide) / len(wide):.3%} (n = {len(wide)})")

    print("\n=== Сырые начала по сегментам — верхняя граница, НЕ n_trades ===")
    print(f"{'сегмент':10} {'k':>4} {'начал':>7} {'LONG':>6} {'SHORT':>6} {'в сутки':>8} "
          f"{'часов с началом':>16} {'сред.|r_h|, bps':>16}")
    for name, (lo, hi) in segments.items():
        days = (hi - lo) / 86_400_000
        for k in KS:
            events = [p for s in series.values() for p in in_segment(name, onsets(s.points, k))]
            n_long = sum(p.z < 0 for p in events)
            hours = len({p.as_of for p in events})
            move = (f"{st.mean(abs(p.move) for p in events) * 1e4:16.1f}"
                    if name == "train" and events else f"{'-':>16}")
            print(f"{name:10} {k:>4} {len(events):>7} {n_long:>6} {len(events) - n_long:>6} "
                  f"{len(events) / days:>8.2f} {hours:>16} {move}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
