"""Снепшотная кросс-валидация: читает замороженные fixtures (JSON) с парами
(candles + decisions) из живой БД и проверяет, что Backtester на текущем
коде воспроизводит те же решения.

Реконструирует Config целиком из config_snapshot в файле fixture, полностью
игнорируя текущий ``config/settings.yaml``.  Это означает, что плановый
тюнинг порогов (min_score, веса и т.д.) не ломает снепшот — тест проверяет
только "код всё ещё воспроизводит этот исторический сценарий при тех же
настройках, что были зафиксированы".

Маркер: ``@pytest.mark.cross_validate_snapshot`` — CI-ready, не требует
живой БД.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from crypto_bot.config.env import Config, EnvConfig
from crypto_bot.config.schemas import Settings
from crypto_bot.core.enums import Mode
from crypto_bot.core.types import Candle
from crypto_bot.features.context import SymbolMarketContext
from crypto_bot.simulation.backtester import Backtester
from crypto_bot.simulation.decision_aggregate import aggregate_raw_rows
from crypto_bot.simulation.decision_diff import compare_decisions
from crypto_bot.simulation.historical_source import HistoricalCandleSource
from crypto_bot.storage.db import Database
from ._shared import CONFIG_SNAPSHOT_KEYS

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
DECISIONS_COLUMNS = ("ts_ms", "symbol", "timeframe", "accepted", "reject_reason", "score", "signal")
SCORE_TOLERANCE = 1.0


def _fixture_paths() -> list[Path]:
    if not FIXTURE_DIR.exists():
        return []
    return sorted(FIXTURE_DIR.glob("*.json"))


def _load_fixture(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _candle_from_dict(d: dict) -> Candle:
    return Candle(timestamp=d["timestamp"], open=d["open"], high=d["high"],
                  low=d["low"], close=d["close"], volume=d["volume"])


@pytest.mark.cross_validate_snapshot
@pytest.mark.parametrize("fixture_path", _fixture_paths(), ids=lambda p: p.stem)
def test_cross_validate_snapshot(tmp_path, fixture_path):
    f = _load_fixture(fixture_path)
    symbol: str = f["symbol"]
    timeframe: str = f["timeframe"]

    # Reconstruct Config from snapshot (ignores current settings.yaml).
    # config_snapshot is a partial dump of Settings — wrap in Settings defaults
    # so model_validate fills the missing fields with their Schema defaults.
    snap_partial = Settings().model_dump()
    snap_partial.update(f["config_snapshot"])
    settings = Settings.model_validate(snap_partial)
    env = EnvConfig(crypto_bot_mode=Mode.PAPER)
    config = Config(settings=settings, env=env)

    # Load decisions (determine start/end from the snapshot, not candle timestamps)
    decisions_raw = f["decisions"]
    if not decisions_raw:
        pytest.skip(f"empty decisions in {fixture_path}")

    ts_values = [d["ts_ms"] for d in decisions_raw]
    start_ms = min(ts_values)
    period_ms = 3_600_000 if timeframe.endswith("h") else 300_000
    end_ms = max(ts_values) + period_ms

    # Load candles
    primary = [_candle_from_dict(c) for c in f["candles"]]
    if not primary:
        pytest.skip(f"empty candles in {fixture_path}")

    source = HistoricalCandleSource()
    source.load_all(symbol, timeframe, primary)

    for extra_key in ("candles_btc", "candles_eth"):
        extra_list = f.get(extra_key, [])
        if extra_list:
            extra_sym = "BTC/USDT" if "btc" in extra_key else "ETH/USDT"
            source.load_all(extra_sym, timeframe, [
                _candle_from_dict(c) for c in extra_list
            ])

    # market_map from snapshot
    mm = f.get("market_map", {})
    market_map = {symbol: SymbolMarketContext(
        quote_volume_24h=mm.get("quote_volume_24h", 1_000_000_000),
        spread_pct=mm.get("spread_pct", 0.0),
    )}

    # Run Backtester
    bt_db_path = tmp_path / "snapshot_bt.db"
    bt = Backtester(
        config,
        symbols=[symbol],
        timeframes=[timeframe],
        start_ms=start_ms,
        end_ms=end_ms,
        source=source,
        market_map=market_map,
        db=Database(bt_db_path),
    )
    bt.run()

    # Read backtest decisions
    import sqlite3

    con = sqlite3.connect(str(bt_db_path))
    con.row_factory = sqlite3.Row
    try:
        bt_rows = con.execute(
            f"""SELECT {', '.join(DECISIONS_COLUMNS)}
                FROM decisions WHERE symbol=? AND timeframe=?
                ORDER BY ts_ms""",
            (symbol, timeframe),
        ).fetchall()
    finally:
        con.close()
    bt_raw = [dict(r) for r in bt_rows]

    # Compare snapshot decisions vs backtest.
    # missing_in_live is expected — the backtester runs bars where the
    # live bot had no data yet (clock starts at the first candle, not at
    # the first live decision).  Only mismatches and missing_in_backtest
    # signal a real regression.
    snap = aggregate_raw_rows(f["decisions"])
    bt_agg = aggregate_raw_rows(bt_raw)

    report = compare_decisions(snap, bt_agg, score_tolerance=SCORE_TOLERANCE)
    assert not report.missing_in_backtest and not report.mismatches, report.summary()