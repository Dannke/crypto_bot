#!/usr/bin/env python3
"""Signal frequency pre-check for Mean Reversion strategy.

This script scans historical data to estimate how often the mean reversion
signal (|z-score| >= entry_threshold) would trigger, to verify that
n_trades >= 200 is achievable in the test window.

Usage:
    python scripts/signal_frequency_check.py --db data/crypto_bot.db --symbols BTC/USDT ETH/USDT ... --start 2025-01-01 --end 2025-12-31 --entry-threshold 3.0 --zscore-window 48 --signal-lookback 4h
"""
from __future__ import annotations

import argparse
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from crypto_bot.config.env import Config, EnvConfig
from crypto_bot.config.settings import load_settings
from crypto_bot.config.schemas import Settings
from crypto_bot.core.enums import Mode
from src.crypto_bot.simulation.historical_source import HistoricalCandleSource
from src.crypto_bot.portfolio import get_market_snapshot, get_universe_snapshot
from src.crypto_bot.portfolio.mean_reversion_features import compute_zscore_snapshot


def _iso_to_ms(value: str) -> int:
    return int(datetime.fromisoformat(value).replace(tzinfo=UTC).timestamp() * 1000)


def _parse_duration_hours(value: str) -> int:
    """Parse duration like '4h', '8h', '1d' to hours."""
    value = value.strip().lower()
    if value.endswith('h'):
        return int(value[:-1])
    if value.endswith('d'):
        return int(value[:-1]) * 24
    if value.endswith('w'):
        return int(value[:-1]) * 168
    raise ValueError(f"Unknown duration format: {value}")


def run_signal_frequency_check(
    db_path: str,
    symbols: list[str],
    start_ms: int,
    end_ms: int,
    timeframe: str = "1h",
    zscore_window_bars: int = 48,
    signal_lookback_hours: int = 4,
    entry_threshold: float = 3.0,
) -> dict:
    """
    Scan historical data and count how often |z| >= entry_threshold.
    
    Returns:
        dict with signal counts, frequencies, and estimated n_trades
    """
    from crypto_bot.config.schemas import Settings
    from crypto_bot.config.env import Config, EnvConfig
    from crypto_bot.core.enums import Mode
    
    config = load_settings(yaml_path=Path("config/settings.yaml"))
    config.settings.runtime.mode = Mode.PAPER
    config = Config(settings=config.settings, env=EnvConfig())
    
# Load candles
    from crypto_bot.core.types import Candle
    print(f"DEBUG: Candle class = {Candle}, id = {id(Candle)}")
    source = HistoricalCandleSource()
    for sym in symbols:
        rows = []
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                """SELECT ts_ms, open, high, low, close, volume 
                   FROM candles WHERE symbol=? AND timeframe=? AND ts_ms BETWEEN ? AND ?
                   ORDER BY ts_ms""",
                (sym, timeframe, start_ms, end_ms)
            ).fetchall()
        candles = [
            Candle(
                timestamp=r[0], open=r[1], high=r[2], low=r[3], close=r[4], volume=r[5]
            )
            for r in rows
        ]
        print(f"DEBUG: Created {len(candles)} candles, first = {candles[0]}, type = {type(candles[0])}, isinstance = {isinstance(candles[0], Candle)}")
