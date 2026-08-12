"""Reproduce the 1h train walk_forward with a preserved DB to inspect positions."""
import argparse
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from crypto_bot.config.env import Config
from crypto_bot.config.settings import load_settings
from crypto_bot.core.enums import Mode
from crypto_bot.core.policy import timeframe_to_seconds
from crypto_bot.core.types import Candle
from crypto_bot.simulation.backtester import Backtester
from crypto_bot.simulation.historical_source import HistoricalCandleSource
from crypto_bot.simulation.market_constants import MARKET_QUOTE_VOLUME
from crypto_bot.features.context import SymbolMarketContext
from crypto_bot.storage.db import CandleRepository, Database


def calendar_split(
    candles: list[Candle],
    period_ms: int,
    split_ratio: float,
) -> tuple[int, int, int, int]:
    first_ts = candles[0].timestamp
    last_close = candles[-1].timestamp + period_ms
    total_range = last_close - first_ts
    split_ts = first_ts + int(total_range * split_ratio)
    return first_ts, split_ts, split_ts, last_close


def fetch_candles(db_path: str, symbol: str, timeframe: str):
    db = Database(db_path)
    try:
        repo = CandleRepository(db)
        import asyncio
        return asyncio.run(repo.fetch_since(symbol, timeframe, since_ts=0))
    finally:
        db.close()


def fetch_all_candles(db_path: str, symbols: list[str], timeframe: str):
    result = {}
    for sym in symbols:
        print(f"  {sym} ...", end=" ", flush=True)
        candles = fetch_candles(db_path, sym, timeframe)
        print(f"{len(candles)} bars")
        result[sym] = candles
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/settings.yaml")
    parser.add_argument("--db", default="data/crypto_bot.db")
    parser.add_argument("--timeframe", default="1h")
    parser.add_argument("--split", type=float, default=0.8)
    parser.add_argument("--out", default=None, help="Path to keep the temp DB (default: auto temp)")
    args = parser.parse_args()

    symbols = list(MARKET_QUOTE_VOLUME.keys())

    config_obj = load_settings(yaml_path=Path(args.config))
    config_obj.settings.runtime.mode = Mode.PAPER
    config = Config(settings=config_obj.settings, env=config_obj.env)

    print(f"Loading candles for {len(symbols)} symbols ({args.timeframe}) from {args.db} ...")
    all_candles = fetch_all_candles(args.db, symbols, args.timeframe)

    first_sym = symbols[0]
    first_candles = all_candles.get(first_sym, [])
    if not first_candles:
        print(f"FAIL: no candles for {first_sym}")
        sys.exit(1)

    period_ms = timeframe_to_seconds(args.timeframe) * 1000
    train_start, train_end, test_start, test_end = calendar_split(first_candles, period_ms, args.split)

    def tf(ts):
        return datetime.fromtimestamp(ts / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M")

    print(f"  Train window:  {tf(train_start)}  ->  {tf(train_end)}")

    source = HistoricalCandleSource()
    for sym, candles in all_candles.items():
        source.load_all(sym, args.timeframe, candles)

    market_map = {}
    for sym in symbols:
        qv = MARKET_QUOTE_VOLUME.get(sym, 1_000_000_000)
        market_map[sym] = SymbolMarketContext(quote_volume_24h=qv)

    if args.out:
        db_path = args.out
    else:
        tmp = tempfile.NamedTemporaryFile(suffix="_train.db", delete=False)
        db_path = tmp.name
        tmp.close()

    print(f"Running TRAIN (DB: {db_path}) ...")
    bt = Backtester(
        config,
        symbols=symbols,
        timeframes=[args.timeframe],
        start_ms=train_start,
        end_ms=train_end,
        source=source,
        market_map=market_map,
        db=Database(db_path),
    )
    summary = bt.run()

    print(f"\n{'='*60}")
    print(f"Halt triggered: {bt.emergency_halt_triggered}")
    print(f"Trades: {summary.total_trades}  PnL: {summary.total_pnl_pct}%  MaxDD: {summary.max_drawdown_pct}%")

    # SQL dump
    import sqlite3
    con = sqlite3.connect(db_path)
    try:
        tables = con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        print(f"\nTables: {[r[0] for r in tables]}")

        for table_row in tables:
            tname = table_row[0]
            print(f"\n--- {tname} ---")
            try:
                rows = con.execute(f"SELECT * FROM \"{tname}\"").fetchall()
                cols = [d[0] for d in con.execute(f"PRAGMA table_info(\"{tname}\")").fetchall()]
                print(f"  Columns: {cols}")
                print(f"  Rows: {len(rows)}")
                for row in rows[:50]:
                    print(f"    {dict(row)}")
            except Exception as e:
                print(f"  Error: {e}")

        # Positions query
        print(f"\n--- POSITIONS ANALYSIS ---")
        halt_row = con.execute("SELECT * FROM positions WHERE closed_by='emergency_drawdown' LIMIT 1").fetchone()
        if halt_row:
            halt_cols = [d[0] for d in con.execute("PRAGMA table_info(positions)").fetchall()]
            halt_dict = dict(zip(halt_cols, halt_row))
            halt_time = halt_dict.get("closed_at_ms") or halt_dict.get("opened_at_ms")
            print(f"Emergency drawdown position: {halt_dict}")

            # All positions open at halt time
            query = """
                SELECT symbol, timeframe, side, opened_at_ms, closed_at_ms, closed_by, pnl_pct
                FROM positions
                WHERE opened_at_ms <= ?
                  AND (closed_at_ms IS NULL OR closed_at_ms >= ?)
                ORDER BY opened_at_ms;
            """
            rows = con.execute(query, (halt_time, halt_time)).fetchall()
            cols = [d[0] for d in con.execute("PRAGMA table_info(positions)").fetchall()]
            print(f"\nPositions open at halt moment ({len(rows)}):")
            for r in rows:
                print(f"  {dict(zip(cols, r))}")
        else:
            print("No emergency_drawdown positions found.")

        # All positions summary
        print(f"\nALL POSITIONS:")
        all_rows = con.execute("SELECT * FROM positions ORDER BY opened_at_ms").fetchall()
        for r in all_rows:
            print(f"  {dict(zip(cols, r))}")
    finally:
        con.close()

    if not args.out:
        print(f"\nTemp DB kept at: {db_path}")
    print("Done.")


if __name__ == "__main__":
    main()
