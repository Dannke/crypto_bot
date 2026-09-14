#!/usr/bin/env python3
"""
Replace a symbol in the candle database with a valid one.

Usage:
    python scripts/replace_symbol.py --old LINK/USDT --new DOT/USDT --db data/crypto_bot.db
    python scripts/replace_symbol.py --old LINK/USDT --new MATIC/USDT --db data/crypto_bot.db --timeframe 1h --days 365
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime

import ccxt

from crypto_bot.config.settings import load_settings
from crypto_bot.config.env import Config
from crypto_bot.core.enums import Mode
from crypto_bot.storage.db import Database


def fetch_and_store_candles(
    ex,
    symbol: str,
    timeframe: str,
    start_ts: int,
    end_ts: int,
    db,
) -> int:
    """Fetch candles from exchange and store in database."""
    from crypto_bot.core.types import Candle
    from crypto_bot.storage.db import CandleRepository
    
    repo = CandleRepository(db)
    
    print(f"Fetching {symbol} {timeframe} candles from {datetime.fromtimestamp(start_ts/1000, UTC)} to {datetime.fromtimestamp(end_ts/1000, UTC)}...")
    
    all_candles = []
    limit = 200
    current_since = start_ts
    
    while current_since < end_ts:
        candles = ex.fetch_ohlcv(symbol, timeframe, limit=limit, since=current_since)
        if not candles:
            break
        all_candles.extend(candles)
        # Advance to after the last candle
        current_since = candles[-1][0] + 1
        if len(candles) < limit:
            break
    
    if not all_candles:
        print(f"  No candles returned for {symbol}")
        return 0
    
    # Filter to our time range
    filtered = [c for c in all_candles if start_ts <= c[0] <= end_ts]
    
    # Convert to Candle objects and store in batch
    candles_list = []
    for candle_data in filtered:
        try:
            c = Candle(
                timestamp=candle_data[0],
                open=candle_data[1],
                high=candle_data[2],
                low=candle_data[3],
                close=candle_data[4],
                volume=candle_data[5],
            )
            candles_list.append(c)
        except Exception as e:
            print(f"  Error creating candle: {e}")
    
    if candles_list:
        repo = CandleRepository(db)
        repo.upsert_many(symbol, timeframe, candles_list)
        stored = len(candles_list)
        print(f"  Stored {stored} candles for {symbol}")
        return stored
    
    print(f"  No valid candles to store for {symbol}")
    return 0


def delete_symbol_candles(db, symbol: str, timeframe: str) -> int:
    """Delete all candles for a symbol/timeframe."""
    cur = db.conn.cursor()
    cur.execute(
        "DELETE FROM candles WHERE symbol = ? AND timeframe = ?",
        (symbol, timeframe)
    )
    deleted = cur.rowcount
    db.conn.commit()
    print(f"Deleted {deleted} candles for {symbol} {timeframe}")
    return deleted


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old", required=True, help="Symbol to replace (e.g., LINK/USDT)")
    parser.add_argument("--new", required=True, help="New symbol (e.g., DOT/USDT, MATIC/USDT, TRX/USDT)")
    parser.add_argument("--db", default="data/crypto_bot.db", help="Path to SQLite database")
    parser.add_argument("--timeframe", default="1h", help="Timeframe (15m, 1h, 4h)")
    parser.add_argument("--days", type=int, default=365, help="Days of history to fetch")
    parser.add_argument("--config", default="config/settings.yaml", help="Path to settings YAML")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without making changes")
    args = parser.parse_args()

    # Valid replacement symbols
    valid_symbols = {"DOT/USDT", "MATIC/USDT", "TRX/USDT", "AVAX/USDT", "ADA/USDT", "SOL/USDT"}
    if args.new not in valid_symbols:
        print(f"Warning: {args.new} not in recommended list: {valid_symbols}")
        response = input("Continue anyway? (y/N): ")
        if response.lower() != 'y':
            return 1

    # Load config
    config = load_settings(yaml_path=args.config)
    settings = config.settings
    settings.runtime.mode = Mode.PAPER
    config = Config(settings=settings, env=config.env)

    # Initialize exchange
    exchange_name = config.env.exchange_name or config.settings.exchange.name
    sandbox = (
        config.env.exchange_sandbox
        if config.env.exchange_sandbox is not None
        else config.settings.exchange.sandbox
    )
    opts = {
        "enableRateLimit": True,
        "rateLimit": config.settings.exchange.rate_limit_ms,
        "timeout": 30000,
        "options": {"defaultType": "swap"},
    }
    ex = getattr(ccxt, exchange_name)(opts)
    ex.set_sandbox_mode(sandbox)
    ex.load_markets()

    db = Database(args.db)

    try:
        # Calculate time range
        end_ts = int(datetime.now(UTC).timestamp() * 1000)
        start_ts = end_ts - args.days * 24 * 3600 * 1000

        print(f"Replacing {args.old} with {args.new}")
        print(f"Timeframe: {args.timeframe}, Days: {args.days}")
        print(f"Database: {args.db}")

        if args.dry_run:
            print("DRY RUN - no changes will be made")
            return 0

        # Delete old symbol candles
        print(f"\n1. Removing old symbol {args.old}...")
        delete_symbol_candles(db, args.old, args.timeframe)

        # Fetch and store new symbol candles
        print(f"\n2. Fetching and storing new symbol {args.new}...")
        stored = fetch_and_store_candles(
            ex, args.new, args.timeframe, start_ts, end_ts, db
        )

        # Verify
        from crypto_bot.storage.db import CandleRepository
        repo = CandleRepository(db)
        count = await repo.count(args.new, args.timeframe)
        print(f"\n3. Verification: {count} candles now stored for {args.new} {args.timeframe}")

        if count > 0:
            print(f"\n✓ Successfully replaced {args.old} with {args.new}")
            return 0
        else:
            print(f"\n✗ Failed: no candles stored for {args.new}")
            return 1

    except Exception as e:
        print(f"Error: {e}")
        return 1
    finally:
        db.close()
        try:
            ex.close()
        except Exception:
            pass


if __name__ == "__main__":
    import asyncio
    sys.exit(asyncio.run(main()))