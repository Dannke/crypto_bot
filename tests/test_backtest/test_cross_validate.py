"""Кросс-валидация: сравнивает решения живого бота (decisions в боевой БД) с
решениями Backtester'а, прогнанного на том же историческом окне того же
(symbol, timeframe). Совпадение — доказательство, что Backtester не врёт.
Расхождение — конкретный бар для ручного разбора.

ВАЖНО — прочитать перед запуском:
1. FIX_CUTOFF_MS ниже нужно проставить вручную = timestamp момента, когда
   бот был перезапущен ПОСЛЕ всех фиксов (per_timeframe, ts_ms историческое
   время, cooldown as_of_ms, корреляция по timestamp). Данные до этого
   момента отражают старую, местами ошибочную логику и не годятся для
   сравнения — не потому что "плохие", а потому что сравнивать с ними
   после фикса логики физически бессмысленно.
2. Backtester вызывается с РОВНО ОДНИМ timeframe за раз. Часы движка
   привязаны к timeframes[0] — при нескольких TF в одном прогоне бары
   быстрых TF пересчитывались бы реже, чем в live, и сравнение потеряло
   бы смысл для них.
3. db=Database(...) передаётся ЯВНО и ВСЕГДА. Backtester без этого
   параметра молча падает на settings.storage.db_path — то есть на живую
   БД бота. Пропуск этого параметра — не опция, а порча живого журнала.
4. source=HistoricalCandleSource() передаётся ЯВНО и загружается свечами
   из той же БД (LIVE_DB_PATH), откуда читаются живые решения.
   Загружаются свечи для тестируемого символа, а также для BTC и ETH
   (нужны для корреляционных фич).
5. market_map= передаётся из фиксированных констант — исторических тикеров
   у нас нет, поэтому quote_volume берётся из констант ниже. Если объём
   сильно изменился — значение нужно обновить.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from crypto_bot.config.settings import load_settings
from crypto_bot.core.policy import timeframe_to_seconds
from crypto_bot.core.types import Candle
from crypto_bot.features.context import SymbolMarketContext
from crypto_bot.simulation.backtester import Backtester
from crypto_bot.simulation.decision_aggregate import aggregate_raw_rows
from crypto_bot.simulation.decision_diff import compare_decisions
from crypto_bot.simulation.historical_source import HistoricalCandleSource
from crypto_bot.storage.db import Database

# --- Заполнить перед запуском ------------------------------------------------
LIVE_DB_PATH = Path("data/crypto_bot.db")
FIX_CUTOFF_MS = 1783613354213  # 2026-07-09T16:09:14.213835+00:00 — рестарт после всех фиксов
CONFIG_PATH = "config/settings.yaml"
SCORE_TOLERANCE = 1.0  # известный источник шума: cross-correlation, см. docs/plan_stage2.md

# Приблизительный 24h quote volume для symbols (USD) — берётся из тикеров
# на момент периода валидации. Исторических тикеров у нас нет, поэтому
# фиксированные константы; если объём изменился — обновить.
MARKET_QUOTE_VOLUME: dict[str, float] = {
    "BTC/USDT": 20_000_000_000,
    "ETH/USDT": 10_000_000_000,
    "SOL/USDT": 2_000_000_000,
    "BNB/USDT": 1_000_000,         # реальный 24h объём был ниже min_quote_volume_usd=5M
    "XRP/USDT": 1_500_000_000,
    "ADA/USDT": 1_000_000,         # реальный 24h объём был ниже min_quote_volume_usd=5M
    "AVAX/USDT": 500_000_000,
    "DOGE/USDT": 1_000_000,        # реальный 24h объём был ниже min_quote_volume_usd=5M
    "POL/USDT": 1_000_000,         # реальный 24h объём был ниже min_quote_volume_usd=5M
}

# ------------------------------------------------------------------------------

DECISIONS_COLUMNS = ("ts_ms", "symbol", "timeframe", "accepted", "reject_reason", "score", "signal")


def _read_decisions(
    db_path: Path, symbol: str, timeframe: str, start_ms: int, end_ms: int,
) -> list[dict]:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            f"""
            SELECT {", ".join(DECISIONS_COLUMNS)}
            FROM decisions
            WHERE symbol = ? AND timeframe = ? AND ts_ms BETWEEN ? AND ?
            ORDER BY ts_ms
            """,
            (symbol, timeframe, start_ms, end_ms),
        ).fetchall()
    finally:
        con.close()
    return [dict(r) for r in rows]


def _load_all_candles(db_path: Path, symbol: str, timeframe: str) -> list[Candle]:
    """Load ALL candles for (symbol, timeframe) from a DB file (no cap)."""
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
        Candle(timestamp=int(r["ts_ms"]), open=float(r["open"]),
               high=float(r["high"]), low=float(r["low"]),
               close=float(r["close"]), volume=float(r["volume"]))
        for r in rows
    ]


def _load_source_for_backtest(
    live_db_path: Path,
    symbol: str,
    timeframe: str,
    *extra_symbols: str,
) -> HistoricalCandleSource | None:
    """Load candles for *symbol* + *extra_symbols* into a HistoricalCandleSource.

    Returns None if the primary *symbol* has no candles at all.
    """
    primary = _load_all_candles(live_db_path, symbol, timeframe)
    if not primary:
        return None

    source = HistoricalCandleSource()
    source.load_all(symbol, timeframe, primary)

    for extra in extra_symbols:
        extra_candles = _load_all_candles(live_db_path, extra, timeframe)
        if extra_candles:
            source.load_all(extra, timeframe, extra_candles)

    return source


CROSS_VALIDATE_PAIRS = [
    ("BTC/USDT", "5m"),
    ("BTC/USDT", "15m"),
    ("ETH/USDT", "5m"),
    ("ETH/USDT", "15m"),
    ("SOL/USDT", "5m"),
    ("SOL/USDT", "15m"),
    ("BNB/USDT", "5m"),
    ("BNB/USDT", "15m"),
    ("XRP/USDT", "5m"),
    ("XRP/USDT", "15m"),
    ("ADA/USDT", "5m"),
    ("ADA/USDT", "15m"),
    ("AVAX/USDT", "5m"),
    ("AVAX/USDT", "15m"),
    ("DOGE/USDT", "5m"),
    ("DOGE/USDT", "15m"),
    ("POL/USDT", "5m"),
    ("POL/USDT", "15m"),
    ("BTC/USDT", "1h"),
    ("ETH/USDT", "1h"),
    ("SOL/USDT", "1h"),
    ("BNB/USDT", "1h"),
    ("XRP/USDT", "1h"),
    ("ADA/USDT", "1h"),
    ("AVAX/USDT", "1h"),
    ("DOGE/USDT", "1h"),
    ("POL/USDT", "1h"),
    ("BTC/USDT", "4h"),
    ("ETH/USDT", "4h"),
    ("SOL/USDT", "4h"),
    ("BNB/USDT", "4h"),
    ("XRP/USDT", "4h"),
    ("ADA/USDT", "4h"),
    ("AVAX/USDT", "4h"),
    ("DOGE/USDT", "4h"),
    ("POL/USDT", "4h"),
]


@pytest.mark.cross_validate_live
@pytest.mark.parametrize("symbol,timeframe", CROSS_VALIDATE_PAIRS)
def test_cross_validate(tmp_path, symbol, timeframe):
    raw_live = _read_decisions(
        LIVE_DB_PATH, symbol, timeframe, FIX_CUTOFF_MS, end_ms=2**63 - 1,
    )
    if not raw_live:
        pytest.skip(
            f"нет живых данных для {symbol} {timeframe} после FIX_CUTOFF_MS={FIX_CUTOFF_MS}",
        )

    start_ms = raw_live[0]["ts_ms"]
    period_ms = timeframe_to_seconds(timeframe) * 1000
    # end_ms must extend past the last decision's open-time so the backtester
    # clock includes the bar that produced that decision
    end_ms = raw_live[-1]["ts_ms"] + period_ms

    # Load candles: primary symbol + BTC + ETH (needed for correlation features)
    source = _load_source_for_backtest(
        LIVE_DB_PATH, symbol, timeframe, "BTC/USDT", "ETH/USDT",
    )
    if source is None:
        pytest.skip(f"нет свечей для {symbol} {timeframe} в {LIVE_DB_PATH}")

    config = load_settings(CONFIG_PATH)
    bt_db_path = tmp_path / f"cv_{symbol.replace('/', '_')}_{timeframe}.db"

    # market_map: give each symbol a realistic quote volume so liquidity
    # filter doesn't reject on missing ticker context
    quote_volume = MARKET_QUOTE_VOLUME.get(symbol, 1_000_000_000)
    market_map = {symbol: SymbolMarketContext(quote_volume_24h=quote_volume)}

    backtester = Backtester(
        config,
        symbols=[symbol],
        timeframes=[timeframe],
        start_ms=start_ms,
        end_ms=end_ms,
        source=source,
        market_map=market_map,
        db=Database(bt_db_path),
    )
    backtester.run()

    raw_backtest = _read_decisions(bt_db_path, symbol, timeframe, start_ms, end_ms)

    live = aggregate_raw_rows(raw_live)
    backtest = aggregate_raw_rows(raw_backtest)

    report = compare_decisions(live, backtest, score_tolerance=SCORE_TOLERANCE)
    # missing_in_live is expected — the live bot can skip cycles (maintenance,
    # network issues).  We only care about: bars the backtester missed
    # (missing_in_backtest) and decisions that differ on the same bar
    # (mismatches).
    assert not report.missing_in_backtest and not report.mismatches, report.summary()
