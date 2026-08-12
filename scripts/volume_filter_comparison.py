"""Сравнение VolumeFilter: baseline (включён) vs отключён.

Прогоняет один и тот же мульти-символьный мульти-TF бэктест
на исправленной (Часть A) инфраструктуре с unified clock в двух
вариантах и печатает сводку для принятия решения.
"""
from __future__ import annotations

import logging
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Suppress verbose backtest logs - only show final summary
logging.getLogger("crypto_bot.simulation.backtester").setLevel(logging.WARNING)
logging.getLogger("crypto_bot.simulation.executor").setLevel(logging.WARNING)

from crypto_bot.config.env import Config, EnvConfig
from crypto_bot.config.schemas import Settings, FilterParams, RuntimeConfig
from crypto_bot.config.settings import load_settings
from crypto_bot.core.enums import Mode
from crypto_bot.core.policy import timeframe_to_seconds
from crypto_bot.core.types import Candle
from crypto_bot.features.context import SymbolMarketContext
from crypto_bot.simulation.backtester import Backtester
from crypto_bot.simulation.historical_source import HistoricalCandleSource
from crypto_bot.simulation.market_constants import MARKET_QUOTE_VOLUME
from crypto_bot.storage.db import Database

LIVE_DB_PATH = Path("data/crypto_bot.db")
CONFIG_PATH = "config/settings.yaml"
ALL_SYMBOLS = list(MARKET_QUOTE_VOLUME.keys())
ALL_TFS = ["15m", "1h", "4h"]
TMP_DIR = Path("/tmp/bt_volume_compare")
LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)
RESULT_LOG = LOG_DIR / "volume_filter_comparison_results.txt"

# ─── helpers ────────────────────────────────────────────────────────────────


def load_all_candles(db_path: Path, symbol: str, timeframe: str) -> list[Candle]:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT ts_ms, open, high, low, close, volume FROM candles "
            "WHERE symbol=? AND timeframe=? ORDER BY ts_ms ASC",
            (symbol, timeframe),
        ).fetchall()
    finally:
        con.close()
    return [
        Candle(
            timestamp=int(r["ts_ms"]),
            open=float(r["open"]),
            high=float(r["high"]),
            low=float(r["low"]),
            close=float(r["close"]),
            volume=float(r["volume"]),
        )
        for r in rows
    ]


def compute_bounds(
    db_path: Path, symbols: list[str], timeframes: list[str],
) -> tuple[int, int]:
    """Determine the common time window across all (symbol, tf) pairs."""
    con = sqlite3.connect(str(db_path))
    try:
        placeholders = ",".join("?" for _ in symbols)
        rows = con.execute(
            f"""
            SELECT symbol, timeframe, MIN(ts_ms) as first_ts, MAX(ts_ms) as last_ts
            FROM candles
            WHERE symbol IN ({placeholders})
            GROUP BY symbol, timeframe
            """,
            symbols,
        ).fetchall()
    finally:
        con.close()

    # Build per-TF bounds
    from collections import defaultdict

    tf_starts: dict[str, list[int]] = defaultdict(list)
    tf_ends: dict[str, list[int]] = defaultdict(list)
    for sym, tf, first, last in rows:
        if tf not in timeframes:
            continue
        tf_starts[tf].append(first)
        tf_ends[tf].append(last)

    # For each TF, compute when min_needed=200 candles are available
    # from its latest-starting symbol
    tf_effective_start: dict[str, int] = {}
    for tf in timeframes:
        period_ms = timeframe_to_seconds(tf) * 1000
        latest_first = max(tf_starts.get(tf, [0]))
        # 200-bar warmup from the latest-starting symbol
        tf_effective_start[tf] = latest_first + 200 * period_ms

    start_ms = max(tf_effective_start.values())
    # Earliest end across all TFs
    end_ms = min(min(v) for v in tf_ends.values())

    return start_ms, end_ms


