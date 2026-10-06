#!/usr/bin/env python3
"""
scripts/reproduce_mr_cycle2.py

Re-runs the MR cycle 2 production walk-forward on the current code and checks
it against the recorded run number for number: the funding accounting fix
(Task 1, test 8 of docs/research/funding-basis/plan.md) must not move a run
that had no funding.

Pinned exactly as in the production run (docs/research/mean-reversion/cycle-2/):

- config: config/settings.yaml, checked first against the Config Snapshot of
  3-preregistration.md (``check_config_snapshot.py --strict-unregistered``);
- candles: 1h from data/crypto_bot.db (spot, backlog item 10), window
  2024-01-01..2026-09-17, split 0.5 / validation 0.2, the registered command;
- funding: none. The production windows read funding from their own empty
  databases (F0), so the reproduction hands them an empty source;
- instrument specs: the snapshot data/cache/bybit_instruments_2026-09-26T0709Z.json
  (sha256 checked) — never the live cache, never the API.

Only the validation and test windows run: the decision rule reads nothing else,
and every window replays on a fresh database of its own.

Passes (exit 0) when both hold:
1. decisions, equity, positions, trades and funding_payments of each window
   equal data/backtests/mr_cycle2_production/ row for row, apart from the
   columns stamped with the time of writing (created_at, updated_at,
   trades.ts_ms). Only counts are printed, no values;
2. scripts/mr_decision_rule.py on the reproduced databases prints the block
   published in 4-closure.md, section 1, and exits 1 (REJECT) as there.

Usage::

    python scripts/reproduce_mr_cycle2.py [--db-dir DIR]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sqlite3
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from crypto_bot.config.env import Config
from crypto_bot.config.settings import load_settings
from crypto_bot.core.enums import Mode, StrategyType
from crypto_bot.data import instruments
from crypto_bot.data.funding import HistoricalFundingSource
from crypto_bot.features.context import SymbolMarketContext
from crypto_bot.simulation.historical_source import HistoricalCandleSource
from crypto_bot.simulation.market_constants import MARKET_QUOTE_VOLUME
from crypto_bot.simulation.walk_forward import (
    _run_single_window,
    calendar_split,
    fetch_all_candles,
    pin_candles,
)

ROOT = Path(__file__).resolve().parents[1]
CYCLE2 = ROOT / "docs" / "research" / "mean-reversion" / "cycle-2"
PREREGISTRATION = CYCLE2 / "3-preregistration.md"
CLOSURE = CYCLE2 / "4-closure.md"
CONFIG = ROOT / "config" / "settings.yaml"
DATA_DB = ROOT / "data" / "crypto_bot.db"
PRODUCTION_DIR = ROOT / "data" / "backtests" / "mr_cycle2_production"
SPECS_SNAPSHOT = ROOT / "data" / "cache" / "bybit_instruments_2026-09-26T0709Z.json"
SPECS_SHA256 = "766b66e5690b1ab73ecae7aaf84839727a3d25cfa194b527fad337acbd5c5e33"

# The registered command: 3-preregistration.md, section 7
SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT",
    "ADA/USDT", "DOGE/USDT", "BNB/USDT", "POL/USDT",
]
TIMEFRAME = "1h"
PERIOD_MS = 3_600_000
START, END = "2024-01-01", "2026-09-17"
SPLIT, VALIDATION_SPLIT = 0.5, 0.2
TEST_START, TEST_END = "2025-11-24 00:00", "2026-09-17 00:00"

TABLES = ("decisions", "equity", "positions", "trades", "funding_payments")
# Stamped with the time of writing, not the simulated time, so two replays differ
# in them by construction: created_at / updated_at everywhere, and trades.ts_ms —
# TradeRepository.insert writes time.time(). Simulated times are compared through
# decisions.ts_ms, equity.ts_ms and positions.opened_at_ms / closed_at_ms.
WALL_CLOCK_COLUMNS = {"created_at", "updated_at"}
WALL_CLOCK_BY_TABLE = {"trades": {"ts_ms"}}
PRODUCTION_LABEL = "data/backtests/mr_cycle2_production"
CLOSURE_OUTPUT_MARKER = "Вывод (код возврата 1 = REJECT):"


def utc_date_ms(value: str) -> int:
    """Same parsing as scripts/walk_forward.py --start/--end."""
    return int(datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC).timestamp() * 1000)


def check_config_snapshot() -> bool:
    result = subprocess.run(
        [sys.executable, "scripts/check_config_snapshot.py", str(PREREGISTRATION.relative_to(ROOT)),
         "--strict-unregistered"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    tail = [line for line in result.stdout.splitlines() if line.strip()][-3:]
    print(f"config snapshot: {' | '.join(tail)} (exit {result.returncode})")
    return result.returncode == 0


class _NoLiveSpecs:
    """Stands in for the Bybit client: the run must never fall back to live specs."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise RuntimeError("instrument snapshot did not load; refusing to fetch live specs")