source.load_all(sym, timeframe, candles)
        print(f"DEBUG: After load_all, cache = {('BTC/USDT', '1h') in source._cache}")
        if ('BTC/USDT', '1h') in source._cache:
            candles_cached, close_times, period_ms = source._cache[('BTC/USDT', '1h')]
            print(f"DEBUG: Cached first candle = {candles_cached[0]}, type = {type(candles_cached[0])}, isinstance = {isinstance(candles_cached[0], Candle)}")

    universe = get_universe_snapshot(source, symbols, timeframe, end_ms)
    snapshot = get_market_snapshot(source, universe, timeframe)
    
    # Count signals over time
    signal_counts = {sym: 0 for sym in symbols}
    total_bars = 0
    
    current_ms = start_ms
    period_ms = 3600_000  # 1h
    signal_lookback_ms = signal_lookback_hours * 3600_000
    zscore_window_bars = zscore_window_bars
    
    while current_ms <= end_ms:
        # Create snapshot at this timestamp
        universe = get_universe_snapshot(source, symbols, timeframe, current_ms)
        snapshot = get_market_snapshot(source, universe, timeframe)
        
        # Compute z-scores
        zs = compute_zscore_snapshot(
            snapshot,
            window_bars=zscore_window_bars,
            signal_lookback_bars=signal_lookback_hours,
        )
        
        # Count signals
        for sym, z in zs.zscores.items():
            if abs(z) >= entry_threshold:
                signal_counts[sym] += 1
        
        total_bars += 1
        current_ms += period_ms
    
    total_signals = sum(signal_counts.values())
    days = (end_ms - start_ms) / 86_400_000
    
    return {
        "symbols": symbols,
        "period_days": days,
        "total_bars": total_bars,
        "signal_counts": signal_counts,
        "total_signals": total_signals,
        "signals_per_day": total_signals / days if days > 0 else 0,
        "estimated_n_trades_test": total_signals * 0.3,  # Rough estimate for test split
        "params": {
            "entry_threshold": entry_threshold,
            "zscore_window_bars": zscore_window_bars,
            "signal_lookback_hours": signal_lookback_hours,
            "max_positions": 2,
        }
    }


def main():
    parser = argparse.ArgumentParser(description="Signal frequency pre-check for Mean Reversion")
    parser.add_argument("--db", default="data/crypto_bot.db", help="Path to SQLite database")
    parser.add_argument("--symbols", nargs="+", required=True, help="Symbols to check (e.g., BTC/USDT ETH/USDT)")
    parser.add_argument("--start", required=True, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", required=True, help="End date (YYYY-MM-DD)")
    parser.add_argument("--timeframe", default="1h", help="Timeframe (default: 1h)")
    parser.add_argument("--zscore-window", type=int, default=48, help="Z-score window in bars")
    parser.add_argument("--signal-lookback", default="4h", help="Signal lookback (e.g., 4h, 8h)")
    parser.add_argument("--entry-threshold", type=float, default=3.0, help="Entry threshold (|z| >= threshold)")
    
    args = parser.parse_args()
    
    start_ms = _iso_to_ms(args.start)
    end_ms = _iso_to_ms(args.end)
    signal_lookback_hours = _parse_duration_hours(args.signal_lookback)
    
    print(f"Signal Frequency Pre-Check")
    print(f"=" * 60)
    print(f"Database: {args.db}")
    print(f"Symbols: {', '.join(args.symbols)}")
    print(f"Period: {args.start} to {args.end}")
    print(f"Timeframe: {args.timeframe}")
    print(f"Z-score window: {args.zscore_window} bars")
    print(f"Signal lookback: {args.signal_lookback}")
    print(f"Entry threshold: |z| >= {args.entry_threshold}")
    print(f"Max positions: 2")
    print()
    
    result = run_signal_frequency_check(
        db_path=args.db,
        symbols=args.symbols,
        start_ms=start_ms,
        end_ms=end_ms,
        timeframe=args.timeframe,
        zscore_window_bars=args.zscore_window,
        signal_lookback_hours=signal_lookback_hours,
        entry_threshold=args.entry_threshold,
    )
    
    print(f"Results:")
    print(f"  Period: {result['period_days']:.1f} days")
    print(f"  Total bars scanned: {result['total_bars']}")
    print(f"  Signal counts per symbol:")
    for sym, count in result['signal_counts'].items():
        print(f"    {sym}: {count}")
    print(f"  Total signals: {result['total_signals']}")
    print(f"  Signals per day: {result['signals_per_day']:.1f}")
    print(f"  Estimated n_trades in test (30%): ~{result['estimated_n_trades_test']:.0f}")
    print()
    
    # Check if n_trades >= 200 is achievable
    if result['estimated_n_trades_test'] >= 200:
        print("✓ PASS: Estimated n_trades in test >= 200")
    else:
        print(f"✗ FAIL: Estimated n_trades in test < 200 ({result['estimated_n_trades_test']:.0f})")
        print("  Consider lowering entry_threshold or increasing max_positions")
    
    return 0 if result['estimated_n_trades_test'] >= 200 else 1


if __name__ == "__main__":
    exit(main())