def build_source(
    db_path: Path, symbols: list[str], timeframes: list[str],
) -> HistoricalCandleSource:
    """Load all candles into an in-memory source."""
    source = HistoricalCandleSource()
    loaded = 0
    for sym in symbols:
        for tf in timeframes:
            candles = load_all_candles(db_path, sym, tf)
            if candles:
                source.load_all(sym, tf, candles)
                loaded += 1
            else:
                print(f"  [warn] no candles for {sym} {tf}")
    print(f"  loaded {loaded} (symbol, tf) pairs")
    return source


def build_market_map(symbols: list[str]) -> dict[str, SymbolMarketContext]:
    return {
        sym: SymbolMarketContext(
            quote_volume_24h=MARKET_QUOTE_VOLUME.get(sym, 1_000_000_000),
        )
        for sym in symbols
    }


def make_config(
    live_config: Config, *, volume_filter_enabled: bool,
) -> Config:
    """Build a Config from live config with overrides for backtest mode."""
    settings = live_config.settings
    filters_override = settings.filters.model_copy(
        update={"enable_volume_filter": volume_filter_enabled},
    )
    runtime_override = settings.runtime.model_copy(
        update={"mode": Mode.PAPER},
    )
    modified = settings.model_copy(
        update={"filters": filters_override, "runtime": runtime_override},
    )
    env = EnvConfig(crypto_bot_mode=Mode.PAPER)
    return Config(settings=modified, env=env)  # type: ignore[call-arg]


def run_backtest(
    config: Config,
    symbols: list[str],
    timeframes: list[str],
    start_ms: int,
    end_ms: int,
    source: HistoricalCandleSource,
    market_map: dict[str, SymbolMarketContext],
    db_path: Path,
) -> tuple:
    """Run a single backtest and return timing + PnLSummary."""
    bt = Backtester(
        config,
        symbols=symbols,
        timeframes=timeframes,
        start_ms=start_ms,
        end_ms=end_ms,
        source=source,
        market_map=market_map,
        db=Database(db_path),
    )
    t0 = time.time()
    summary = bt.run()
    elapsed = time.time() - t0

    # Count positions from DB (open + closed)
    con = sqlite3.connect(str(db_path))
    try:
        total_positions = con.execute(
            "SELECT COUNT(*) FROM positions",
        ).fetchone()[0]
        open_positions = con.execute(
            "SELECT COUNT(*) FROM positions WHERE status='open'",
        ).fetchone()[0]
        # Count decisions
        accepted_decisions = con.execute(
            "SELECT COUNT(*) FROM decisions WHERE accepted=1",
        ).fetchone()[0]
        rejected_decisions = con.execute(
            "SELECT COUNT(*) FROM decisions WHERE accepted=0",
        ).fetchone()[0]
        # Pipeline-passed: decisions that reached executor (accepted + executor-rejected)
        pipeline_passed = con.execute(
            """SELECT COUNT(*) FROM decisions
               WHERE outcome IN ('position_opened','drawdown_halt','slot_taken','max_positions_reached','open_unrealized_drawdown')""",
        ).fetchone()[0]
    finally:
        con.close()

    return summary, elapsed, total_positions, open_positions, accepted_decisions, rejected_decisions, pipeline_passed


# ─── recommendation ──────────────────────────────────────────────────────────


def _volume_filter_recommendation(baseline, variant) -> str:
    """Compare baseline (filter ON) vs variant (filter OFF) by direction.

    Three comparison axes: total_pnl_pct, sharpe_ratio, win_rate.
    ``baseline_wins`` counts on how many of these the baseline is ahead.
    When baseline_wins >= 2 → ON objectively leads → keep filter.
    When baseline_wins < 2  → OFF leads → disable filter.
    """
    baseline_wins = sum([
        baseline.total_pnl_pct > variant.total_pnl_pct,
        baseline.sharpe_ratio > variant.sharpe_ratio,
        baseline.win_rate > variant.win_rate,
    ])
    leader = "ON" if baseline_wins >= 2 else "OFF"
    if baseline.total_trades < 20 or variant.total_trades < 20:
        return (
            f"INSUFFICIENT SAMPLE (baseline={baseline.total_trades}, "
            f"variant={variant.total_trades}) — направление есть "
            f"({leader} лидирует по большинству метрик), "
            f"но решение по нему пока не принимать."
        )
    return "KEEP volume filter ON" if baseline_wins >= 2 else "DISABLE volume filter"


