"""Export validation snapshots from live DB + Backtester.

Usage:
    python scripts/export_validation_fixture.py --symbol BTC/USDT --timeframe 5m

    Exports a JSON fixture to ``tests/test_backtest/fixtures/BTC_USDT_5m.json``.

Guard: refuses to export if live and Backtester disagree on the target window.
Run only after ``test_cross_validate.py`` passes green on the same window.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from crypto_bot.config.env import Config
from crypto_bot.config.settings import load_settings
from crypto_bot.core.types import Candle
from crypto_bot.features.context import SymbolMarketContext
from crypto_bot.simulation.backtester import Backtester
from crypto_bot.simulation.decision_aggregate import aggregate_raw_rows
from crypto_bot.simulation.decision_diff import compare_decisions
from crypto_bot.simulation.historical_source import HistoricalCandleSource
from crypto_bot.simulation.market_constants import MARKET_QUOTE_VOLUME
from crypto_bot.storage.db import Database

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO_ROOT / "tests" / "test_backtest" / "fixtures"
DECISIONS_COLUMNS = ("ts_ms", "symbol", "timeframe", "accepted", "reject_reason", "score", "signal")
SCORE_TOLERANCE = 2.0
FIX_CUTOFF_MS = 1784029876819  # общий cutoff для всех символов — дата рестарта бота после фиксов


# --------------------------------------------------------------------------- #
# Helpers (mirror test_cross_validate.py)
# --------------------------------------------------------------------------- #

def _read_decisions(db_path: Path, symbol: str, timeframe: str, start_ms: int, end_ms: int) -> list[dict]:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            f"""SELECT {', '.join(DECISIONS_COLUMNS)}
                FROM decisions
                WHERE symbol = ? AND timeframe = ? AND ts_ms BETWEEN ? AND ?
                ORDER BY ts_ms""",
            (symbol, timeframe, start_ms, end_ms),
        ).fetchall()
    finally:
        con.close()
    return [dict(r) for r in rows]


def _load_all_candles(db_path: Path, symbol: str, timeframe: str) -> list[Candle]:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT ts_ms, open, high, low, close, volume FROM candles "
            "WHERE symbol=? AND timeframe=? ORDER BY ts_ms ASC",
            (symbol, timeframe),
        ).fetchall()
    finally:
        con.close()
    return [
        Candle(timestamp=int(r["ts_ms"]), open=float(r["open"]),
               high=float(r["high"]), low=float(r["low"]),
               close=float(r["close"]), volume=float(r["volume"]))
        for r in rows
    ]


def _candle_to_dict(c: Candle) -> dict:
    return {"timestamp": c.timestamp, "open": c.open, "high": c.high,
            "low": c.low, "close": c.close, "volume": c.volume}


def _period_ms_for_timeframe(tf: str) -> int:
    from crypto_bot.core.policy import timeframe_to_seconds
    return timeframe_to_seconds(tf) * 1000


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def export(symbol: str, timeframe: str, live_db_path: Path, config_path: str) -> None:
    config = load_settings(config_path)
    settings = config.settings
    quote = settings.universe.quote.upper()

    live_raw = _read_decisions(live_db_path, symbol, timeframe, FIX_CUTOFF_MS, 2**63 - 1)
    if not live_raw:
        print(f"no decisions for {symbol} {timeframe} in {live_db_path}")
        return

    start_ms = live_raw[0]["ts_ms"]
    period_ms = _period_ms_for_timeframe(timeframe)
    # Extend end_ms past the last live decision's open-time so the backtester
    # clock includes the bar that produced that decision
    end_ms = live_raw[-1]["ts_ms"] + period_ms

    # Load candles — primary symbol + BTC + ETH for correlations
    primary = _load_all_candles(live_db_path, symbol, timeframe)
    btc = _load_all_candles(live_db_path, f"BTC/{quote}", timeframe)
    eth = _load_all_candles(live_db_path, f"ETH/{quote}", timeframe)

    if not primary:
        print(f"no candles for {symbol} {timeframe}")
        return

    source = HistoricalCandleSource()
    source.load_all(symbol, timeframe, primary)
    for extra_sym, extra_candles in [(f"BTC/{quote}", btc), (f"ETH/{quote}", eth)]:
        if extra_candles:
            source.load_all(extra_sym, timeframe, extra_candles)

    # market_map — from live decisions window (approximate 24h quote volume)
    market_map = {symbol: SymbolMarketContext(quote_volume_24h=MARKET_QUOTE_VOLUME.get(symbol, 429_000_000))}

    # Run Backtester NOW on the same window
    from tempfile import TemporaryDirectory
    with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        bt_db_path = Path(tmp) / "bt_check.db"
        bt = Backtester(
            config,
            symbols=[symbol],
            timeframes=[timeframe],
            start_ms=start_ms,
            end_ms=end_ms,
            source=source,
            market_map=market_map,
            db=Database(bt_db_path),
        )
        bt.run()
        bt_raw = _read_decisions(bt_db_path, symbol, timeframe, start_ms, end_ms)

    # Guard: refuse to export if backtest misses bars it should have seen,
    # or if decisions on the same bar disagree.
    # missing_in_live is tolerated — the live bot legitimately skips cycles.
    live_agg = aggregate_raw_rows(live_raw)
    bt_agg = aggregate_raw_rows(bt_raw)

    report = compare_decisions(live_agg, bt_agg, score_tolerance=SCORE_TOLERANCE, accept_liquidity_mismatches=True)
    if report.missing_in_backtest or report.mismatches:
        raise RuntimeError(
            f"Refusing to export snapshot for {symbol} {timeframe}: "
            f"live and backtest diverge right now.\n{report.summary()}\n"
            "Snapshot must capture a moment of CONFIRMED agreement."
        )

    # Build fixture
    fixture = {
        "symbol": symbol,
        "timeframe": timeframe,
        "config_snapshot": settings.model_dump(mode="json", include={
            "strategy", "scoring", "filters", "risk", "timeframes",
        }),
        "candles": [_candle_to_dict(c) for c in primary],
        "candles_btc": [_candle_to_dict(c) for c in btc],
        "candles_eth": [_candle_to_dict(c) for c in eth],
        "market_map": {"quote_volume_24h": MARKET_QUOTE_VOLUME.get(symbol, 429_000_000)},
        "decisions": [dict(r) for r in live_raw],  # from LIVE, not backtest_now
        "exported_at_ms": int(datetime.now(tz=UTC).timestamp() * 1000),
    }

    # Write
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"{symbol.replace('/', '_')}_{timeframe}"
    dest = FIXTURE_DIR / f"{stem}.json"
    dest.write_text(json.dumps(fixture, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"exported {len(live_raw)} decisions + {len(primary)} candles -> {dest}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Export validation fixture")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--timeframe", required=True)
    parser.add_argument("--live-db", default="data/crypto_bot.db", type=Path)
    parser.add_argument("--config", default="config/settings.yaml")
    args = parser.parse_args()
    export(args.symbol, args.timeframe, args.live_db, args.config)