def load_instrument_snapshot() -> instruments.InstrumentCache | None:
    """The cycle's snapshot, checked by sha256; the live cache and the API are never used."""
    try:
        cache = instruments.InstrumentCache.from_snapshot(SPECS_SNAPSHOT, expected_sha256=SPECS_SHA256)
    except ValueError as exc:
        print(f"FAIL: {exc}")
        return None
    instruments.BybitInstrumentsClient = _NoLiveSpecs  # type: ignore[misc, assignment]
    tradable = {sym: cache.is_tradable_linear_perpetual(sym) for sym in SYMBOLS}
    print(f"instrument specs: {SPECS_SNAPSHOT.name}, sha256 ok, tradable {tradable}")
    return cache if all(tradable.values()) else None


def load_config() -> Config:
    """Same construction as scripts/walk_forward.py without overrides."""
    config_obj = load_settings(yaml_path=CONFIG)
    settings = config_obj.settings.__class__.model_validate(config_obj.settings.model_dump())
    settings.runtime.mode = Mode.PAPER
    return Config(settings=settings, env=config_obj.env)


def run_windows(config: Config, db_dir: Path, instrument_cache: instruments.InstrumentCache) -> None:
    """Validation and test windows exactly as run_walk_forward builds them."""
    source = HistoricalCandleSource()
    for sym, candles in fetch_all_candles(str(DATA_DB), SYMBOLS, TIMEFRAME).items():
        source.load_all(sym, TIMEFRAME, candles)
    market_map = {
        sym: SymbolMarketContext(quote_volume_24h=MARKET_QUOTE_VOLUME.get(sym, 1_000_000_000))
        for sym in SYMBOLS
    }
    split_candles = pin_candles(
        source.slice_between(0, 2**63 - 1, SYMBOLS[0], TIMEFRAME), utc_date_ms(START), utc_date_ms(END),
    )
    _, _, val_start, val_end, test_start, test_end = calendar_split(
        split_candles, PERIOD_MS, SPLIT, three_way=True, validation_ratio=VALIDATION_SPLIT,
    )
    for name, start_ms, end_ms in (("validation", val_start, val_end), ("test", test_start, test_end)):
        began = datetime.now(tz=UTC)
        asyncio.run(_run_single_window(
            config, SYMBOLS, TIMEFRAME, source, start_ms, end_ms,
            strategy_mode=StrategyType.PORTFOLIO,
            regime_config=config.settings.regime,
            market_map=market_map,
            db_path=str(db_dir / f"{name}.db"),
            enable_funding=True,  # bybit_perp_default(), the production cost model
            funding_source=HistoricalFundingSource(),  # no events, as the production windows saw
            instrument_cache=instrument_cache,
        ))
        minutes = (datetime.now(tz=UTC) - began).total_seconds() / 60
        print(f"{name}: {fmt(start_ms)} .. {fmt(end_ms)} replayed in {minutes:.1f} min")


