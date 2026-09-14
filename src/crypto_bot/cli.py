# src/crypto_bot/cli.py

import asyncio
import csv
import io
import json
import sys
from pathlib import Path

import click

from crypto_bot.config.settings import load_settings
from crypto_bot.core.exceptions import ConfigError
from crypto_bot.orchestrator import run_orchestrator
from crypto_bot.orchestrator_portfolio import run_portfolio_orchestrator
from crypto_bot.storage.db import Database, Repositories

# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #

def _load(config_path: str):
    config_obj = load_settings(yaml_path=Path(config_path))
    settings = config_obj.settings
    click.echo(f"🚀 crypto_bot started in {settings.runtime.mode} mode")
    click.echo(f"Exchange: {settings.exchange.name} (sandbox: {settings.exchange.sandbox})")
    return config_obj


def _run(config_path: str):
    config_obj = _load(config_path)
    from crypto_bot.core.validators import validate_runtime_safety
    validate_runtime_safety(config_obj)
    asyncio.run(run_orchestrator(config_obj))


# --------------------------------------------------------------------------- #
# CLI group
# --------------------------------------------------------------------------- #

@click.group(invoke_without_command=True)
@click.option("--config", default="config/settings.yaml", help="Path to settings YAML file")
@click.pass_context
def cli(ctx, config):
    """crypto_bot — крипто-трейдинговый бот."""
    ctx.ensure_object(dict)
    ctx.obj["config"] = config
    if ctx.invoked_subcommand is None:
        # no subcommand → legacy behaviour: run scan
        try:
            _run(config)
        except ConfigError as e:
            click.echo(f"❌ Configuration error: {e}", err=True)
            sys.exit(1)
        except Exception as e:  # noqa: BLE001
            click.echo(f"💥 Fatal error: {e}", err=True)
            sys.exit(1)


# --------------------------------------------------------------------------- #
# Subcommands
# --------------------------------------------------------------------------- #

@cli.command()
@click.option("--config", default=None, help="Path to settings YAML file (overrides group default)")
@click.pass_context
def run(ctx, config):
    """Запуск основного цикла сканирования (эквивалент ``crypto-bot --config ...``)."""
    config = config or ctx.obj.get("config", "config/settings.yaml")
    try:
        _run(config)
    except ConfigError as e:
        click.echo(f"❌ Configuration error: {e}", err=True)
        sys.exit(1)
    except Exception as e:  # noqa: BLE001
        click.echo(f"💥 Fatal error: {e}", err=True)
        sys.exit(1)


def _fetch_current_prices(config_obj, repos, symbols: set[str]) -> dict[str, float]:
    """Fetch current prices from exchange; fallback to DB candle close."""
    prices: dict[str, float] = {}

    async def _fetch():
        try:
            from crypto_bot.data.exchange import MarketDataClient
            async with MarketDataClient(config_obj) as client:
                tickers = await client.fetch_tickers(list(symbols))
                for sym in symbols:
                    t = tickers.get(sym, {})
                    if t and t.get("last"):
                        prices[sym] = float(t["last"])
        except Exception:
            pass

    try:
        import logging
        logging.getLogger("crypto_bot.data.exchange").setLevel(logging.ERROR)
        asyncio.run(_fetch())
    except Exception:
        pass

    # fallback to DB for symbols still missing
    for sym in symbols:
        if sym not in prices:
            close = repos.candles.latest_close(sym)
            if close:
                prices[sym] = close

    return prices


