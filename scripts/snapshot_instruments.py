#!/usr/bin/env python3
"""
scripts/snapshot_instruments.py

Fetch Bybit instrument specs — ``category=linear`` and ``category=spot`` — from
mainnet into a versioned snapshot for backtests. Metadata only: no prices and
no funding rates are requested.

The snapshot records its source and the time of the fetch, is written read-only
and never overwritten, and is pinned by its sha256: a registered run passes it
as ``--instrument-snapshot`` with ``--instrument-snapshot-sha256``. The 24-hour
cache is not used — it holds whatever list the last fetch returned, testnet or
mainnet (backlog item 13).

The summary says how many specs of each market carry ``launchTime``: admission
by ``launchTime <= t`` works only for those.

Usage::

    python scripts/snapshot_instruments.py [--out-dir data/instruments]
"""
from __future__ import annotations

import argparse
import asyncio
import os
import stat
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from crypto_bot.config.env import Config
from crypto_bot.config.settings import load_settings
from crypto_bot.data.instruments import (
    MAINNET_URL,
    BybitInstrumentsClient,
    InstrumentSpec,
    write_snapshot,
)

ROOT = Path(__file__).resolve().parents[1]
SHOWN = ("BTCUSDT", "ETHUSDT")


def _utc(ms: int | None) -> str:
    if ms is None:
        return "none"
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M")


async def _fetch(config: Config) -> tuple[list[InstrumentSpec], list[InstrumentSpec]]:
    async with BybitInstrumentsClient(config, base_url=MAINNET_URL) as client:
        linear = await client.fetch_instruments("linear")
        spot = await client.fetch_instruments("spot")
    return linear, spot


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out-dir", type=Path, default=ROOT / "data" / "instruments")
    args = parser.parse_args()

    config = load_settings(yaml_path=ROOT / "config" / "settings.yaml")
    fetched_at_ms = int(time.time() * 1000)
    linear, spot = asyncio.run(_fetch(config))
    if not linear or not spot:
        print(f"FAIL: empty response — linear {len(linear)}, spot {len(spot)}")
        return 1

    args.out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromtimestamp(fetched_at_ms / 1000, tz=UTC).strftime("%Y-%m-%dT%H%MZ")
    path = args.out_dir / f"bybit_instruments_{stamp}.json"
    digest = write_snapshot(path, linear + spot, api_base_url=MAINNET_URL, fetched_at_ms=fetched_at_ms)
    os.chmod(path, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)

    perpetuals = [s for s in linear if s.contract_type == "LinearPerpetual"]
    shown_path = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path
    print(f"snapshot {shown_path.as_posix()}")
    print(f"source {MAINNET_URL}; fetched_at {_utc(fetched_at_ms)} UTC; sha256 {digest}")
    for name, specs in (("linear", linear), ("spot", spot)):
        with_launch = sum(s.launch_time_ms is not None for s in specs)
        print(f"{name}: {len(specs)} specs, {with_launch} with launchTime")
    print(f"linear perpetuals: {len(perpetuals)}")
    for market, specs in (("linear", linear), ("spot", spot)):
        by_symbol = {s.symbol: s for s in specs}
        for symbol in SHOWN:
            s = by_symbol.get(symbol)
            if s is None:
                print(f"{market:6} {symbol}: no spec")
                continue
            print(
                f"{market:6} {symbol}: {s.contract_type} {s.status} qty_step {s.qty_step:g} "
                f"min_notional {s.min_notional_value:g} launch {_utc(s.launch_time_ms)}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
