#!/usr/bin/env python3
"""
scripts/walk_forward.py

Walk-forward analysis: run the Backtester on two consecutive calendar windows
(train / test) with the *same* config, then compare PnLSummary results.

Calendar-time split (not bar-count split) -- ensures multiple timeframes share
the same cut point.

Usage::

    # 70/30 split by calendar time, default config
    python scripts/walk_forward.py --symbol BTC/USDT --timeframe 1h

    # 80/20 with explicit config and custom DB
    python scripts/walk_forward.py --symbol ETH/USDT --timeframe 15m ^
        --config config/settings.yaml --db data/crypto_bot.db --split 0.8

    # Custom filter thresholds to test (e.g. after tuning from Front 2)
    # Note: volatility and volume are nested under strategy in the config.
    python scripts/walk_forward.py --symbol SOL/USDT --timeframe 1h ^
        --override strategy__volatility__atr_min_pct=0.2 ^
        --override strategy__volume__spike_ratio=1.2
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from crypto_bot.config.env import Config
from crypto_bot.config.settings import load_settings
from crypto_bot.core.enums import Mode
from crypto_bot.core.types import Candle
from crypto_bot.features.context import SymbolMarketContext
from crypto_bot.simulation.backtester import Backtester
from crypto_bot.simulation.historical_source import HistoricalCandleSource
from crypto_bot.simulation.market_constants import MARKET_QUOTE_VOLUME
from crypto_bot.simulation.pnl import PnLSummary
from crypto_bot.storage.db import CandleRepository, Database

# --------------------------------------------------------------------------- #
# Calendar-time split
# --------------------------------------------------------------------------- #

def calendar_split(
    candles: list[Candle],
    period_ms: int,
    split_ratio: float,
) -> tuple[int, int, int, int]:
    """Split candle date range into train/test by calendar time.

    Args:
        candles: Sorted candle list (ascending timestamp).
        period_ms: Bar period in ms.
        split_ratio: Fraction of total time range allocated to train (0 < r < 1).

    Returns:
        (train_start_ms, train_end_ms, test_start_ms, test_end_ms).
    """
    first_ts = candles[0].timestamp
    last_close = candles[-1].timestamp + period_ms
    total_range = last_close - first_ts
    split_ts = first_ts + int(total_range * split_ratio)
    return first_ts, split_ts, split_ts, last_close


# --------------------------------------------------------------------------- #
# Fetch candles from DB
# --------------------------------------------------------------------------- #

def fetch_candles(db_path: str, symbol: str, timeframe: str) -> list[Candle]:
    """Load all candles for *symbol*/*timeframe* from the main DB."""
    db = Database(db_path)
    try:
        repo = CandleRepository(db)
        import asyncio
        return asyncio.run(repo.fetch_since(symbol, timeframe, since_ts=0))
    finally:
        db.close()


def fetch_all_candles(
    db_path: str, symbols: list[str], timeframe: str,
) -> dict[str, list[Candle]]:
    """Load candles for every symbol in *symbols*.
    
    Returns {symbol: candles, ...} so the caller can feed each symbol's
    candles into ``HistoricalCandleSource.load_all()`` independently.
    """
    result: dict[str, list[Candle]] = {}
    for sym in symbols:
        print(f"  {sym} ...", end=" ", flush=True)
        candles = fetch_candles(db_path, sym, timeframe)
        print(f"{len(candles)} bars")
        result[sym] = candles
    return result


# --------------------------------------------------------------------------- #
# Run a single backtest window
# --------------------------------------------------------------------------- #

def run_window(
    config: Config,
    symbols: list[str],
    timeframe: str,
    start_ms: int,
    end_ms: int,
    source: HistoricalCandleSource,
    label: str,
    market_map: dict[str, SymbolMarketContext] | None = None,
) -> PnLSummary:
    """Run the Backtester on *[start_ms, end_ms]* and return its PnLSummary."""
    with tempfile.NamedTemporaryFile(suffix=f"_{label}.db", delete=False) as tmp:
        tmp_path = tmp.name

    bt = Backtester(
        config,
        symbols=symbols,
        timeframes=[timeframe],
        start_ms=start_ms,
        end_ms=end_ms,
        source=source,
        market_map=market_map,
        db=Database(tmp_path),
    )
    summary = bt.run()
    Path(tmp_path).unlink(missing_ok=True)
    return summary


# --------------------------------------------------------------------------- #
# Comparison helpers
# --------------------------------------------------------------------------- #

@dataclass
class WalkForwardResult:
    symbol: str
    timeframe: str
    split_ratio: float
    train: PnLSummary
    test: PnLSummary
    train_n_bars: int
    test_n_bars: int

    def _overfit_hint(self, min_trades: int = 10) -> str:
        """Heuristic: if train is noticeably better than test -> possible overfit.

        Returns early with ``NOT ENOUGH DATA`` when either window has fewer
        than *min_trades* closed trades — too few to draw any conclusion.
        """
        if self.train.total_trades < min_trades or self.test.total_trades < min_trades:
            return (
                f"NOT ENOUGH DATA: train={self.train.total_trades} trades, "
                f"test={self.test.total_trades} trades — minimum {min_trades}"
                f" trades needed for evaluation"
            )

        hints: list[str] = []
        # Sharpe ratio drop
        if self.train.sharpe_ratio > 0 and self.test.sharpe_ratio > 0:
            drop = (self.train.sharpe_ratio - self.test.sharpe_ratio) / self.train.sharpe_ratio
            if drop > 0.5:
                hints.append(f"Sharpe drops {drop:.0%} train->test")
        elif self.train.sharpe_ratio > 0 and self.test.sharpe_ratio <= 0:
            hints.append("Sharpe positive on train, non-positive on test")

        # Win rate drop
        wr_drop = (self.train.win_rate or 0) - (self.test.win_rate or 0)
        if wr_drop > 0.2:
            hints.append(f"Win rate drops {wr_drop:.1%} train->test")

        # P&L reversal: train positive, test negative
        if self.train.total_pnl_pct > 1.0 and self.test.total_pnl_pct < -1.0:
            hints.append("P&L reversed: train +, test -")

        # Drawdown much worse on test
        if self.test.max_drawdown_pct > self.train.max_drawdown_pct * 2 > 1:
            hints.append(f"Test max drawdown {self.test.max_drawdown_pct:.1f}% > 2? train {self.train.max_drawdown_pct:.1f}%")

        if not hints:
            return "looks consistent (no strong overfit signal)"
        return "[!]  POSSIBLE OVERFIT: " + "; ".join(hints)

    @staticmethod
    def _fmt_pct_or_na(value: float, n_trades: int) -> str:
        return f"{value:.1%}" if n_trades > 0 else "N/A"

    @staticmethod
    def _fmt_float_or_na(value: float, n_trades: int, fmt: str = "+7.2f") -> str:
        return f"{value:{fmt}}" if n_trades > 0 else "N/A"

    def print(self) -> None:
        print(f"\n{'='*65}")
        print(f"  WALK-FORWARD: {self.symbol} {self.timeframe}")
        print(f"  Split: {self.split_ratio:.0%} train / {1-self.split_ratio:.0%} test (calendar time)")
        print(f"{'='*65}")
        print(f"  {'':>12} {'TRAIN':>15} {'TEST':>15} {'Delta':>10}")
        print(f"  {'':-<12} {'':-<15} {'':-<15} {'':-<10}")
        print(f"  {'Bars':<12} {self.train_n_bars:>8}      {self.test_n_bars:>8}      --")
        print(f"  {'Trades':<12} {self.train.total_trades:>8}      {self.test.total_trades:>8}      {self.test.total_trades - self.train.total_trades:>+9}")
        print(f"  {'Win rate':<12} {self._fmt_pct_or_na(self.train.win_rate, self.train.total_trades):>8}    "
              f"{self._fmt_pct_or_na(self.test.win_rate, self.test.total_trades):>8}    "
              f"{self._fmt_float_or_na(self.test.win_rate - self.train.win_rate, max(self.train.total_trades, self.test.total_trades), '+9.1%')}")
        print(f"  {'Total P&L':<12} {self._fmt_float_or_na(self.train.total_pnl_pct, self.train.total_trades, '+7.2f')}%    "
              f"{self._fmt_float_or_na(self.test.total_pnl_pct, self.test.total_trades, '+7.2f')}%    "
              f"{self._fmt_float_or_na(self.test.total_pnl_pct - self.train.total_pnl_pct, max(self.train.total_trades, self.test.total_trades), '+9.2f')}%")
        print(f"  {'Max DD':<12} {self.train.max_drawdown_pct:>7.2f}%    {self.test.max_drawdown_pct:>7.2f}%    {self.test.max_drawdown_pct - self.train.max_drawdown_pct:>+9.2f}%")
        print(f"  {'Sharpe':<12} {self._fmt_float_or_na(self.train.sharpe_ratio, self.train.total_trades, '8.2f')}      "
              f"{self._fmt_float_or_na(self.test.sharpe_ratio, self.test.total_trades, '8.2f')}      "
              f"{self._fmt_float_or_na(self.test.sharpe_ratio - self.train.sharpe_ratio, max(self.train.total_trades, self.test.total_trades), '+9.2f')}")
        print(f"  {'Wins':<12} {self.train.winning_trades:>8}      {self.test.winning_trades:>8}      {self.test.winning_trades - self.train.winning_trades:>+9}")
        print(f"  {'Losses':<12} {self.train.losing_trades:>8}      {self.test.losing_trades:>8}      {self.test.losing_trades - self.train.losing_trades:>+9}")
        print(f"  {'Closed by SL':<12} {self.train.closed_by_sl:>8}      {self.test.closed_by_sl:>8}      {self.test.closed_by_sl - self.train.closed_by_sl:>+9}")
        print(f"  {'Closed by TP':<12} {self.train.closed_by_tp:>8}      {self.test.closed_by_tp:>8}      {self.test.closed_by_tp - self.train.closed_by_tp:>+9}")
        print(f"  {'P&L from SL':<12} {self._fmt_float_or_na(self.train.pnl_from_sl, self.train.total_trades, '+7.2f')}%    "
              f"{self._fmt_float_or_na(self.test.pnl_from_sl, self.test.total_trades, '+7.2f')}%    "
              f"{self._fmt_float_or_na(self.test.pnl_from_sl - self.train.pnl_from_sl, max(self.train.total_trades, self.test.total_trades), '+9.2f')}%")
        print(f"  {'P&L from TP':<12} {self._fmt_float_or_na(self.train.pnl_from_tp, self.train.total_trades, '+7.2f')}%    "
              f"{self._fmt_float_or_na(self.test.pnl_from_tp, self.test.total_trades, '+7.2f')}%    "
              f"{self._fmt_float_or_na(self.test.pnl_from_tp - self.train.pnl_from_tp, max(self.train.total_trades, self.test.total_trades), '+9.2f')}%")
        print(f"{'='*65}")
        print(f"  {self._overfit_hint()}")
        print(f"{'='*65}\n")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="+", default=None, metavar="SYM",
                        help="Symbol(s) for portfolio run (default: all 9)")
    parser.add_argument("--timeframe", default="1h", help="Timeframe (15m, 1h, 4h)")
    parser.add_argument("--config", default="config/settings.yaml", help="Path to settings YAML")
    parser.add_argument("--db", default="data/crypto_bot.db", help="Path to main candle DB")
    parser.add_argument("--split", type=float, default=0.7, help="Train fraction (0 < r < 1, default 0.7)")
    parser.add_argument(
        "--override", action="append", default=[],
        help="Settings override, e.g. --override scoring__min_score=50. Can be repeated.",
    )
    args = parser.parse_args()

    if not 0 < args.split < 1:
        parser.error("--split must be between 0 and 1 (exclusive)")

    # Default universe: symbols from market_constants
    symbols = args.symbols or list(MARKET_QUOTE_VOLUME.keys())

    # --- Load config ---
    config_obj = load_settings(yaml_path=Path(args.config))
    settings = config_obj.settings

    # Apply overrides
    for ov in args.override:
        key, val = ov.split("=", 1)
        parts = key.split("__")
        d = settings.model_dump()
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

    # --- Fetch candles for all symbols ---
    print(f"Loading candles for {len(symbols)} symbols ({args.timeframe}) from {args.db} ...")
    all_candles = fetch_all_candles(args.db, symbols, args.timeframe)
    # Use first symbol for calendar split, assume others cover the same range
    first_sym = symbols[0]
    first_candles = all_candles.get(first_sym, [])
    if not first_candles:
        print(f"FAIL: no candles found for {first_sym}")
        sys.exit(1)

    from crypto_bot.core.policy import timeframe_to_seconds
    period_ms = timeframe_to_seconds(args.timeframe) * 1000
    train_start, train_end, test_start, test_end = calendar_split(first_candles, period_ms, args.split)

    def tf(ts):
        return datetime.fromtimestamp(ts / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M")
    print(f"  Train window:  {tf(train_start)}  ->  {tf(train_end)}")
    print(f"  Test window:   {tf(test_start)}  ->  {tf(test_end)}")

    # --- Build candle source for all symbols ---
    source = HistoricalCandleSource()
    for sym, candles in all_candles.items():
        source.load_all(sym, args.timeframe, candles)

    # Count total train/test bars across all symbols
    total_train_bars = sum(
        sum(1 for c in candles if c.timestamp + period_ms <= train_end)
        for candles in all_candles.values()
    )
    total_test_bars = sum(
        sum(1 for c in candles if c.timestamp + period_ms >= test_start)
        for candles in all_candles.values()
    )

    # --- Build market_map for all symbols ---
    market_map: dict[str, SymbolMarketContext] = {}
    for sym in symbols:
        qv = MARKET_QUOTE_VOLUME.get(sym, 1_000_000_000)
        market_map[sym] = SymbolMarketContext(quote_volume_24h=qv)

    # --- Run train ---
    print(f"\nRunning TRAIN ({total_train_bars} total bars across {len(symbols)} symbols)...")
    train_summary = run_window(config, symbols, args.timeframe, train_start, train_end, source, "train", market_map)

    # --- Run test ---
    print(f"Running TEST ({total_test_bars} total bars)...")
    test_summary = run_window(config, symbols, args.timeframe, test_start, test_end, source, "test", market_map)

    # --- Print comparison ---
    result = WalkForwardResult(
        symbol="+".join(symbols),
        timeframe=args.timeframe,
        split_ratio=args.split,
        train=train_summary,
        test=test_summary,
        train_n_bars=total_train_bars,
        test_n_bars=total_test_bars,
    )
    result.print()


if __name__ == "__main__":
    main()