@cli.command()
@click.option("--config", default=None, help="Path to settings YAML file")
@click.option(
    "--format", "output_format",
    type=click.Choice(["table", "csv", "json"]), default="table",
    help="Output format",
)
@click.option(
    "--status", default=None, type=click.Choice(["open", "closed"]),
    help="Filter by position status",
)
@click.option(
    "--out", "out_path", default=None, type=click.Path(),
    help="Write to file instead of stdout",
)
@click.pass_context
def positions(ctx, config, output_format, status, out_path):
    """View and export positions from the database."""
    config_path = config or ctx.obj.get("config", "config/settings.yaml")
    try:
        config_obj = load_settings(yaml_path=Path(config_path))
        db_path = config_obj.settings.storage.db_path
    except ConfigError as e:
        click.echo(f"Configuration error: {e}", err=True)
        sys.exit(1)

    db = Database(db_path)
    repos = Repositories(db)

    if status == "closed":
        rows = repos.positions.list_closed(limit=1000)
    elif status == "open":
        rows = repos.positions.list_open()
    else:
        rows = repos.positions.list_open() + repos.positions.list_closed(limit=1000)

    if not rows:
        click.echo("No positions found.")
        db.close()
        return

    # Fetch current prices for symbols with open positions
    symbols = {p.symbol for p in rows if p.status.value == "open"}
    current_prices = _fetch_current_prices(config_obj, repos, symbols) if symbols else {}

    closed_rows = [p for p in rows if p.status.value == "closed"]
    total_closed = len(closed_rows)
    if total_closed:
        wins = [p for p in closed_rows if p.pnl_pct is not None and p.pnl_pct > 0]
        by_sl = [p for p in closed_rows if p.closed_by == "stop_loss"]
        by_tp = [p for p in closed_rows if p.closed_by == "take_profit"]
        win_rate = len(wins) / total_closed * 100
        total_pnl = sum(p.pnl_pct or 0.0 for p in closed_rows)
        sl_pnl = sum(p.pnl_pct or 0.0 for p in by_sl)
        tp_pnl = sum(p.pnl_pct or 0.0 for p in by_tp)
    else:
        win_rate = total_pnl = sl_pnl = tp_pnl = 0.0

    def unrealized_pnl(entry: float, price: float, side) -> float | None:
        if price <= 0:
            return None
        if side.value == "LONG":
            return (price - entry) / entry * 100.0
        else:
            return (entry - price) / entry * 100.0

    if output_format == "json":
        data = [
            {
                "id": p.id,
                "symbol": p.symbol,
                "timeframe": p.timeframe,
                "side": p.side.value,
                "size": p.size,
                "entry_price": p.entry_price,
                "current_price": current_prices.get(p.symbol),
                "unrealized_pnl_pct": (
                    unrealized_pnl(p.entry_price, current_prices[p.symbol], p.side)
                    if p.symbol in current_prices and p.status.value == "open"
                    else None
                ),
                "stop": p.stop,
                "take": p.take,
                "status": p.status.value,
                "closed_by": p.closed_by,
                "exit_price": p.exit_price,
                "pnl_pct": p.pnl_pct,
                "opened_at": p.opened_at.isoformat() if p.opened_at else None,
                "closed_at": p.closed_at.isoformat() if p.closed_at else None,
            }
            for p in rows
        ]
        summary_block = {
            "summary": {
                "total_closed": total_closed,
                "win_rate_pct": round(win_rate, 1),
                "total_pnl_pct": round(total_pnl, 2),
                "closed_by_sl": len(by_sl),
                "sl_pnl_pct": round(sl_pnl, 2),
                "closed_by_tp": len(by_tp),
                "tp_pnl_pct": round(tp_pnl, 2),
            }
        }
        data.append(summary_block)
        output = json.dumps(data, indent=2, ensure_ascii=False)
    elif output_format == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["id", "symbol", "timeframe", "side", "size", "entry", "current",
                         "unr_pnl%", "stop", "take", "status", "closed_by", "exit", "pnl%",
                         "opened_at", "closed_at"])
        for p in rows:
            cur = current_prices.get(p.symbol, "")
            unr = (
                round(unrealized_pnl(p.entry_price, current_prices[p.symbol], p.side), 2)
                if p.symbol in current_prices and p.status.value == "open"
                else ""
            )
            writer.writerow([
                p.id, p.symbol, p.timeframe, p.side.value, round(p.size, 6),
                round(p.entry_price, 8), cur, unr,
                round(p.stop, 8), round(p.take, 8),
                p.status.value, p.closed_by or "",
                round(p.exit_price, 8) if p.exit_price else "",
                round(p.pnl_pct, 2) if p.pnl_pct is not None else "",
                p.opened_at.isoformat() if p.opened_at else "",
                p.closed_at.isoformat() if p.closed_at else "",
            ])
        if total_closed:
            writer.writerow([])
            writer.writerow(["summary", "", "", "", "", "", "", "", "", "", "", "", "", "", ""])
            writer.writerow(["total_closed", total_closed])
            writer.writerow(["win_rate%", f"{win_rate:.1f}"])
            writer.writerow(["total_pnl%", f"{total_pnl:+.2f}"])
            writer.writerow(["closed_by_sl", len(by_sl), "pnl%", f"{sl_pnl:+.2f}"])
            writer.writerow(["closed_by_tp", len(by_tp), "pnl%", f"{tp_pnl:+.2f}"])
        output = buf.getvalue()
    else:
        header = (
            f"{'ID':>4} {'Symbol':<10} {'TF':<5} {'Side':<6} {'Size':>10} "
            f"{'Entry':>11} {'Cur':>11} {'Unr%':>7} "
            f"{'Stop':>11} {'Take':>11} {'Status':<8} "
            f"{'Exit':>11} {'P&L%':>7} {'Opened':<16}"
        )
        sep = "-" * len(header)
        lines = [sep, header, sep]
        for p in rows:
            cur = current_prices.get(p.symbol, 0.0)
            unr = unrealized_pnl(p.entry_price, cur, p.side) if cur and cur > 0 and p.status.value == "open" else None
            cur_str = f"{cur:>11.6f}" if cur else ""
            unr_str = f"{unr:>+6.2f}%" if unr is not None else ""
            pnl_str = f"{p.pnl_pct:>+6.2f}%" if p.pnl_pct is not None else ""
            exit_str = f"{p.exit_price:>11.6f}" if p.exit_price else ""
            opened_str = p.opened_at.strftime("%m-%d %H:%M") if p.opened_at else ""
            lines.append(
                f"{p.id:>4} {p.symbol:<10} {p.timeframe:<5} {p.side.value:<6} "
                f"{p.size:>10.4f} {p.entry_price:>11.6f} {cur_str:>11} {unr_str:>7} "
                f"{p.stop:>11.6f} {p.take:>11.6f} {p.status.value:<8} "
                f"{exit_str:>11} {pnl_str:>7} {opened_str:<16}"
            )
        lines.append(sep)
        if total_closed:
            lines.append("")
            lines.append(f"  Closed: {total_closed}  Win rate: {win_rate:.1f}%  "
                         f"Total P&L: {total_pnl:+.2f}%")
            lines.append(f"  SL: {len(by_sl)} ({sl_pnl:+.2f}%)  "
                         f"TP: {len(by_tp)} ({tp_pnl:+.2f}%)  "
                         f"Other: {total_closed - len(by_sl) - len(by_tp)}")
        output = "\n".join(lines)

    if out_path:
        Path(out_path).write_text(output, encoding="utf-8")
        click.echo(f"Written to {out_path}")
    else:
        click.echo(output)

    db.close()


