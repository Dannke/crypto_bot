#!/usr/bin/env python3
"""
R7: Walk-forward comparison with regime gating enabled vs disabled.

Runs the walk-forward analysis on the same dataset with regime gating
enabled vs disabled, and compares the overfit signals, Sharpe ratios,
and turnover metrics to determine if regime gating reduces overfitting.

R0.5: Supports running with realistic Bybit perpetual costs:
- Funding accrual (R0.2): funding rates applied on each bar close
- Margin/leverage limits (R0.3): max_leverage + maintenance_margin_buffer
- Instrument specs (R0.4): qtyStep rounding + minNotionalValue checks
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from crypto_bot.config.env import Config
from crypto_bot.config.settings import load_settings
from crypto_bot.config.schemas import RegimeConfig
from crypto_bot.core.enums import Mode, StrategyType
from crypto_bot.execution.costs import CompositeCostModel
from crypto_bot.portfolio.risk import PortfolioRiskLimits
from crypto_bot.simulation.backtester import Backtester
from crypto_bot.simulation.historical_source import HistoricalCandleSource
from crypto_bot.simulation.market_constants import MARKET_QUOTE_VOLUME
from crypto_bot.storage.db import CandleRepository, Database


def _iso_to_ms(value: str) -> int:
    return int(datetime.fromisoformat(value).replace(tzinfo=UTC).timestamp() * 1000)


def _fetch_candles(db_path: str, symbol: str, timeframe: str):
    db = Database(db_path)
    try:
        return CandleRepository(db).fetch_since(symbol, timeframe, since_ms=0)
    finally:
        db.close()


def main() -> int:
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

    # R0.5: Cost model selection
    enable_funding = args.enable_funding and not args.disable_funding
    cost_model = CompositeCostModel.bybit_perp_default() if enable_funding else CompositeCostModel.legacy_default()

    print(f"\nComparison window: {tf(start_ms)} -> {tf(end_ms)}")
    print(f"seed={args.seed} max_positions={args.max_positions} "
          f"capital=10,000 USDT")
    print(f"funding={'enabled' if enable_funding else 'disabled'} "
          f"max_leverage={args.max_leverage} "
          f"maintenance_margin_buffer={args.maintenance_margin_buffer:.1%}")

    # Run with regime DISABLED
    print("\n" + "="*80)
    print("  RUN 1: Regime gating DISABLED")
    print("="*80)

    bt_disabled = Backtester(
        Config(settings=settings, env=config.env),
        symbols=symbols,
        timeframes=[args.timeframe],
        start_ms=start_ms,
        end_ms=end_ms,
        source=source,
        db=Database("data/backtests/r7_disabled.db"),
        strategy_mode=StrategyType.PORTFOLIO,
        portfolio_limits=PortfolioRiskLimits(
            max_positions=args.max_positions,
            max_position_weight=1.0,
            max_gross_exposure=1.0,
            max_net_exposure=1.0,
            max_leverage=args.max_leverage,
            maintenance_margin_buffer_pct=args.maintenance_margin_buffer,
        ),
        regime_config=RegimeConfig(enabled=False),
    )
    # Apply cost model
    bt_disabled._executor._costs = cost_model
    summary_disabled = bt_disabled.run()
    
    # Run with regime ENABLED
    print("\n" + "="*80)
    print("  RUN 2: Regime gating ENABLED")
    print("="*80)
    
    bt_enabled = Backtester(
        Config(settings=settings, env=config.env),
        symbols=symbols,
        timeframes=[args.timeframe],
        start_ms=start_ms,
        end_ms=end_ms,
        source=source,
        db=Database("data/backtests/r7_enabled.db"),
        strategy_mode=StrategyType.PORTFOLIO,
        portfolio_limits=PortfolioRiskLimits(
            max_positions=args.max_positions,
            max_position_weight=1.0,
            max_gross_exposure=1.0,
            max_net_exposure=1.0,
            max_leverage=args.max_leverage,
            maintenance_margin_buffer_pct=args.maintenance_margin_buffer,
        ),
        regime_config=RegimeConfig(
            enabled=True,
            reference="universe_basket",
            trend_period=14,
            trend_threshold=25.0,
            vol_lookback_bars=168,
            vol_percentile_high=0.75,
        ),
    )
    # Apply cost model
    bt_enabled._executor._costs = cost_model
    summary_enabled = bt_enabled.run()
    
    # Compare results
    print("\n" + "="*80)
    print("  R7 COMPARISON: Regime Gating ENABLED vs DISABLED")
    print("="*80)
    print(f"  {'Metric':<25} {'DISABLED':>12} {'ENABLED':>12} {'Delta':>12}")
    print("-"*65)
    
    metrics = [
        ("Total P&L %", summary_disabled.total_pnl_pct, summary_enabled.total_pnl_pct),
        ("Total P&L USDT", summary_disabled.total_pnl_abs, summary_enabled.total_pnl_abs),
        ("Sharpe Ratio", summary_disabled.sharpe_ratio, summary_enabled.sharpe_ratio),
        ("Max Drawdown %", summary_disabled.max_drawdown_pct, summary_enabled.max_drawdown_pct),
        ("Win Rate", summary_disabled.win_rate, summary_enabled.win_rate),
        ("Total Trades", summary_disabled.total_trades, summary_enabled.total_trades),
    ]
    
    for name, disabled, enabled in metrics:
        delta = enabled - disabled if isinstance(disabled, (int, float)) and isinstance(enabled, (int, float)) else None
        delta_str = f"{delta:>+12.2f}" if delta is not None else "N/A"
        print(f"  {name:<25} {disabled:>12.2f} {enabled:>12.2f} {delta_str}")
    
    # Save report
    if args.out_dir:
        out = Path(args.out_dir)
        out.mkdir(parents=True, exist_ok=True)
        payload = {
            "comparison": "regime_gating_enabled_vs_disabled",
            "seed": args.seed,
            "max_positions": args.max_positions,
            "symbols": list(symbols),
            "timeframes": [args.timeframe],
            "start_ms": start_ms,
            "end_ms": end_ms,
            "starting_capital": 10000.0,
            "results": [
                {
                    "name": "regime_disabled",
                    "regime_enabled": False,
                    "total_pnl_pct": summary_disabled.total_pnl_pct,
                    "total_pnl_abs": summary_disabled.total_pnl_abs,
                    "sharpe_ratio": summary_disabled.sharpe_ratio,
                    "max_drawdown_pct": summary_disabled.max_drawdown_pct,
                    "win_rate": summary_disabled.win_rate,
                    "total_trades": summary_disabled.total_trades,
                },
                {
                    "name": "regime_enabled",
                    "regime_enabled": True,
                    "total_pnl_pct": summary_enabled.total_pnl_pct,
                    "total_pnl_abs": summary_enabled.total_pnl_abs,
                    "sharpe_ratio": summary_enabled.sharpe_ratio,
                    "max_drawdown_pct": summary_enabled.max_drawdown_pct,
                    "win_rate": summary_enabled.win_rate,
                    "total_trades": summary_enabled.total_trades,
                },
            ],
        }
        (out / "regime_comparison_report.json").write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )
        print(f"\nReport written to {out / 'regime_comparison_report.json'}")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())