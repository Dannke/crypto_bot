"""Синтетический тест: сравнивает решения живого пути
(build_features_batch → pipeline.process) и Backtester'а на синтетических
данных.

Параметризация: (config_type, scenario).

config_type:
  - golden:   ultra-short indicator periods, filters OFF, min_score=30
  - production: default config with real filter thresholds

scenario:
  - uptrend:   strong monotonic uptrend (≈1.5%/bar) — accept path (BUY)
  - downtrend: strong monotonic downtrend (≈1.5%/bar) — accept path (SELL)
  - flat:      low-amplitude random walk — reject path (no_direction
               and/or volatility_out_of_range)

Для production-конфига передаётся market_map= с синтетическими объёмами,
чтобы LiquidityFilter/VolumeFilter не отклоняли тривиально.
"""
from __future__ import annotations

import pytest

from crypto_bot.config.env import Config
from crypto_bot.core.types import Candle
from crypto_bot.features.batch import build_features_batch
from crypto_bot.features.builder import builder_from_settings
from crypto_bot.features.context import SymbolMarketContext
from crypto_bot.pipeline.factory import (
    build_decision_pipeline,
    build_strategy_manager,
    get_active_strategy,
)
from crypto_bot.simulation.backtester import Backtester
from crypto_bot.simulation.decision_aggregate import aggregate_raw_rows
from crypto_bot.simulation.decision_diff import compare_decisions
from crypto_bot.simulation.executor import SignalExecutor
from crypto_bot.simulation.historical_source import HistoricalCandleSource
from crypto_bot.simulation.pnl import PnLTracker
from crypto_bot.storage.db import Database, Repositories

from ._shared import (
    uptrend_candles,
    downtrend_candles,
    flat_candles,
    golden_config,
    production_config,
    MARKET_QUOTE_VOLUME,
)

SYMBOL = "BTC/USDT"
TIMEFRAMES = ["1h"]
PERIOD_MS = 3_600_000

CONFIGS: dict[str, Config] = {
    "golden": golden_config(),
    "production": production_config(),
}

SCENARIOS: dict[str, list[Candle]] = {
    "uptrend": uptrend_candles(),
    "downtrend": downtrend_candles(),
    "flat": flat_candles(),
}

# Need to re-run each test with a unique db — parametrize by name so pytest
# can enumerate them sensibly.
_PARAMS = [(cfg_name, scn_name) for cfg_name in CONFIGS for scn_name in SCENARIOS]


def _run_live_path(
    config: Config,
    symbol: str,
    candles: list[Candle],
    timeframes: list[str],
    db_path: str,
    market_map: dict[str, SymbolMarketContext],
) -> None:
    """Simulate the live orchestrator path bar-by-bar.

    For each bar timestamp:
    1. Slice candles up to as_of
    2. build_features_batch → pipeline.process(as_of_ms=None)
    3. Journal decisions via executor
    """
    settings = config.settings
    quote = settings.universe.quote.upper()
    trigger_tf = timeframes[0]

    builder = builder_from_settings(settings)
    pipeline = build_decision_pipeline(settings)
    strategy_mgr = build_strategy_manager(settings, strategy_name="per_timeframe")
    strategy = get_active_strategy(strategy_mgr)
    db = Database(db_path)
    repos = Repositories(db)
    executor = SignalExecutor(config, repos, PnLTracker())

    start_ms = candles[0].timestamp
    end_ms = candles[-1].timestamp + PERIOD_MS

    for as_of in range(start_ms, end_ms + 1, PERIOD_MS):
        sliced = [c for c in candles if c.timestamp + PERIOD_MS <= as_of]
        if len(sliced) < 2:
            continue

        symbol_candles = {symbol: {tf: list(sliced) for tf in timeframes}}
        features_by_symbol = build_features_batch(
            symbol_candles, builder, market_map,
            quote=quote, trigger_tf=trigger_tf,
        )
        if not features_by_symbol:
            continue

        result = pipeline.process(
            features_by_symbol, strategy, per_timeframe=True, as_of_ms=None,
        )
        for report in result.get("selected", []):
            executor.handle_selected(report)
        for report in result.get("rejected", []):
            executor.handle_rejected(report)

    db.close()


def _read_decisions(db_path: str) -> list[dict]:
    import sqlite3

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT ts_ms, symbol, timeframe, accepted, reject_reason, score, signal "
            "FROM decisions ORDER BY ts_ms"
        ).fetchall()
    finally:
        con.close()
    return [dict(r) for r in rows]


@pytest.mark.parametrize("cfg_name,scenario", _PARAMS)
def test_live_vs_backtester_synthetic(tmp_path, cfg_name, scenario):
    candles = SCENARIOS[scenario]
    config = CONFIGS[cfg_name]

    start_ms = candles[0].timestamp
    end_ms = candles[-1].timestamp + PERIOD_MS

    # market_map — synthetic but plausible quote volumes
    qv = MARKET_QUOTE_VOLUME.get(SYMBOL, 1_000_000_000)
    market_map = {SYMBOL: SymbolMarketContext(quote_volume_24h=qv)}

    # --- Live path ---
    live_db = str(tmp_path / f"live_{cfg_name}_{scenario}.db")
    _run_live_path(config, SYMBOL, candles, TIMEFRAMES, live_db, market_map)

    # --- Backtester path ---
    source = HistoricalCandleSource()
    source.load_all(SYMBOL, TIMEFRAMES[0], candles)

    bt_db = str(tmp_path / f"bt_{cfg_name}_{scenario}.db")
    bt = Backtester(
        config,
        symbols=[SYMBOL],
        timeframes=TIMEFRAMES,
        start_ms=start_ms,
        end_ms=end_ms,
        source=source,
        market_map=market_map,
        db=Database(bt_db),
    )
    bt.run()

    # --- Compare ---
    live_raw = _read_decisions(live_db)
    bt_raw = _read_decisions(bt_db)

    live = aggregate_raw_rows(live_raw)
    backtest = aggregate_raw_rows(bt_raw)

    report = compare_decisions(live, backtest, score_tolerance=1.0)
    assert report.is_clean, report.summary()