@cli.command()
@click.argument("symbol", type=str)
@click.argument("timeframe", type=str)
@click.option("--start", default=None, help="Start datetime (ISO format, e.g. 2025-01-01)")
@click.option("--end", default=None, help="End datetime (ISO format, e.g. 2025-02-01)")
@click.option("--conflict", default="pessimistic", type=click.Choice(["pessimistic", "open_proximity"]))
@click.option("--config", default=None, help="Path to settings YAML file")
@click.pass_context
def backtest(ctx, symbol, timeframe, start, end, conflict, config):
    """Запуск backtest для одной пары на одном таймфрейме."""
    config_path = config or ctx.obj.get("config", "config/settings.yaml")
    from datetime import datetime as dt_mod

    try:
        config_obj = load_settings(yaml_path=Path(config_path))
    except ConfigError as e:
        click.echo(f"❌ Configuration error: {e}", err=True)
        sys.exit(1)

    # Resolve start/end timestamps
    now = dt_mod.now()
    if start:
        start_dt = dt_mod.fromisoformat(start)
        start_ms = int(start_dt.timestamp() * 1000)
    else:
        start_ms = int((now.timestamp() - 7 * 86400) * 1000)  # default: last 7 days

    if end:
        end_dt = dt_mod.fromisoformat(end)
        end_ms = int(end_dt.timestamp() * 1000)
    else:
        end_ms = int(now.timestamp() * 1000)

    from crypto_bot.simulation.backtester import Backtester

    # Isolated DB per run — never touches the live paper-trading DB
    safe_sym = symbol.upper().replace("/", "_")
    run_id = f"{safe_sym}_{timeframe}_{start_ms}_{end_ms}"
    bt_db_path = Path("data/backtest") / f"{run_id}.db"
    bt_db_path.parent.mkdir(parents=True, exist_ok=True)

    bt = Backtester(
        config_obj,
        symbols=[symbol.upper()],
        timeframes=[timeframe],
        start_ms=start_ms,
        end_ms=end_ms,
        conflict_resolution=conflict,
        db=Database(bt_db_path),
    )

    click.echo(f"Running backtest: {symbol} {timeframe} [{start_ms} .. {end_ms}]")
    summary = bt.run()

    click.echo(f"\n{'='*60}")
    click.echo(f"  BACKTEST RESULTS — {symbol} {timeframe}")
    click.echo(f"{'='*60}")
    click.echo(f"  Total trades:   {summary.total_trades}")
    click.echo(f"  Win rate:       {summary.win_rate*100:.1f}%")
    click.echo(f"  Total P&L:      {summary.total_pnl_pct:+.2f}%")
    click.echo(f"  Closed by SL:   {summary.closed_by_sl}")
    click.echo(f"  Closed by TP:   {summary.closed_by_tp}")
    click.echo(f"  Max drawdown:   {summary.max_drawdown_pct:.2f}%")
    click.echo(f"  Sharpe (simpl): {summary.sharpe_ratio:.2f}")
    click.echo(f"{'='*60}\n")


