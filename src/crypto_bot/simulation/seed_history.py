"""Seed historical OHLCV from exchange into the local SQLite DB.

Forces mainnet by default (testnet has sparse historical data).
Override with ``--network testnet`` or ``--network auto`` (follows config).

Usage::

    python -m crypto_bot.simulation.seed_history BTC/USDT 1h --start 2025-01-01 --end 2025-02-01
    python -m crypto_bot.simulation.seed_history ETH/USDT 1h --network auto
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace as dataclass_replace
from datetime import datetime
from pathlib import Path

from ..config.settings import load_settings
from ..core.types import Candle
from ..data.exchange import MarketDataClient
from ..storage.db import Database, Repositories


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed historical OHLCV data from exchange")
    parser.add_argument("symbol", help="Trading pair (e.g. BTC/USDT)")
    parser.add_argument("timeframe", help="Timeframe (e.g. 1h, 4h, 1d)")
    parser.add_argument("--start", default=None, help="Start datetime (ISO format, e.g. 2025-01-01)")
    parser.add_argument("--end", default=None, help="End datetime (ISO format)")
    parser.add_argument("--network", default="mainnet", choices=["mainnet", "testnet", "auto"],
                        help="Exchange network (default: mainnet)")
    parser.add_argument("--config", default="config/settings.yaml", help="Path to settings YAML")
    return parser.parse_args()


async def _run(
    symbol: str = "",
    timeframe: str = "",
    start: str | None = None,
    end: str | None = None,
    network: str = "mainnet",
    config_path: str = "config/settings.yaml",
) -> None:
    config = load_settings(yaml_path=Path(config_path))

    # Override network
    if network == "mainnet":
        new_env = config.env.model_copy(update={"exchange_sandbox": False})
        config = dataclass_replace(config, env=new_env)
    elif network == "testnet":
        new_env = config.env.model_copy(update={"exchange_sandbox": True})
        config = dataclass_replace(config, env=new_env)

    now = datetime.now()
    end_ms = int(datetime.fromisoformat(end).timestamp() * 1000) if end else int(now.timestamp() * 1000)
    start_ms = int(datetime.fromisoformat(start).timestamp() * 1000) if start else end_ms - 30 * 86400 * 1000

    db = Database(config.settings.storage.db_path)
    repos = Repositories(db)
    limit = 200

    total = 0
    since = start_ms

    async with MarketDataClient(config) as client:
        while since < end_ms:
            raw = await client.fetch_ohlcv(symbol, timeframe, limit=limit, since=since)
            if not raw:
                break

            candles = []
            for r in raw:
                ts = int(r[0])
                if ts > end_ms:
                    break
                candles.append(Candle(
                    timestamp=ts, open=float(r[1]), high=float(r[2]),
                    low=float(r[3]), close=float(r[4]), volume=float(r[5]),
                ))

            if not candles:
                break

            inserted = repos.candles.upsert_many(symbol, timeframe, candles)
            total += inserted

            last_ts = candles[-1].timestamp
            since = last_ts + 1
            last_dt = datetime.fromtimestamp(last_ts / 1000)

            print(f"  {len(candles):>3} candles, {inserted} inserted, up to {last_dt}")

    print(f"Done: {total} candles seeded for {symbol} {timeframe}")
    db.close()


def main() -> None:
    args = _parse_args()
    asyncio.run(_run(
        symbol=args.symbol,
        timeframe=args.timeframe,
        start=args.start,
        end=args.end,
        network=args.network,
        config_path=args.config,
    ))


if __name__ == "__main__":
    main()
