#!/usr/bin/env python3
"""
scripts/walk_forward.py

Walk-forward analysis for BOTH decision layers: candidate (SingleTF, the
current strategy) and portfolio (``settings.portfolio.strategy_name``, e.g.
``cross_sectional_momentum_v0``).  Runs the Backtester on calendar windows
with the *same* config, candle source, execution (fees/slippage) and risk limits.

Supports 2-way (train/test) and 3-way (train/validation/test) splits.

R0.5: Supports running with realistic Bybit perpetual costs:
- Funding accrual (R0.2): funding rates applied on each bar close
- Margin/leverage limits (R0.3): max_leverage + maintenance_margin_buffer
- Instrument specs (R0.4): qtyStep rounding + minNotionalValue checks

Usage::

    # Candidate layer (default), 70/30 split by calendar time
    python scripts/walk_forward.py --symbol BTC/USDT --timeframe 1h

    # Portfolio layer (CSM), 80/20 with explicit config and custom DB
    python scripts/walk_forward.py --symbol ETH/USDT --timeframe 1h ^
        --config config/settings.yaml --db data/crypto_bot.db --split 0.8 ^
        --mode portfolio

    # 3-way split (50/20/30) for rigorous evaluation
    python scripts/walk_forward.py --symbols BTC/USDT ETH/USDT --timeframe 1h ^
        --mode portfolio --three-way --validation-split 0.2 --split 0.5

    # Keep train/validation/test DBs for inspection
    python scripts/walk_forward.py --symbols SOL/USDT --timeframe 1h ^
        --mode portfolio --three-way --db-dir data/backtests/wf
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from crypto_bot.config.env import Config
from crypto_bot.config.settings import load_settings
from crypto_bot.core.enums import Mode, StrategyType
from crypto_bot.simulation.historical_source import HistoricalCandleSource
from crypto_bot.simulation.market_constants import MARKET_QUOTE_VOLUME
from crypto_bot.simulation.walk_forward import fetch_all_candles, run_walk_forward


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--symbols", nargs="+", default=None, metavar="SYM",
        help="Symbol(s) for portfolio run (default: all 9)",
    )
    parser.add_argument("--timeframe", default="1h", help="Timeframe (15m, 1h, 4h)")
    parser.add_argument("--config", default="config/settings.yaml", help="Path to settings YAML")
    parser.add_argument("--db", default="data/crypto_bot.db", help="Path to main candle DB")
    parser.add_argument("--split", type=float, default=0.7, help="Train fraction (0 < r < 1, default 0.7)")
    parser.add_argument(
        "--mode", choices=["candidate", "portfolio"], default="candidate",
        help="Decision layer: candidate (SingleTF) or portfolio "
             "(settings.portfolio.strategy_name, e.g. cross_sectional_momentum_v0)",
    )
    parser.add_argument(
        "--three-way", action="store_true", default=False,
        help="Use 3-way split (train/validation/test) instead of 2-way (train/test)"
    )
    parser.add_argument(
        "--validation-split", type=float, default=0.2,
        help="Validation fraction for 3-way split (default 0.2). train + validation < 1"
    )
    parser.add_argument(
        "--db-dir", default=None,
        help="Keep train/validation/test backtest DBs under this directory for inspection",
    )
    parser.add_argument(
        "--override", action="append", default=[],
        help="Settings override, e.g. --override scoring__min_score=50. Can be repeated.",
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

    if not 0 < args.split < 1:
        parser.error("--split must be between 0 and 1 (exclusive)")
    if args.three_way:
        if not 0 < args.validation_split < 1:
            parser.error("--validation-split must be between 0 and 1 (exclusive)")
        if not args.split + args.validation_split < 1:
            parser.error("--split + --validation-split must be < 1")

    # Default universe: symbols from market_constants
    symbols = args.symbols or list(MARKET_QUOTE_VOLUME.keys())

    # --- Load config ---
    config_obj = load_settings(yaml_path=Path(args.config))
    settings = config_obj.settings

    # Apply overrides
    d = settings.model_dump()
    for ov in args.override:
        key, val = ov.split("=", 1)
        parts = key.split("__")
        target = d
        for p in parts[:-1]:
            target = target[p]
            if isinstance(target, str):
                raise TypeError(f"Override {ov}: intermediate key {p} resolves to a string, not a dict")
        # Try numeric type
        try:
            typed_val = int(val) if "." not in val else float(val)
        except ValueError:
            typed_val = val
        target[parts[-1]] = typed_val
    settings = settings.__class__.model_validate(d)

    # Ensure PAPER mode for backtesting
    settings.runtime.mode = Mode.PAPER
    config = Config(settings=settings, env=config_obj.env)

    strategy_mode = StrategyType(args.mode)

    # --- Fetch candles for all symbols ---
    print(f"Loading candles for {len(symbols)} symbols ({args.timeframe}) from {args.db} ...")
    all_candles = fetch_all_candles(args.db, symbols, args.timeframe)
    if not all_candles.get(symbols[0], []):
        print(f"FAIL: no candles found for {symbols[0]}")
        sys.exit(1)

    # --- Build candle source for all symbols ---
    source = HistoricalCandleSource()
    for sym, candles in all_candles.items():
        source.load_all(sym, args.timeframe, candles)

    # --- Build market_map for all symbols ---
    from crypto_bot.features.context import SymbolMarketContext
    market_map: dict[str, SymbolMarketContext] = {}
    for sym in symbols:
        qv = MARKET_QUOTE_VOLUME.get(sym, 1_000_000_000)
        market_map[sym] = SymbolMarketContext(quote_volume_24h=qv)

    # R0.5: Funding options
    enable_funding = args.enable_funding and not args.disable_funding

    result = run_walk_forward(
        config,
        symbols=symbols,
        timeframe=args.timeframe,
        source=source,
        split_ratio=args.split,
        market_map=market_map,
        strategy_mode=strategy_mode,
        db_dir=args.db_dir,
        enable_funding=enable_funding,
        max_leverage=args.max_leverage,
        maintenance_margin_buffer_pct=args.maintenance_margin_buffer,
        three_way=args.three_way,
        validation_ratio=args.validation_split,
    )

    def tf(ts: int) -> str:
        return datetime.fromtimestamp(ts / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M")

    print(f"  Mode: {result.strategy_mode.value}  Strategy: {result.strategy_name}")
    print(f"  Train window: {tf(result.train_start_ms)} -> {tf(result.train_end_ms)} ({result.train_n_bars} bars)")
    if result.split_type == "3-way":
        print(f"  Validation window: {tf(result.validation_start_ms)} -> {tf(result.validation_end_ms)} ({result.validation_n_bars} bars)")
    print(f"  Test window:  {tf(result.test_start_ms)} -> {tf(result.test_end_ms)} ({result.test_n_bars} bars)")
    result.print()


if __name__ == "__main__":
    main()