@cli.command()
@click.argument("symbol", type=str)
@click.argument("timeframe", type=str)
@click.option("--start", default=None, help="Start datetime (ISO format, e.g. 2025-01-01)")
@click.option("--end", default=None, help="End datetime (ISO format)")
@click.option("--network", default="mainnet", type=click.Choice(["mainnet", "testnet", "auto"]),
              help="Exchange network (default: mainnet — testnet has sparse history)")
@click.option("--config", default=None, help="Path to settings YAML file")
@click.pass_context
def seed_history(ctx, symbol, timeframe, start, end, network, config):
    """Загрузка исторических OHLCV данных с биржи в локальную БД."""
    config_path = config or ctx.obj.get("config", "config/settings.yaml")
    import asyncio as _asyncio

    from crypto_bot.simulation.seed_history import _run as seed_run
    _asyncio.run(seed_run(
        symbol=symbol.upper(), timeframe=timeframe,
        start=start, end=end, network=network, config_path=config_path,
    ))


@cli.command()
@click.option("--config", default=None, help="Path to settings YAML file")
@click.pass_context
def summary(ctx, config):
    """Сводка по P&L paper-торговли."""
    config_path = config or ctx.obj.get("config", "config/settings.yaml")
    try:
        config_obj = load_settings(yaml_path=Path(config_path))
        db_path = config_obj.settings.storage.db_path
    except ConfigError as e:
        click.echo(f"❌ Configuration error: {e}", err=True)
        sys.exit(1)

    db = Database(db_path)
    repos = Repositories(db)

    closed = repos.positions.list_closed(limit=10000)
    total = len(closed)
    if total == 0:
        click.echo("No closed positions found.")
        db.close()
        return

    wins = [p for p in closed if p.pnl_pct is not None and p.pnl_pct > 0]
    losses = [p for p in closed if p.pnl_pct is not None and p.pnl_pct <= 0]
    by_sl = [p for p in closed if p.closed_by == "stop_loss"]
    by_tp = [p for p in closed if p.closed_by == "take_profit"]

    click.echo(f"\n{'='*60}")
    click.echo(f"  P&L SUMMARY — {total} closed positions")
    click.echo(f"{'='*60}")
    click.echo(f"  Win rate:       {len(wins)/total*100:>6.1f}%  ({len(wins)}W / {len(losses)}L)")
    click.echo(f"  Closed by SL:   {len(by_sl):>3}  P&L={sum(p.pnl_pct or 0 for p in by_sl):>+.2f}%")
    click.echo(f"  Closed by TP:   {len(by_tp):>3}  P&L={sum(p.pnl_pct or 0 for p in by_tp):>+.2f}%")
    click.echo(f"  Other:          {total - len(by_sl) - len(by_tp):>3}")
    click.echo(f"{'='*60}\n")

    db.close()


