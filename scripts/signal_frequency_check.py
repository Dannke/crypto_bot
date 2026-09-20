#!/usr/bin/env python3
"""Signal frequency pre-check for Mean Reversion strategy.

This script scans historical data to estimate how often the mean reversion
signal (|z-score| >= entry_threshold) would trigger, to verify that
n_trades >= 200 is achievable in the test window.

Key features:
- Runs on the actual test segment (3-way split: 50/20/30) of available data
- Applies regime gating: filters out entries during trend_high_vol regime (exposure=0)
- Accounts for operational cadence: rebalance_hours, max_positions, holding period

Usage:
    python scripts/signal_frequency_check.py --db data/crypto_bot.db --symbols BTC/USDT ETH/USDT ... --entry-threshold 2.0 --zscore-window 48 --signal-lookback 8h
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from crypto_bot.config.env import Config, EnvConfig
from crypto_bot.config.settings import load_settings
from crypto_bot.core import policy
from crypto_bot.core.enums import Mode, Side
from crypto_bot.core.types import Candle
from crypto_bot.portfolio import get_market_snapshot, get_universe_snapshot
from crypto_bot.portfolio.mean_reversion_features import compute_zscore_snapshot
from crypto_bot.simulation.historical_source import HistoricalCandleSource


def _iso_to_ms(value: str) -> int:
    return int(datetime.fromisoformat(value).replace(tzinfo=UTC).timestamp() * 1000)


def _parse_duration_hours(value: str) -> int:
    """Parse a duration like '4h', '8h', '1d' to hours."""
    value = value.strip().lower()
    if value.endswith('h'):
        return int(value[:-1])
    if value.endswith('d'):
        return int(value[:-1]) * 24
    if value.endswith('w'):
        return int(value[:-1]) * 168
    raise ValueError(f"Unknown duration format: {value}")


def _load_regime_timeline(regime_csv_path: str) -> list[tuple[int, int, str]]:
    """Load regime timeline from CSV.
    
    Returns list of (start_ms, end_ms, regime) sorted by start_ms.
    """
    timeline = []
    with open(regime_csv_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row['regime'] in ('ERROR', ''):
                continue
            start_ms = int(row['as_of_ms'])
            regime = row['regime']
            timeline.append((start_ms, 0, regime))  # end_ms will be filled next
    
    # Fill in end_ms for each segment (next segment's start - 1ms)
    for i in range(len(timeline) - 1):
        timeline[i] = (timeline[i][0], timeline[i+1][0] - 1, timeline[i][2])
    # Last segment extends to infinity
    if timeline:
        timeline[-1] = (timeline[-1][0], 2**63 - 1, timeline[-1][2])
    
    return timeline


def _get_regime_at(timeline: list[tuple[int, int, str]], timestamp_ms: int) -> str:
    """Get regime at a given timestamp using binary search."""
    lo, hi = 0, len(timeline) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        start, end, regime = timeline[mid]
        if start <= timestamp_ms <= end:
            return regime
        elif timestamp_ms < start:
            hi = mid - 1
        else:
            lo = mid + 1
    return "unknown"


def _get_data_bounds(db_path: str, symbols: list[str], timeframe: str) -> tuple[int, int]:
    """Get the min and max timestamp available in candle data for the first symbol."""
    first_symbol = symbols[0]
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT MIN(ts_ms), MAX(ts_ms) FROM candles WHERE symbol=? AND timeframe=?",
            (first_symbol, timeframe)
        ).fetchone()
        if not row or row[0] is None:
            raise ValueError(f"No candle data for {first_symbol} {timeframe}")
        return row[0], row[1]


def _get_split_bounds(
    data_start_ms: int,
    data_end_ms: int,
    train_ratio: float = 0.5,
    validation_ratio: float = 0.2,
) -> tuple[int, int, int, int]:
    """Calculate 3-way split bounds (50/20/30) from data bounds.
    
    Returns: (train_start, train_end, val_end, test_end)
    """
    total_ms = data_end_ms - data_start_ms
    train_end = data_start_ms + int(total_ms * train_ratio)
    val_end = train_end + int(total_ms * validation_ratio)
    test_end = data_end_ms
    return data_start_ms, train_end, val_end, test_end


def run_signal_frequency_check(
    db_path: str,
    symbols: list[str],
    start_ms: int,
    end_ms: int,
    timeframe: str = "1h",
    zscore_window_bars: int = 48,
    signal_lookback_hours: int = 8,
    entry_threshold: float = 2.0,
    exit_threshold: float = 0.5,
    max_holding_bars: int = 48,
    rebalance_hours: int = 12,
    max_positions: int = 4,
    min_expected_edge_bps: int = 10,
    regime_csv_path: str = "data/regime_timeline.csv",
) -> dict:
    """
    Scan historical data and count how often the strategy would actually enter positions,
    respecting the strategy's operational cadence (rebalance_hours, max_positions, holding period)
    and regime gating (filters out entries during trend_high_vol regime where exposure=0).

    Returns:
        dict with signal counts, frequencies, and estimated n_trades
    """
    from crypto_bot.core.enums import Side

    config = load_settings(yaml_path=Path("config/settings.yaml"))
    config.settings.runtime.mode = Mode.PAPER
    config = Config(settings=config.settings, env=EnvConfig())

    bar_hours = policy.timeframe_to_seconds(timeframe) / 3600.0
    max_holding_hours = max_holding_bars * bar_hours

    # Load regime timeline
    regime_timeline = _load_regime_timeline(regime_csv_path)
    
    # Load candles - need extra bars before start_ms for warmup
    source = HistoricalCandleSource()
    for sym in symbols:
        rows = []
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                """SELECT ts_ms, open, high, low, close, volume 
                   FROM candles WHERE symbol=? AND timeframe=? AND ts_ms BETWEEN ? AND ?
                   ORDER BY ts_ms""",
                (sym, timeframe, start_ms - (zscore_window_bars + 1) * 3600_000, end_ms)
            ).fetchall()
        candles = [
            Candle(
                timestamp=r[0], open=r[1], high=r[2], low=r[3], close=r[4], volume=r[5]
            )
            for r in rows
        ]
        source.load_all(sym, timeframe, candles)

    # Simulate strategy evaluation at rebalance cadence
    rebalance_ms = rebalance_hours * 3600_000
    signal_lookback_bars = signal_lookback_hours  # at 1h timeframe
    
    # Track positions: dict[symbol] = (side, entry_ms)
    open_positions: dict[str, tuple[Side, int]] = {}
    entry_counts = {sym: 0 for sym in symbols}
    exit_counts = {sym: 0 for sym in symbols}
    exit_reversion_counts = {sym: 0 for sym in symbols}
    exit_time_stop_counts = {sym: 0 for sym in symbols}
    skipped_regime_counts = {sym: 0 for sym in symbols}  # entries skipped due to regime
    
    total_bars = 0
    rebalance_count = 0
    
    # Start at first rebalance after adjusted_start_ms
    current_ms = start_ms + rebalance_ms
    
    while current_ms <= end_ms:
        # Create snapshot at this timestamp
        universe = get_universe_snapshot(source, symbols, timeframe, current_ms)
        snapshot = get_market_snapshot(source, universe, timeframe)

        # Compute z-scores
        zs = compute_zscore_snapshot(
            snapshot,
            window_bars=zscore_window_bars,
            signal_lookback_bars=signal_lookback_bars,
        )

        # Check exits for existing positions
        symbols_to_close = []
        for sym, (_side, entry_ms) in open_positions.items():
            z = zs.zscores.get(sym, 0.0)
            age_hours = (current_ms - entry_ms) / 3_600_000
            
            # Exit conditions: |z| <= exit_threshold (reversion) OR age >= max_holding (time-stop).
            # max_holding задан в БАРАХ, поэтому переводим его в часы по таймфрейму —
            # сравнивать age_hours напрямую с числом баров верно только при 1h.
            exited = False
            exit_reason = ""
            if abs(z) <= exit_threshold:
                exited = True
                exit_reason = "reversion"
            elif age_hours >= max_holding_hours:
                exited = True
                exit_reason = "time_stop"
                
            if exited:
                symbols_to_close.append((sym, exit_reason))
                exit_counts[sym] += 1
                if exit_reason == "reversion":
                    exit_reversion_counts[sym] += 1
                else:
                    exit_time_stop_counts[sym] += 1
        
        # Close exited positions
        for sym, _ in symbols_to_close:
            del open_positions[sym]
        
        # REGIME GATING: Skip entries if current regime is trend_high_vol (exposure=0)
        current_regime = _get_regime_at(regime_timeline, current_ms)
        regime_gated = current_regime == "trend_high_vol"
        
        # Determine entry candidates
        # Rank by z-score ascending (most negative first = LONG candidates)
        ranked = sorted(zs.zscores.items(), key=lambda item: item[1])
        
        # Simple top_fraction approach - use top/bottom by z-score
        # For frequency check, we'll just consider all symbols that meet threshold
        long_candidates = [
            symbol for symbol, z in ranked
            if z <= -entry_threshold and symbol not in open_positions
        ]
        short_candidates = [
            symbol for symbol, z in ranked
            if z >= entry_threshold and symbol not in open_positions
        ]
        
        # Apply min_expected_edge_bps filter (matching MeanReversionStrategy._filter_by_min_edge)
        if min_expected_edge_bps > 0:
            long_candidates = [
                sym for sym in long_candidates
                if abs(zs.zscores.get(sym, 0.0)) - exit_threshold > 0
                and (abs(zs.zscores.get(sym, 0.0)) - exit_threshold) * zs.rolling_stds.get(sym, 0.0) * 10000.0 >= min_expected_edge_bps
            ]
            short_candidates = [
                sym for sym in short_candidates
                if abs(zs.zscores.get(sym, 0.0)) - exit_threshold > 0
                and (abs(zs.zscores.get(sym, 0.0)) - exit_threshold) * zs.rolling_stds.get(sym, 0.0) * 10000.0 >= min_expected_edge_bps
            ]
        
        # Fill available slots up to max_positions
        available_slots = max_positions - len(open_positions)
        
        # Prioritize strongest signals (most negative for long, most positive for short)
        new_entries = 0
        for sym in long_candidates:
            if new_entries >= available_slots:
                break
            # Regime gating: skip if in trend_high_vol
            if regime_gated:
                skipped_regime_counts[sym] += 1
                continue
            open_positions[sym] = (Side.LONG, current_ms)
            entry_counts[sym] += 1
            new_entries += 1
            
        for sym in short_candidates:
            if new_entries >= available_slots:
                break
            # Regime gating: skip if in trend_high_vol
            if regime_gated:
                skipped_regime_counts[sym] += 1
                continue
            open_positions[sym] = (Side.SHORT, current_ms)
            entry_counts[sym] += 1
            new_entries += 1
        
        total_bars += 1
        rebalance_count += 1
        current_ms += rebalance_ms

    total_entries = sum(entry_counts.values())
    total_exits = sum(exit_counts.values())
    total_reversion_exits = sum(exit_reversion_counts.values())
    total_time_stop_exits = sum(exit_time_stop_counts.values())
    total_skipped_regime = sum(skipped_regime_counts.values())
    days = (end_ms - start_ms) / 86_400_000

    return {
        "symbols": symbols,
        "period_days": days,
        "total_rebalances": rebalance_count,
        "entry_counts": entry_counts,
        "exit_counts": exit_counts,
        "exit_reversion_counts": exit_reversion_counts,
        "exit_time_stop_counts": exit_time_stop_counts,
        "skipped_regime_counts": skipped_regime_counts,
        "total_entries": total_entries,
        "total_exits": total_exits,
        "total_reversion_exits": total_reversion_exits,
        "total_time_stop_exits": total_time_stop_exits,
        "total_skipped_regime": total_skipped_regime,
        "entries_per_day": total_entries / days if days > 0 else 0,
        "exits_per_day": total_exits / days if days > 0 else 0,
        "estimated_n_trades_test": total_entries,  # Full count since we run on test segment directly
        "params": {
            "entry_threshold": entry_threshold,
            "exit_threshold": exit_threshold,
            "zscore_window_bars": zscore_window_bars,
            "signal_lookback_hours": signal_lookback_hours,
            "max_holding_bars": max_holding_bars,
            "rebalance_hours": rebalance_hours,
            "max_positions": max_positions,
            "min_expected_edge_bps": min_expected_edge_bps,
        }
    }


def main():
    parser = argparse.ArgumentParser(description="Signal frequency pre-check for Mean Reversion")
    parser.add_argument("--db", default="data/crypto_bot.db", help="Path to SQLite database")
    parser.add_argument("--symbols", nargs="+", required=True, help="Symbols to check (e.g., BTC/USDT ETH/USDT)")
    parser.add_argument("--timeframe", default="1h", help="Timeframe (15m, 1h, 4h)")
    parser.add_argument("--zscore-window", type=int, default=48, help="Z-score window in bars")
    parser.add_argument("--signal-lookback", default="8h", help="Signal lookback (e.g., 4h, 8h)")
    parser.add_argument("--entry-threshold", type=float, default=2.0, help="Entry threshold (|z| >= threshold)")
    parser.add_argument("--exit-threshold", type=float, default=0.5, help="Exit threshold (|z| <= threshold)")
    parser.add_argument("--max-holding-bars", type=int, default=48, help="Max holding period in BARS of --timeframe (time-stop)")
    parser.add_argument("--rebalance-hours", type=int, default=12, help="Rebalance cadence in hours")
    parser.add_argument("--max-positions", type=int, default=4, help="Max concurrent positions")
    parser.add_argument("--min-expected-edge-bps", type=int, default=10, help="Min expected edge in bps (filters entries)")
    parser.add_argument("--regime-csv", default="data/regime_timeline.csv", help="Path to regime timeline CSV")
    parser.add_argument(
        "--start", default=None,
        help="Pin the data window start (YYYY-MM-DD). Without it the window is taken "
             "from the DB, so the result drifts as new candles arrive.",
    )
    parser.add_argument(
        "--end", default=None,
        help="Pin the data window end (YYYY-MM-DD). See --start.",
    )
    parser.add_argument(
        "--split", type=float, default=0.5,
        help="Train fraction of the 3-way split. Must match the value passed to "
             "walk_forward.py, otherwise this pre-check measures a different test "
             "segment than the run it is supposed to predict (default 0.5).",
    )
    parser.add_argument(
        "--validation-split", type=float, default=0.2,
        help="Validation fraction of the 3-way split (default 0.2).",
    )
    args = parser.parse_args()

    if not 0 < args.split < 1:
        parser.error("--split must be between 0 and 1 (exclusive)")
    if not 0 < args.validation_split < 1:
        parser.error("--validation-split must be between 0 and 1 (exclusive)")
    if not args.split + args.validation_split < 1:
        parser.error("--split + --validation-split must be < 1")

    # Окно анализа. Без --start/--end оно берётся из БД и, значит, смещается при
    # каждом новом баре — результат перестаёт быть воспроизводимым.
    db_start_ms, db_end_ms = _get_data_bounds(args.db, args.symbols, args.timeframe)
    data_start_ms = _iso_to_ms(args.start) if args.start else db_start_ms
    data_end_ms = _iso_to_ms(args.end) if args.end else db_end_ms
    if data_start_ms >= data_end_ms:
        parser.error(f"--start ({args.start}) must precede --end ({args.end})")
    pinned = bool(args.start or args.end)

    train_start, train_end, val_end, test_end = _get_split_bounds(
        data_start_ms, data_end_ms, args.split, args.validation_split,
    )

    # Проверка выполняется только на TEST-сегменте.
    end_ms = test_end

    # Прогрев z-score считается в барах текущего таймфрейма, а не в часах:
    # при --timeframe 4h один бар это 4 часа, и хардкод 3600с занижал окно в 4 раза.
    bar_seconds = policy.timeframe_to_seconds(args.timeframe)
    bar_ms = bar_seconds * 1000
    warmup_ms = (args.zscore_window + 1) * bar_ms
    earliest_with_warmup = train_start + warmup_ms
    adjusted_start_ms = max(val_end, earliest_with_warmup)
    if adjusted_start_ms > val_end:
        print(
            f"Warning: not enough history before the test segment for a "
            f"{args.zscore_window}-bar warmup; test start moved forward"
        )
    
    tf_hours = bar_seconds / 3600.0
    print(f"Window: {'PINNED via --start/--end' if pinned else 'DERIVED from DB (drifts as data grows)'}")
    print(f"Split: train={args.split:.2f} validation={args.validation_split:.2f} "
          f"test={1 - args.split - args.validation_split:.2f} "
          f"(must match walk_forward.py --split / --validation-split)")
    print(f"Timeframe: {args.timeframe} = {tf_hours:g}h per bar")
    print(f"  zscore_window = {args.zscore_window} bars = {args.zscore_window * tf_hours:g}h")
    print(f"  max_holding   = {args.max_holding_bars} bars = {args.max_holding_bars * tf_hours:g}h")
    print(f"  rebalance     = {args.rebalance_hours}h")
    print(f"Data bounds: {datetime.fromtimestamp(data_start_ms/1000, tz=UTC)} to {datetime.fromtimestamp(data_end_ms/1000, tz=UTC)}")
    print(f"Train: {datetime.fromtimestamp(train_start/1000, tz=UTC)} to {datetime.fromtimestamp(train_end/1000, tz=UTC)}")
    print(f"Validation: {datetime.fromtimestamp(train_end/1000, tz=UTC)} to {datetime.fromtimestamp(val_end/1000, tz=UTC)}")
    print(f"Test: {datetime.fromtimestamp(val_end/1000, tz=UTC)} to {datetime.fromtimestamp(test_end/1000, tz=UTC)}")
    print(f"Test segment (after warmup): {datetime.fromtimestamp(adjusted_start_ms/1000, tz=UTC)} to {datetime.fromtimestamp(end_ms/1000, tz=UTC)}")
    print()

    result = run_signal_frequency_check(
        db_path=args.db,
        symbols=args.symbols,
        start_ms=adjusted_start_ms,
        end_ms=end_ms,
        timeframe=args.timeframe,
        zscore_window_bars=args.zscore_window,
        signal_lookback_hours=_parse_duration_hours(args.signal_lookback),
        entry_threshold=args.entry_threshold,
        exit_threshold=args.exit_threshold,
        max_holding_bars=args.max_holding_bars,
        rebalance_hours=args.rebalance_hours,
        max_positions=args.max_positions,
        min_expected_edge_bps=args.min_expected_edge_bps,
        regime_csv_path=args.regime_csv,
    )

    print("Results:")
    print(f"  Period: {result['period_days']:.1f} days")
    print(f"  Total rebalances: {result['total_rebalances']}")
    print("  Entry counts per symbol:")
    for sym, count in result["entry_counts"].items():
        print(f"    {sym}: {count}")
    print(f"  Total entries: {result['total_entries']}")
    print(f"  Total exits: {result['total_exits']}")
    print(f"    Reversion exits: {result['total_reversion_exits']} ({result['total_reversion_exits']/result['total_exits']*100:.1f}%)")
    print(f"    Time-stop exits: {result['total_time_stop_exits']} ({result['total_time_stop_exits']/result['total_exits']*100:.1f}%)")
    print(f"    Skipped (regime): {result['total_skipped_regime']} ({result['total_skipped_regime']/result['total_entries']*100:.1f}% of potential entries)")
    print(f"  Entries per day: {result['entries_per_day']:.2f}")
    print(f"  Exits per day: {result['exits_per_day']:.2f}")
    print(f"  Estimated n_trades in test: {result['estimated_n_trades_test']:.0f}")
    print()
    print("  Exit distribution per symbol:")
    for sym in result["symbols"]:
        rev = result["exit_reversion_counts"].get(sym, 0)
        ts = result["exit_time_stop_counts"].get(sym, 0)
        skipped = result["skipped_regime_counts"].get(sym, 0)
        total = rev + ts
        if total > 0:
            print(f"    {sym}: reversion={rev} ({rev/total*100:.1f}%), time_stop={ts} ({ts/total*100:.1f}%), skipped={skipped} ({skipped/(total+skipped)*100:.1f}%)")
    print()

    # Check if n_trades >= 200 is achievable
    if result["estimated_n_trades_test"] >= 200:
        print("[PASS] Estimated n_trades in test >= 200")
    else:
        print(f"[FAIL] Estimated n_trades in test < 200 ({result['estimated_n_trades_test']:.0f})")
        print("  Consider lowering entry_threshold, increasing max_positions, or reducing rebalance_hours")

    return 0 if result["estimated_n_trades_test"] >= 200 else 1


if __name__ == "__main__":
    exit(main())