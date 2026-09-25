#!/usr/bin/env python3
"""Число сделок mean_reversion_v0 реальным Backtester на train-сегменте — без PnL.

Правило выбора порога (docs/research/mr_cycle2_signal_definition.md, п. 6.3):
n_train(k) — PnLSummary.total_trades реального Backtester на train-сегменте;
конфигурация — регистрируемая (config/settings.yaml), кроме entry_threshold = k
и risk.emergency_drawdown_pct = 100.0 (diagnostic_widened: стоп по просадке не
должен цензурировать счёт).

Это не параллельная реализация логики стратегии. Сегмент прогоняется той же
функцией окна, что и walk_forward.py (_run_single_window), — с той же моделью
издержек, той же вселенной, тем же закреплённым сплитом и той же историей до
начала сегмента для прогрева. Прежний signal_frequency_check.py расходился с
боевым кодом пять раз именно потому, что был отдельной имитацией.

Скрипт печатает только счётчики. PnL, эквити и Sharpe не выводятся, а
логирование проекта на время прогона приглушено до WARNING: бэктестер пишет в
INFO итог каждой закрытой позиции вместе с pnl_abs. Сегмент — только train:
validation и test участвуют в decision rule и этим скриптом не расходуются.

С --onsets-train/--onsets-test (сырые начала при том же k из вывода
scripts/scan_sigma_events.py) скрипт считает и проекцию правила 6.3
N_proj = n_train * onsets_test / onsets_train, сравнивает её с 300 и
возвращает код 0 при PASS.

Запуск (из корня репозитория):

    python scripts/count_mr_trades.py --entry-threshold 3.0 --onsets-train N --onsets-test M
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
import sqlite3
import tempfile
from collections import Counter
from contextlib import closing
from pathlib import Path

from crypto_bot.config.env import Config
from crypto_bot.config.schemas import Settings
from crypto_bot.config.settings import load_settings
from crypto_bot.core.enums import Mode, StrategyType
from crypto_bot.features.context import SymbolMarketContext
from crypto_bot.simulation.historical_source import HistoricalCandleSource
from crypto_bot.simulation.market_constants import MARKET_QUOTE_VOLUME
from crypto_bot.simulation.walk_forward import (
    _run_single_window,
    calendar_split,
    fetch_all_candles,
    pin_candles,
)

PERIOD_MS = 3_600_000
DIAGNOSTIC_EMERGENCY_DD = 100.0
RULE_MIN_PROJECTED = 300      # правило 6.3: 1.5 * 200
UNIVERSE = ("BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT",
            "ADA/USDT", "DOGE/USDT", "BNB/USDT", "POL/USDT")


def utc_ms(day: str) -> int:
    return int(dt.datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=dt.UTC).timestamp() * 1000)


def fmt(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, dt.UTC).strftime("%Y-%m-%d %H:%M")


def diagnostic_config(config_path: Path, entry_threshold: float) -> Config:
    """Регистрируемый конфиг с двумя переопределениями правила 6.3 и режимом PAPER."""
    loaded = load_settings(yaml_path=config_path)
    data = loaded.settings.model_dump()
    data["portfolio"]["mean_reversion"]["entry_threshold"] = entry_threshold
    data["risk"]["emergency_drawdown_pct"] = DIAGNOSTIC_EMERGENCY_DD
    settings = Settings.model_validate(data)
    settings.runtime.mode = Mode.PAPER          # как в scripts/walk_forward.py
    return Config(settings=settings, env=loaded.env)


class FailedTicks(logging.Handler):
    """Тики, которые бэктестер потерял целиком: исключение в пайплайне тика.

    Бэктестер ловит такое исключение широким except и пишет «bt: pipeline failed
    at ts=...» — вместе с тиком теряются и входы, и выходы. Счёт сделок обязан
    говорить, сколько тиков выпало и где.
    """

    PREFIX = "bt: pipeline failed at ts="

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.ts: list[int] = []

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if message.startswith(self.PREFIX):
            self.ts.append(int(message[len(self.PREFIX):].split(":", 1)[0]))


def max_concurrent(intervals: list[tuple[int, int]]) -> int:
    events = sorted([(o, 1) for o, _ in intervals] + [(c, -1) for _, c in intervals],
                    key=lambda e: (e[0], e[1]))
    level = peak = 0
    for _, delta in events:
        level += delta
        peak = max(peak, level)
    return peak


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--entry-threshold", type=float, required=True)
    parser.add_argument("--db", default="data/crypto_bot.db")
    parser.add_argument("--config", default="config/settings.yaml")
    parser.add_argument("--start", default="2024-01-01")
    parser.add_argument("--end", default="2026-09-17")
    parser.add_argument("--split", type=float, default=0.5)
    parser.add_argument("--validation-split", type=float, default=0.2)
    parser.add_argument("--symbols", nargs="+", default=list(UNIVERSE))
    parser.add_argument("--onsets-train", type=int, default=None,
                        help="сырые начала на train при этом k (вывод scan_sigma_events.py)")
    parser.add_argument("--onsets-test", type=int, default=None,
                        help="сырые начала на test при этом k (вывод scan_sigma_events.py)")
    args = parser.parse_args()
    if (args.onsets_train is None) != (args.onsets_test is None):
        parser.error("--onsets-train и --onsets-test задаются вместе")

    config = diagnostic_config(Path(args.config), args.entry_threshold)
    project_logger = logging.getLogger("crypto_bot")
    project_logger.setLevel(logging.WARNING)
    failed_ticks = FailedTicks()
    project_logger.addHandler(failed_ticks)
    mr = config.settings.portfolio.mean_reversion

    symbols = list(args.symbols)
    candles = fetch_all_candles(args.db, symbols, mr.timeframe)
    source = HistoricalCandleSource()
    for sym, bars in candles.items():
        source.load_all(sym, mr.timeframe, bars)
    bounds = calendar_split(pin_candles(candles[symbols[0]], utc_ms(args.start), utc_ms(args.end)),
                            PERIOD_MS, args.split, three_way=True,
                            validation_ratio=args.validation_split)
    lo, hi = bounds[0], bounds[1]
    market_map = {sym: SymbolMarketContext(quote_volume_24h=MARKET_QUOTE_VOLUME.get(sym, 1_000_000_000))
                  for sym in symbols}

    print("=== Прогон: реальный Backtester, train-сегмент, только счётчики ===")
    print(f"конфиг: {args.config}; переопределено: entry_threshold={args.entry_threshold}, "
          f"risk.emergency_drawdown_pct={DIAGNOSTIC_EMERGENCY_DD} (diagnostic_widened)")
    print(f"символы: {len(symbols)}; окно [{args.start}, {args.end}); split={args.split}, "
          f"validation_split={args.validation_split}")
    print(f"train: {fmt(lo)} .. {fmt(hi)} = {(hi - lo) // PERIOD_MS} ч = "
          f"{(hi - lo) / 86_400_000:.2f} сут")
    print(f"сетка: h={mr.signal_lookback}, W={mr.zscore_window_bars}, exit_threshold={mr.exit_threshold}, "
          f"max_holding_bars={mr.max_holding_bars}, rebalance_hours={mr.rebalance_hours}, "
          f"weighting={mr.weighting}, execution={mr.entry_execution}/{mr.exit_execution}")

    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "count.db")
        summary = asyncio.run(_run_single_window(
            config, symbols, mr.timeframe, source, lo, hi,
            strategy_mode=StrategyType.PORTFOLIO,
            regime_config=config.settings.regime,
            market_map=market_map,
            db_path=db_path,
            enable_funding=True,
        ))
        with closing(sqlite3.connect(db_path)) as con:
            rows = con.execute(
                "SELECT symbol, side, status, closed_by, opened_at_ms, closed_at_ms FROM positions"
            ).fetchall()
            rejected = Counter(detail for (detail,) in con.execute(
                "SELECT detail FROM decisions WHERE accepted = 0"
            ).fetchall())

    closed = [r for r in rows if r[5] is not None]
    days = (hi - lo) / 86_400_000
    print("\n=== Результат (счётчики, без PnL) ===")
    print(f"n_train = PnLSummary.total_trades = {summary.total_trades}")
    print(f"закрытых позиций в БД прогона = {len(closed)}; открытых на конец = {len(rows) - len(closed)}")
    print(f"входов всего = {len(rows)}; в сутки = {len(rows) / days:.2f}")
    print(f"по сторонам: {dict(Counter(r[1] for r in rows))}")
    print(f"по символам: {dict(sorted(Counter(r[0] for r in rows).items()))}")
    print(f"closed_by: {dict(Counter(r[3] for r in closed))}")
    holding = Counter((r[5] - r[4]) / PERIOD_MS for r in closed)
    print(f"удержание закрытых, часов: {dict(sorted(holding.items()))}")
    print(f"пик одновременно открытых позиций = "
          f"{max_concurrent([(r[4], r[5] if r[5] is not None else hi) for r in rows])}")
    print(f"отказы риск-движка по причинам (записи decisions): {dict(rejected)}")
    in_segment = [ts for ts in failed_ticks.ts if lo <= ts <= hi]
    span = f"{fmt(min(in_segment))} .. {fmt(max(in_segment))}" if in_segment else "-"
    print(f"тиков, потерянных пайплайном целиком (исключение в тике): {len(in_segment)}; {span}")

    if args.onsets_train is None:
        return 0
    # Правило 6.3: N_proj(k) = n_train(k) * onsets_test(k) / onsets_train(k).
    # Начала берутся из вывода scripts/scan_sigma_events.py для того же k.
    n_proj = summary.total_trades * args.onsets_test / args.onsets_train
    test_days = (bounds[5] - bounds[4]) / 86_400_000
    passed = n_proj >= RULE_MIN_PROJECTED
    print("\n=== Правило 6.3: проекция на test ===")
    print(f"N_proj = {summary.total_trades} * {args.onsets_test} / {args.onsets_train} = {n_proj:.1f}")
    print(f"N_proj >= {RULE_MIN_PROJECTED}: {'PASS' if passed else 'FAIL'}")
    print(f"входов в сутки на test (проекция) = {n_proj:.1f} / {test_days:.2f} = "
          f"{n_proj / test_days:.2f}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