@cli.command()
@click.option("--config", default=None, help="Path to settings YAML file")
@click.pass_context
def run_portfolio(ctx, config):
    """Запуск portfolio-режима (cross-sectional momentum и др.)."""
    config_path = config or ctx.obj.get("config", "config/settings.yaml")
    try:
        _run_portfolio(config_path)
    except Exception as e:  # noqa: BLE001
        click.echo(f"💥 Fatal error: {e}", err=True)
        sys.exit(1)


def _run_portfolio(config_path: str):
    config_obj = load_settings(yaml_path=Path(config_path))
    settings = config_obj.settings
    click.echo(f"🚀 crypto_bot portfolio started in {settings.runtime.mode} mode")
    click.echo(f"Exchange: {settings.exchange.name} (sandbox: {settings.exchange.sandbox})")
    click.echo(f"Portfolio strategy: {settings.portfolio.strategy_name}")
    from crypto_bot.core.validators import validate_runtime_safety
    validate_runtime_safety(config_obj)
    asyncio.run(run_portfolio_orchestrator(config_obj))


@cli.command()
@click.option("--config", default=None, help="Path to settings YAML file")
@click.option(
    "--price", default=None, type=float,
    help="Exit price (default: entry price = break-even)",
)
@click.option("--force", is_flag=True, help="Skip confirmation prompt")
@click.pass_context
def close_all(ctx, config, price, force):
    """Принудительно закрыть все открытые позиции."""
    config_path = config or ctx.obj.get("config", "config/settings.yaml")
    try:
        config_obj = load_settings(yaml_path=Path(config_path))
        db_path = config_obj.settings.storage.db_path
    except ConfigError as e:
        click.echo(f"Configuration error: {e}", err=True)
        sys.exit(1)

    db = Database(db_path)
    repos = Repositories(db)
    open_positions = repos.positions.list_open()

    if not open_positions:
        click.echo("No open positions to close.")
        db.close()
        return

    click.echo(f"Found {len(open_positions)} open positions:")
    for p in open_positions:
        click.echo(f"  {p.side.value} {p.symbol} {p.timeframe}  entry={p.entry_price} size={p.size:.4f}")

    if not force:
        click.confirm("Close all?", abort=True)

    exit_price = price if price is not None else open_positions[0].entry_price
    count = repos.positions.close_all_open(exit_price=exit_price, closed_by="manual")
    click.echo(f"Closed {count} position(s) at {exit_price}.")
    db.close()


@cli.command()
@click.option("--config", default=None, help="Path to settings YAML file")
@click.option("--force", is_flag=True, help="Skip confirmation prompt")
@click.pass_context
def clear_positions(ctx, config, force):
    """Очистить журнал позиций (удалить все открытые и закрытые)."""
    config_path = config or ctx.obj.get("config", "config/settings.yaml")
    try:
        config_obj = load_settings(yaml_path=Path(config_path))
        db_path = config_obj.settings.storage.db_path
    except ConfigError as e:
        click.echo(f"Configuration error: {e}", err=True)
        sys.exit(1)

    db = Database(db_path)
    repos = Repositories(db)
    open_count = len(repos.positions.list_open())
    closed_count = len(repos.positions.list_closed(limit=99999))

    click.echo(f"Open: {open_count}, Closed: {closed_count}")
    if not force:
        click.confirm("Delete ALL positions and trades? This cannot be undone.", abort=True)

    count = repos.positions.delete_all()
    click.echo(f"Deleted {count} position(s) and all related trades.")
    db.close()


if __name__ == "__main__":
    cli()

# console_scripts entry point (pyproject.toml: crypto-bot = "crypto_bot.cli:main")
main = cli
