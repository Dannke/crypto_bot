#!/usr/bin/env python3
"""
scripts/inspect_regime.py

R3: Isolated regime classifier validation CLI.

Runs the RegimeClassifier on historical Bybit data and outputs a CSV timeline
for human review. This is NOT part of the production pipeline - it's a
diagnostic tool to verify classifier behavior on real data before gating
strategies with it.

Usage::
    # Default: BTC/USDT 1h, last 30 days from local DB
    python scripts/inspect_regime.py

    # Custom symbols, timeframe, window
    python scripts/inspect_regime.py --symbols BTC/USDT ETH/USDT --timeframe 1h --days 90

    # Output to custom CSV
    python scripts/inspect_regime.py --out regime_timeline.csv

    # With Bybit API to fetch missing data (requires API keys in .env)
    python scripts/inspect_regime.py --fetch-missing
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import sys
from datetime import UTC, datetime
from pathlib import Path

from crypto_bot.config.env import Config
from crypto_bot.config.settings import load_settings
from crypto_bot.config.schemas import RegimeConfig
from crypto_bot.core.enums import Mode
from crypto_bot.data.exchange import MarketDataClient
from crypto_bot.data.feed import Feed, resolve_symbols
from crypto_bot.portfolio.regime import classify_regime, RegimeConfig
from crypto_bot.simulation.historical_source import HistoricalCandleSource
from crypto_bot.storage.db import CandleRepository, Database


def _iso_to_ms(value: str) -> int:
    return int(datetime.fromisoformat(value).replace(tzinfo=UTC).timestamp() * 1000)


def _ms_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


async def _load_candles_from_db(db_path: str, symbols: list[str], timeframe: str, start_ms: int, end_ms: int) -> dict[str, list]:
    """Load candles from local SQLite DB."""
    db = Database(db_path)
    repo = CandleRepository(db)
    source = HistoricalCandleSource(repo)

    result = {}
    for sym in symbols:
        try:
            await source.load_all_async(sym, timeframe)
        except Exception:
            pass
        candles = source.slice_between(start_ms, end_ms, sym, timeframe)
        result[sym] = candles
    db.close()
    return result


async def _fetch_missing_candles(client: MarketDataClient, symbols: list[str], timeframe: str, start_ms: int, end_ms: int) -> dict[str, list]:
    """Fetch candles from exchange for symbols missing in DB."""
    from crypto_bot.core.types import Candle
    from crypto_bot.data.feed import _row_to_candle

    result = {}
    for sym in symbols:
        try:
            raw = await client.fetch_ohlcv(sym, timeframe, limit=1000, since=start_ms)
            candles = [_row_to_candle(r) for r in raw]
            candles = [c for c in candles if start_ms <= c.timestamp <= end_ms]
            result[sym] = candles
        except Exception as e:
            print(f"Warning: failed to fetch {sym} {timeframe}: {e}", file=sys.stderr)
    return result


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Inspect regime classifier on historical data",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--symbols", nargs="+", default=["BTC/USDT"],
        help="Symbols to analyze (default: BTC/USDT)"
    )
    parser.add_argument("--timeframe", default="1h", help="Timeframe (15m, 1h, 4h)")
    parser.add_argument(
        "--start", default=None,
        help="Start date (ISO, default: 30 days ago)"
    )
    parser.add_argument(
        "--end", default=None,
        help="End date (ISO, default: now)"
    )
    parser.add_argument(
        "--days", type=int, default=30,
        help="Days of history if --start not given"
    )
    parser.add_argument(
        "--db", default="data/crypto_bot.db",
        help="Path to SQLite DB"
    )
    parser.add_argument(
        "--out", default="regime_timeline.csv",
        help="Output CSV file"
    )
    parser.add_argument(
        "--fetch-missing", action="store_true",
        help="Fetch missing candles from Bybit API (requires .env)"
    )
    parser.add_argument(
        "--config", default="config/settings.yaml",
        help="Path to settings YAML"
    )
    parser.add_argument(
        "--reference", choices=["btc_only", "universe_basket"],
        default="universe_basket",
        help="Reference for regime calculation"
    )
    parser.add_argument(
        "--trend-threshold", type=float, default=25.0,
        help="ADX threshold for trend vs range"
    )
    parser.add_argument(
        "--vol-lookback", type=int, default=168,
        help="Lookback bars for volatility percentile"
    )
    parser.add_argument(
        "--vol-percentile-high", type=float, default=0.75,
        help="Percentile threshold for high vol regime"
    )
    parser.add_argument(
        "--hysteresis", type=int, default=0,
        help="Minimum dwell bars before regime change (0 = disabled)"
    )
    args = parser.parse_args()

    end_ms = _iso_to_ms(args.end) if args.end else int(datetime.now(tz=UTC).timestamp() * 1000)
    start_ms = _iso_to_ms(args.start) if args.start else end_ms - args.days * 24 * 3_600_000

    # Load settings for DB path and other config
    settings = load_settings(yaml_path=Path(args.config))
    config_obj = Config(settings=settings, env=settings.env if hasattr(settings, 'env') else None)

    print(f"Loading candles for {len(args.symbols)} symbols ({args.timeframe})")
    print(f"  Window: {_ms_to_iso(start_ms)} -> {_ms_to_iso(end_ms)}")
    print(f"  DB: {args.db}")
    print(f"  Reference: {args.reference}")

    # Load from DB
    candles_by_symbol = await _load_candles_from_db(
        args.db, args.symbols, args.timeframe, start_ms, end_ms
    )

    # Optionally fetch missing from API
    if args.fetch_missing:
        print("Fetching missing candles from Bybit...")
        async with MarketDataClient(config_obj) as client:
            missing = await _fetch_missing_candles(
                client, args.symbols, args.timeframe, start_ms, end_ms
            )
            for sym, candles in missing.items():
                if sym not in candles_by_symbol or not candles_by_symbol[sym]:
                    candles_by_symbol[sym] = candles
                    print(f"  Fetched {len(candles)} candles for {sym}")

    # Filter symbols with data
    symbols_with_data = [s for s in args.symbols if candles_by_symbol.get(s)]
    if not symbols_with_data:
        print("Error: No candle data available for any symbol", file=sys.stderr)
        return

    print(f"Symbols with data: {symbols_with_data}")
    candles_by_symbol = {s: candles_by_symbol[s] for s in symbols_with_data}

    # Build regime config
    regime_config = RegimeConfig(
        enabled=True,
        reference=args.reference,
        trend_period=14,
        trend_threshold=args.trend_threshold,
        vol_lookback_bars=args.vol_lookback,
        vol_percentile_high=args.vol_percentile_high,
        hysteresis_min_dwell_bars=args.hysteresis,
    )

    # Run classifier at each bar close of the reference timeframe
    # Use the first symbol as reference for the unified clock
    from crypto_bot.core.policy import timeframe_to_seconds
    period_ms = timeframe_to_seconds(args.timeframe) * 1000

    # Get unified clock from reference symbol (first symbol)
    ref_symbol = symbols_with_data[0]
    ref_candles = candles_by_symbol[ref_symbol]
    if not ref_candles:
        print(f"Error: No reference candles for {ref_symbol}", file=sys.stderr)
        return

    # Build timeline: each bar close is a classification point
    timeline = []
    last_regime = None
    bars_since_change = 0
    for c in ref_candles:
        as_of = c.timestamp + period_ms
        if as_of < start_ms or as_of > end_ms:
            continue

        # Slice all symbols up to this point
        from crypto_bot.portfolio.regime import _slice_closed_bars
        sliced = {
            sym: _slice_closed_bars(candles_by_symbol[sym], as_of, args.timeframe)
            for sym in symbols_with_data
        }

        try:
            result = classify_regime(
                sliced,
                as_of,
                regime_config,
                quote="USDT",
                timeframe=args.timeframe,
            )
            raw_regime = result.regime
            raw_ts = result.trend_strength
            raw_vp = result.vol_percentile

            # Apply hysteresis: only allow regime change after min_dwell_bars
            if args.hysteresis > 0:
                if last_regime is None:
                    # First regime
                    pass
                elif raw_regime != last_regime:
                    if bars_since_change < args.hysteresis:
                        # Suppress change - keep previous regime
                        raw_regime = last_regime
                    else:
                        # Allow change
                        bars_since_change = 0
                else:
                    bars_since_change += 1
            else:
                bars_since_change = 0

            last_regime = raw_regime
            timeline.append({
                "as_of_ms": as_of,
                "as_of_iso": _ms_to_iso(as_of),
                "regime": raw_regime,
                "trend_strength": f"{raw_ts:.4f}",
                "vol_percentile": f"{raw_vp:.4f}",
                "reference_universe": ",".join(result.reference_universe),
            })
        except Exception as e:
            timeline.append({
                "as_of_ms": as_of,
                "as_of_iso": _ms_to_iso(as_of),
                "regime": "ERROR",
                "trend_strength": "",
                "vol_percentile": "",
                "reference_universe": str(e),
            })

    # Write CSV
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "as_of_ms", "as_of_iso", "regime", "trend_strength", "vol_percentile", "reference_universe"
        ])
        writer.writeheader()
        writer.writerows(timeline)

    print(f"Wrote {len(timeline)} rows to {out_path}")

    # Summary stats
    if timeline:
        regimes = [r["regime"] for r in timeline if r["regime"] != "ERROR"]
        from collections import Counter
        counts = Counter(regimes)
        print("\nRegime distribution:")
        for regime, count in counts.most_common():
            pct = count / len(regimes) * 100
            print(f"  {regime}: {count} ({pct:.1f}%)")

        # Regime changes
        changes = 0
        prev = None
        for r in timeline:
            if r["regime"] != "ERROR" and r["regime"] != prev:
                if prev is not None:
                    changes += 1
                    print(f"  Change: {prev} -> {r['regime']} at {r['as_of_iso']}")
                prev = r["regime"]
        print(f"Total regime changes: {changes}")

        if args.hysteresis > 0:
            print(f"\nHysteresis (min_dwell_bars={args.hysteresis}) would suppress some changes.")


if __name__ == "__main__":
    asyncio.run(main())