#!/usr/bin/env python3
"""
scripts/compare_baselines.py

Task 13: automated baseline comparison.

Runs the four baselines — SingleTF (current), CrossSectionalMomentum,
Random, ReverseMomentum — on the SAME universe, period, starting capital,
execution model (fees + slippage) and risk constraints, and prints the
comparison table.  Only a strategy that beats its baselines on these
identical conditions counts as an improvement.

Usage::

    # Default universe (9 symbols from market_constants), last 30 days, 1h
    python scripts/compare_baselines.py

    # Explicit universe, window, seed (random baseline reproducibility)
    python scripts/compare_baselines.py ^
        --symbols BTC/USDT ETH/USDT SOL/USDT ^
        --start 2026-07-01 --end 2026-08-01 --seed 42 --max-positions 5

    # Keep run databases + JSON report under data/backtests/baselines/
    python scripts/compare_baselines.py --out-dir data/backtests/baselines
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from crypto_bot.config.env import Config
from crypto_bot.config.settings import load_settings
from crypto_bot.core.enums import Mode
from crypto_bot.simulation.baselines import (
    INITIAL_EQUITY,
    format_baseline_report,
    run_baseline_comparison,
)
from crypto_bot.simulation.historical_source import HistoricalCandleSource
from crypto_bot.simulation.market_constants import MARKET_QUOTE_VOLUME
from crypto_bot.storage.db import CandleRepository, Database


def _fetch_candles(db_path: str, symbol: str, timeframe: str) -> list:
    db = Database(db_path)
    try:
        return CandleRepository(db).fetch_since(symbol, timeframe, since_ms=0)
    finally:
        db.close()


def _iso_to_ms(value: str) -> int:
    return int(datetime.fromisoformat(value).replace(tzinfo=UTC).timestamp() * 1000)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--symbols", nargs="+", default=None, metavar="SYM",
        help="Symbols to compare on (default: all symbols from market_constants)",
    )
    parser.add_argument("--timeframe", default="1h", help="Timeframe (15m, 1h, 4h)")
    parser.add_argument(
        "--start", default=None, metavar="ISO",
        help="Start of the comparison window (ISO, default: 30 days ago)",
    )
    parser.add_argument(
        "--end", default=None, metavar="ISO",
        help="End of the comparison window (ISO, default: now)",
    )
    parser.add_argument("--seed", type=int, default=42, help="Seed for the random baseline")
    parser.add_argument("--max-positions", type=int, default=5, help="Max open positions")
    parser.add_argument("--config", default="config/settings.yaml", help="Path to settings YAML")
    parser.add_argument("--db", default="data/crypto_bot.db", help="Path to main candle DB")
    parser.add_argument(
        "--out-dir", default=None,
        help="Where to write the per-strategy DBs and the JSON report "
             "(default: temp dir; DBs are deleted only if not given)",
    )
    # R0.5: Realistic Bybit perpetual options
    parser.add_argument(
        "--enable-funding", action="store_true", default=True,
        help="Enable funding accrual (fee + slippage + funding)"
    )
    parser.add_argument(
        "--disable-funding", action="store_true", default=False,
        help="Disable funding (legacy fee + slippage only)"
    )
    parser.add_argument(
        "--max-leverage", type=float, default=10.0,
        help="Max leverage for portfolio layer (margin check)"
    )
    parser.add_argument(
        "--maintenance-margin-buffer", type=float, default=0.1,
        help="Maintenance margin buffer (fraction, e.g., 0.1 = 10%%)"
    )
    args = parser.parse_args()

    symbols = args.symbols or list(MARKET_QUOTE_VOLUME.keys())
    end_ms = _iso_to_ms(args.end) if args.end else int(datetime.now(tz=UTC).timestamp() * 1000)
    start_ms = (
        _iso_to_ms(args.start) if args.start else end_ms - 30 * 24 * 3_600_000
    )

    config = load_settings(yaml_path=Path(args.config))
    settings = config.settings
    settings.runtime.mode = Mode.PAPER
    config = Config(settings=settings, env=config.env)

    print(
        f"Loading candles for {len(symbols)} symbols ({args.timeframe}) "
        f"from {args.db} ..."
    )
    source = HistoricalCandleSource()
    for sym in symbols:
        candles = _fetch_candles(args.db, sym, args.timeframe)
        source.load_all(sym, args.timeframe, candles)
        print(f"  {sym}: {len(candles)} bars")

    def tf(ts: int) -> str:
        return datetime.fromtimestamp(ts / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M")

    enable_funding = args.enable_funding and not args.disable_funding
    print(f"\nComparison window: {tf(start_ms)} -> {tf(end_ms)}")
    print(f"seed={args.seed} max_positions={args.max_positions} "
          f"capital={INITIAL_EQUITY:,.0f} USDT")
    print(f"funding={'enabled' if enable_funding else 'disabled'} "
          f"max_leverage={args.max_leverage} "
          f"maintenance_margin_buffer={args.maintenance_margin_buffer:.1%}")

    report = run_baseline_comparison(
        config,
        symbols=symbols,
        timeframes=[args.timeframe],
        start_ms=start_ms,
        end_ms=end_ms,
        source=source,
        seed=args.seed,
        max_positions=args.max_positions,
        out_dir=args.out_dir,
        enable_funding=enable_funding,
        max_leverage=args.max_leverage,
        maintenance_margin_buffer_pct=args.maintenance_margin_buffer,
    )

    print()
    print(format_baseline_report(report))

    if args.out_dir:
        out = Path(args.out_dir)
        out.mkdir(parents=True, exist_ok=True)
        payload = {
            "seed": report.seed,
            "max_positions": report.max_positions,
            "symbols": list(report.symbols),
            "timeframes": list(report.timeframes),
            "start_ms": report.start_ms,
            "end_ms": report.end_ms,
            "starting_capital": INITIAL_EQUITY,
            "results": [
                {
                    "name": r.name,
                    "strategy_name": r.strategy_name,
                    "strategy_mode": r.strategy_mode.value,
                    "total_pnl_pct": r.summary.total_pnl_pct,
                    "total_pnl_abs": r.summary.total_pnl_abs,
                    "sharpe_ratio": r.summary.sharpe_ratio,
                    "max_drawdown_pct": r.summary.max_drawdown_pct,
                    "win_rate": r.summary.win_rate,
                    "total_trades": r.summary.total_trades,
                }
                for r in report.results
            ],
        }
        (out / "baseline_report.json").write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )
        print(f"\nReport written to {out / 'baseline_report.json'}")


if __name__ == "__main__":
    main()