def fmt(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M")


def table_rows(path: Path, table: str) -> tuple[list[str], list[tuple]]:
    # Read-only and raw: Database() would migrate the production databases to v11
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        wall_clock = WALL_CLOCK_COLUMNS | WALL_CLOCK_BY_TABLE.get(table, set())
        columns = [
            row[1] for row in conn.execute(f"PRAGMA table_info({table})")
            if row[1] not in wall_clock
        ]
        rows = conn.execute(f"SELECT {', '.join(columns)} FROM {table} ORDER BY rowid").fetchall()
        return columns, rows
    finally:
        conn.close()


def compare_databases(db_dir: Path) -> bool:
    identical = True
    for window in ("validation", "test"):
        for table in TABLES:
            cols_new, new = table_rows(db_dir / f"{window}.db", table)
            cols_old, old = table_rows(PRODUCTION_DIR / f"{window}.db", table)
            if cols_new != cols_old:
                print(f"{window}.{table}: columns differ {cols_new} vs {cols_old}")
                identical = False
                continue
            differing = sum(a != b for a, b in zip(new, old, strict=False)) + abs(len(new) - len(old))
            print(f"{window}.{table}: rows {len(new)} vs production {len(old)}, differing {differing}")
            identical = identical and differing == 0
    return identical


def published_decision_output() -> list[str]:
    lines = CLOSURE.read_text(encoding="utf-8").splitlines()
    fence = lines.index("```", lines.index(CLOSURE_OUTPUT_MARKER))
    return lines[fence + 1:lines.index("```", fence + 1)]


def check_decision_rule(db_dir: Path) -> bool:
    val_db, test_db = db_dir / "validation.db", db_dir / "test.db"
    result = subprocess.run(
        [sys.executable, "scripts/mr_decision_rule.py", "--label", "production",
         "--validation-db", str(val_db), "--test-db", str(test_db),
         "--test-start", TEST_START, "--test-end", TEST_END],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    output = (
        result.stdout.replace(str(val_db), f"{PRODUCTION_LABEL}/validation.db")
        .replace(str(test_db), f"{PRODUCTION_LABEL}/test.db")
        .splitlines()
    )
    expected = published_decision_output()
    print("mr_decision_rule.py on the reproduced databases (paths shown as in 4-closure.md):")
    for line in output:
        print(f"  {line}")
    if result.stderr.strip():
        print(f"stderr: {result.stderr.strip()}")
    mismatches = [
        (i + 1, got, want)
        for i, (got, want) in enumerate(zip(output, expected, strict=False))
        if got != want
    ]
    same = not mismatches and len(output) == len(expected)
    print(
        f"decision output vs 4-closure.md: {len(output)} vs {len(expected)} lines, "
        f"differing {len(mismatches) + abs(len(output) - len(expected))}; exit {result.returncode} (expected 1)"
    )
    for line_no, got, want in mismatches:
        print(f"  line {line_no}: got {got!r}, published {want!r}")
    return same and result.returncode == 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db-dir", type=Path, default=None,
                        help="Where the reproduced validation/test databases go (default: a new temp dir)")
    args = parser.parse_args()

    db_dir = args.db_dir or Path(tempfile.mkdtemp(prefix="mr_cycle2_repro_"))
    db_dir.mkdir(parents=True, exist_ok=True)
    stale = [p.name for p in (db_dir / "validation.db", db_dir / "test.db") if p.exists()]
    if stale:
        print(f"FAIL: {db_dir} already holds {stale}; a replay appends to an existing database")
        return 1
    missing = [
        str(p.relative_to(ROOT)) for p in
        (DATA_DB, SPECS_SNAPSHOT, PRODUCTION_DIR / "validation.db", PRODUCTION_DIR / "test.db")
        if not p.exists()
    ]
    if missing:
        print(f"FAIL: not on disk: {missing}")
        return 1
    print(f"reproduced databases: {db_dir}")

    config = load_config()
    # load_settings sets up the project logger at INFO: a line per tick otherwise
    logging.getLogger("crypto_bot").setLevel(logging.WARNING)
    if not check_config_snapshot():
        return 1
    instrument_cache = load_instrument_snapshot()
    if instrument_cache is None:
        return 1
    run_windows(config, db_dir, instrument_cache)
    tables_ok = compare_databases(db_dir)
    decision_ok = check_decision_rule(db_dir)
    verdict = "REPRODUCED" if tables_ok and decision_ok else "NOT REPRODUCED"
    print(f"VERDICT: {verdict}")
    return 0 if tables_ok and decision_ok else 1


if __name__ == "__main__":
    sys.exit(main())
