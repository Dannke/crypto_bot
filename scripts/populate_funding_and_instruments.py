#!/usr/bin/env python3
"""
Fetch funding rates and instrument specs from Bybit and populate the database.

Usage:
    python scripts/populate_funding_and_instruments.py \
        --symbols BTC/USDT ETH/USDT SOL/USDT XRP/USDT AVAX/USDT ADA/USDT DOGE/USDT BNB/USDT POL/USDT \
        --start 2024-01-01 --end 2024-02-01 \
        --db data/crypto_bot.db
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

from crypto_bot.config.env import Config
from crypto_bot.config.settings import load_settings
from crypto_bot.core.enums import Mode
from crypto_bot.data.funding import BybitFundingClient, FundingRepository
from crypto_bot.data.instruments import InstrumentCache, build_instrument_cache
from crypto_bot.storage.db import Database


SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT",
    "AVAX/USDT", "ADA/USDT", "DOGE/USDT", "BNB/USDT", "POL/USDT",
]


def _iso_to_ms(value: str) -> int:
    return int(datetime.fromisoformat(value).replace(tzinfo=UTC).timestamp() * 1000)


async def fetch_and_store_funding(
    config: Config,
    symbols: list[str],
    start_ms: int,
    end_ms: int,
    db_path: str,
) -> None:
    """Fetch funding rates from Bybit and store in database."""
    db = Database(db_path)
    repo = FundingRepository(db)

    async with BybitFundingClient(config) as client:
        for sym in symbols:
            print(f"Fetching funding for {sym}...")
            try:
                events = await client.fetch_funding_history(sym, start_ms, end_ms)
                if events:
                    count = repo.upsert_many(events)
                    print(f"  {sym}: stored {count} funding events")
                else:
                    print(f"  {sym}: no funding events found")
            except Exception as exc:
                print(f"  {sym}: ERROR - {exc}")

    db.close()


async def fetch_and_store_instruments(config: Config, db_path: str) -> None:
    """Fetch instrument specs from Bybit and cache them."""
    print("Fetching instrument specs from Bybit...")
    cache = await build_instrument_cache(config)

    tradable = cache.get_tradable_symbols()
    print(f"Loaded {len(tradable)} tradable linear perpetuals")

    # Verify our target symbols
    for sym in SYMBOLS:
        spec = cache.get_spec(sym)
        if spec:
            print(f"  {sym}: qtyStep={spec.qty_step}, minNotional={spec.min_notional_value}, "
                  f"maxLeverage={spec.max_leverage}, tickSize={spec.tick_size}")
        else:
            print(f"  {sym}: NOT FOUND in instrument cache")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--symbols", nargs="+", default=SYMBOLS, metavar="SYM",
        help="Symbols to fetch funding for (default: all 9 validation symbols)",
    )
    parser.add_argument(
        "--start", default="2024-01-01", metavar="ISO",
        help="Start date for funding history (ISO, default: 2024-01-01)",
    )
    parser.add_argument(
        "--end", default="2024-02-01", metavar="ISO",
        help="End date for funding history (ISO, default: 2024-02-01)",
    )
    parser.add_argument(
        "--db", default="data/crypto_bot.db",
        help="Path to SQLite database (default: data/crypto_bot.db)",
    )
    parser.add_argument(
        "--config", default="config/settings.yaml",
        help="Path to settings YAML (default: config/settings.yaml)",
    )
    args = parser.parse_args()

    start_ms = _iso_to_ms(args.start)
    end_ms = _iso_to_ms(args.end)

    print(f"Fetching funding rates for {len(args.symbols)} symbols")
    print(f"Period: {args.start} to {args.end}")
    print(f"Database: {args.db}")

    config = load_settings(yaml_path=Path(args.config))
    settings = config.settings
    settings.runtime.mode = Mode.PAPER
    config = Config(settings=settings, env=config.env)

    # Fetch instrument specs first (needed for position sizing)
    await fetch_and_store_instruments(config, args.db)

    # Fetch funding rates
    await fetch_and_store_funding(config, args.symbols, start_ms, end_ms, args.db)

    print("\nDone!")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))