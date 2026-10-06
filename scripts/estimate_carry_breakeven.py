#!/usr/bin/env python3
"""Пороги cash-and-carry до любых данных — арифметика из объявленных входов.

Пара «лонг спот + шорт линейного перпетуала» одинакового количества монеты не
зарабатывает на цене; доход — фандинг шорта, расход — издержки кругов. Скрипт
считает, какой средний фандинг нужен, чтобы чистая доходность на весь капитал
достигла порога, и сколько стоит один круг. Ни цен, ни ставок фандинга он не
читает: все входы — аргументы, их значения и источники фиксирует документ
решения (docs/research/funding-basis/cycle-1/1-hypothesis-and-decision-rule.md).

Модель капитала К1 (единый торговый счёт): спот идёт в залог маржи перпетуала,
поэтому номинал пары равен доле капитала вне резерва: e = 1 - r.

Пример:
    python scripts/estimate_carry_breakeven.py
"""
from __future__ import annotations

import argparse

DAYS_PER_YEAR = 365  # крипто торгуется круглосуточно, не 252


def round_trip_cost(spot_fee: float, perp_fee: float, slippage: float) -> float:
    """Cost of opening and closing one pair, as a fraction of its notional."""
    return 2 * (spot_fee + slippage) + 2 * (perp_fee + slippage)


def required_funding(threshold: float, exposure: float, trips_per_year: float, c_rt: float) -> float:
    """Average annual funding on notional that yields ``threshold`` on the whole capital."""
    return threshold / exposure + trips_per_year * c_rt


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--threshold", type=float, default=0.08, help="порог годовой доходности на капитал")
    p.add_argument("--reserve", type=float, nargs="+", default=[0.05], help="резерв USDT r, доля капитала")
    p.add_argument("--interest-daily", type=float, default=0.0003,
                   help="процентная компонента ставки в сутки, доля номинала")
    p.add_argument("--spot-fee", type=float, default=0.001, help="комиссия спота на сторону")
    p.add_argument("--perp-fee", type=float, default=0.00055, help="комиссия перпетуала на сторону")
    p.add_argument("--slippage", type=float, default=0.0002, help="проскальзывание на сторону на ногу")
    p.add_argument("--trips", type=float, nargs="+", default=[1.0, 4.0], help="кругов в год")
    p.add_argument("--collateral-ratio", type=float, default=0.98, help="коэффициент залога спота")
    p.add_argument("--mmr", type=float, default=0.0033, help="маржа поддержки первого тира")
    a = p.parse_args()

    base_annual = a.interest_daily * DAYS_PER_YEAR
    print(f"порог H = {a.threshold:.2%} годовых на капитал; база фандинга = "
          f"{a.interest_daily:.4%}/сут = {base_annual:.2%} годовых на номинал")
    for slip in (a.slippage, 2 * a.slippage):
        c_rt = round_trip_cost(a.spot_fee, a.perp_fee, slip)
        tag = "регистрируемое" if slip == a.slippage else "справочно, x2"
        print(f"\nпроскальзывание {slip * 1e4:.1f} bps на сторону на ногу ({tag}): "
              f"круг c_rt = {c_rt * 1e4:.1f} bps номинала; безубыточное удержание при базе = "
              f"{c_rt / a.interest_daily:.1f} сут")
        for r in a.reserve:
            e = 1.0 - r
            for n in a.trips:
                need = required_funding(a.threshold, e, n, c_rt)
                print(f"  r = {r:.0%}, e = {e:.2f}, кругов/год = {n:g}: издержки {n * c_rt * e:.2%} капитала; "
                      f"нужен средний фандинг >= {need:.2%} годовых на номинал")
    for r in a.reserve:
        e = 1.0 - r
        print(f"\nr = {r:.0%}: доход на капитал при базе до издержек = {base_annual * e:.2%}; "
              f"резерв покрывает {r / (e * a.interest_daily):.1f} сут отрицательного фандинга на уровне базы")
    haircut = 1.0 - a.collateral_ratio
    print(f"\nК1, ликвидация пары без свободного USDT: рост цены в {1.0 / (haircut + a.mmr):.1f} раза "
          f"(дисконт залога {haircut:.2%} + MMR {a.mmr:.2%})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