# ─── main ────────────────────────────────────────────────────────────────────


def main():
    TMP_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 72)
    print("VolumeFilter comparison — baseline vs disabled")
    print("=" * 72)

    # 1. Load live settings
    print("\n[1] Loading live config …")
    live_config = load_settings(CONFIG_PATH)

    # 2. Compute time window
    print("\n[2] Computing common time window …")
    start_ms, end_ms = compute_bounds(LIVE_DB_PATH, ALL_SYMBOLS, ALL_TFS)
    start_dt = datetime.fromtimestamp(start_ms / 1000, tz=UTC)
    end_dt = datetime.fromtimestamp(end_ms / 1000, tz=UTC)
    print(f"    start: {start_dt}")
    print(f"    end:   {end_dt}")
    window_hours = (end_ms - start_ms) / 3_600_000
    print(f"    window: {window_hours:.1f} h")

    # 3. Load all candle data (shared between runs)
    print("\n[3] Loading candle data …")
    source = build_source(LIVE_DB_PATH, ALL_SYMBOLS, ALL_TFS)
    market_map = build_market_map(ALL_SYMBOLS)

    # 4. Run baseline (filter enabled)
    print("\n[4] Baseline — enable_volume_filter = true")
    config_on = make_config(live_config, volume_filter_enabled=True)
    db_on = TMP_DIR / "baseline.db"
    if db_on.exists():
        db_on.unlink()
    result_on = run_backtest(
        config_on, ALL_SYMBOLS, ALL_TFS, start_ms, end_ms,
        source, market_map, db_on,
    )
    summary_on, elapsed_on, total_pos_on, open_pos_on, accepted_on, rejected_on, pipeline_on = result_on

    print(f"    time: {elapsed_on:.1f} s")
    print(f"    positions (total): {total_pos_on} (open={open_pos_on})")
    print(f"    decisions: {accepted_on} accepted, {rejected_on} rejected")
    print(f"    pipeline-passed: {pipeline_on}")
    print(f"    trades:    {summary_on.total_trades}")
    print(f"    win_rate:  {summary_on.win_rate*100:.1f}%")
    print(f"    total_pnl: {summary_on.total_pnl_abs:+.2f} ({summary_on.total_pnl_pct:+.2f}%)")
    print(f"    sharpe:    {summary_on.sharpe_ratio:.2f}")
    print(f"    max_dd:    {summary_on.max_drawdown_pct:.2f}%")
    print(f"    closed_by_sl/tp: {summary_on.closed_by_sl}/{summary_on.closed_by_tp}")

    # 5. Run variant (filter disabled)
    print("\n[5] Variant — enable_volume_filter = false")
    config_off = make_config(live_config, volume_filter_enabled=False)
    db_off = TMP_DIR / "variant.db"
    if db_off.exists():
        db_off.unlink()
    result_off = run_backtest(
        config_off, ALL_SYMBOLS, ALL_TFS, start_ms, end_ms,
        source, market_map, db_off,
    )
    summary_off, elapsed_off, total_pos_off, open_pos_off, accepted_off, rejected_off, pipeline_off = result_off

    print(f"    time: {elapsed_off:.1f} s")
    print(f"    positions (total): {total_pos_off} (open={open_pos_off})")
    print(f"    decisions: {accepted_off} accepted, {rejected_off} rejected")
    print(f"    pipeline-passed: {pipeline_off}")
    print(f"    trades:    {summary_off.total_trades}")
    print(f"    win_rate:  {summary_off.win_rate*100:.1f}%")
    print(f"    total_pnl: {summary_off.total_pnl_abs:+.2f} ({summary_off.total_pnl_pct:+.2f}%)")
    print(f"    sharpe:    {summary_off.sharpe_ratio:.2f}")
    print(f"    max_dd:    {summary_off.max_drawdown_pct:.2f}%")
    print(f"    closed_by_sl/tp: {summary_off.closed_by_sl}/{summary_off.closed_by_tp}")

    # 6. Comparison table
    print("\n" + "=" * 72)
    print("Сравнение")
    print("=" * 72)
    print(f"{'Metric':<25} {'Baseline (on)':>15} {'Variant (off)':>15} {'Change':>15}")
    print("-" * 72)
    pairs = [
        ("Total trades", summary_on.total_trades, summary_off.total_trades),
        ("Win rate", f"{summary_on.win_rate*100:.1f}%", f"{summary_off.win_rate*100:.1f}%"),
        ("Total P&L %", f"{summary_on.total_pnl_pct:+.2f}%", f"{summary_off.total_pnl_pct:+.2f}%"),
        ("Sharpe", f"{summary_on.sharpe_ratio:.2f}", f"{summary_off.sharpe_ratio:.2f}"),
        ("Max drawdown %", f"{summary_on.max_drawdown_pct:.2f}%", f"{summary_off.max_drawdown_pct:.2f}%"),
        ("Closed by SL", summary_on.closed_by_sl, summary_off.closed_by_sl),
        ("Closed by TP", summary_on.closed_by_tp, summary_off.closed_by_tp),
        ("Total positions", total_pos_on, total_pos_off),
        ("Pipeline-passed", pipeline_on, pipeline_off),
        ("Accepted decisions", accepted_on, accepted_off),
        ("Rejected decisions", rejected_on, rejected_off),
    ]
    for name, on_val, off_val in pairs:
        if isinstance(on_val, int | float) and isinstance(off_val, int | float):
            delta = off_val - on_val
            sign = "+" if delta > 0 else ""
            if isinstance(on_val, int):
                delta_str = f"{sign}{delta}"
            else:
                delta_str = f"{sign}{delta:.2f}"
        else:
            delta_str = ""
        print(f"{name:<25} {str(on_val):>15} {str(off_val):>15} {delta_str:>15}")

    # 7. Decision helper
    print("\n" + "=" * 72)
    print("Analysis")
    print("=" * 72)

    trade_growth = summary_off.total_trades - summary_on.total_trades
    trade_pct = (
        ((summary_off.total_trades - summary_on.total_trades) / max(1, summary_on.total_trades)) * 100
    )
    print(f"  Trade freq: {summary_on.total_trades} -> {summary_off.total_trades} ({trade_pct:+.0f}%)")
    print(f"  Win rate:   {summary_on.win_rate*100:.1f}% -> {summary_off.win_rate*100:.1f}%")
    print(f"  Sharpe:     {summary_on.sharpe_ratio:.2f} -> {summary_off.sharpe_ratio:.2f}")
    print(f"  Max DD:     {summary_on.max_drawdown_pct:.2f}% -> {summary_off.max_drawdown_pct:.2f}%")
    print(f"  Pipeline-passed: {pipeline_on} -> {pipeline_off} ({(pipeline_off-pipeline_on)/max(1,pipeline_on)*100:+.0f}%)")
    print(f"  Accepted decisions: {accepted_on} -> {accepted_off} ({(accepted_off-accepted_on)/max(1,accepted_on)*100:+.0f}%)")

    # Breakeven — жизнеспособна ли стратегия в принципе
    rr_raw = live_config.settings.risk.take_profit_risk_multiple
    rr = sum(rr_raw.values()) / len(rr_raw) if isinstance(rr_raw, dict) else rr_raw  # type: ignore[union-attr]
    fee_pct = 0.2  # 0.1% entry + 0.1% exit
    eff_rr = (rr - fee_pct / 100.0 * rr) / (1.0 + fee_pct / 100.0)
    be_net = 1.0 / (1.0 + eff_rr) * 100.0
    print(f"\n  Breakeven win rate (net, after {fee_pct}% fees, RR={rr:.2f}): {be_net:.1f}%")
    for label, wr, n in [("Baseline", summary_on.win_rate * 100, summary_on.total_trades),
                          ("Variant",  summary_off.win_rate * 100, summary_off.total_trades)]:
        vs_be = wr - be_net
        sign = "+" if vs_be >= 0 else ""
        if wr >= be_net:
            verdict = "ABOVE breakeven"
        else:
            gap_pp = be_net - wr
            verdict = f"BELOW breakeven by {gap_pp:.1f}pp"
        print(f"  {label:>8}: win rate {wr:.1f}% ({verdict}, diff={sign}{vs_be:.1f}pp, n={n})")

    # Сравнение по направлению: какой вариант объективно лучше?
    print()
    rec = _volume_filter_recommendation(summary_on, summary_off)
    print(f"  => {rec}")

    # Save results to file for clean reference
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    result_file = LOG_DIR / f"volume_filter_comparison_{timestamp}.txt"
    with open(result_file, "w", encoding="utf-8") as f:
        f.write(f"Volume Filter Comparison - {datetime.now(UTC).isoformat()}\n")
        f.write("=" * 72 + "\n\n")
        f.write("Metric                      Baseline (on)   Variant (off)          Change\n")
        f.write("-" * 72 + "\n")
        for name, on_val, off_val in pairs:
            if isinstance(on_val, int | float) and isinstance(off_val, int | float):
                delta = off_val - on_val
                sign = "+" if delta > 0 else ""
                if isinstance(on_val, int):
                    delta_str = f"{sign}{delta}"
                else:
                    delta_str = f"{sign}{delta:.2f}"
            else:
                delta_str = ""
            f.write(f"{name:<25} {str(on_val):>15} {str(off_val):>15} {delta_str:>15}\n")
        f.write("\n" + "=" * 72 + "\n")
        f.write("Analysis\n")
        f.write("=" * 72 + "\n")
        f.write(f"  Trade freq: {summary_on.total_trades} -> {summary_off.total_trades} ({trade_pct:+.0f}%)\n")
        f.write(f"  Win rate:   {summary_on.win_rate*100:.1f}% -> {summary_off.win_rate*100:.1f}%\n")
        f.write(f"  Sharpe:     {summary_on.sharpe_ratio:.2f} -> {summary_off.sharpe_ratio:.2f}\n")
        f.write(f"  Max DD:     {summary_on.max_drawdown_pct:.2f}% -> {summary_off.max_drawdown_pct:.2f}%\n")
        f.write(f"  Pipeline-passed: {pipeline_on} -> {pipeline_off} ({(pipeline_off-pipeline_on)/max(1,pipeline_on)*100:+.0f}%)\n")
        f.write(f"  Accepted decisions: {accepted_on} -> {accepted_off} ({(accepted_off-accepted_on)/max(1,accepted_on)*100:+.0f}%)\n")
        f.write(f"\n  Breakeven win rate (net, after {fee_pct}% fees, RR={rr:.2f}): {be_net:.1f}%\n")
        for label, wr, n in [("Baseline", summary_on.win_rate * 100, summary_on.total_trades),
                              ("Variant",  summary_off.win_rate * 100, summary_off.total_trades)]:
            vs_be = wr - be_net
            sign = "+" if vs_be >= 0 else ""
            if wr >= be_net:
                verdict = "ABOVE breakeven"
            else:
                gap_pp = be_net - wr
                verdict = f"BELOW breakeven by {gap_pp:.1f}pp"
            f.write(f"  {label:>8}: win rate {wr:.1f}% ({verdict}, {sign}{vs_be:.1f}pp, n={n})\n")
        f.write(f"\n  => {_volume_filter_recommendation(summary_on, summary_off)}\n")
    print(f"\nResults saved to: {result_file}")


if __name__ == "__main__":
    main()
