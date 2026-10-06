"""Спецификации двух рынков, допуск по launchTime и версионированный снимок.

План funding/basis, Task 3: спецификации `category=spot` и `category=linear` с
`launchTime`; снимок для бэктеста вместо живого кэша с TTL 24 ч (п. 3 и 13
бэклога); допуск инструмента на тике — `launchTime ≤ t` (п. 11).
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from crypto_bot.config.env import Config, EnvConfig
from crypto_bot.config.schemas import Settings
from crypto_bot.core.enums import Side, StrategyType
from crypto_bot.core.types import Candle
from crypto_bot.data.instruments import (
    BybitInstrumentsClient,
    InstrumentCache,
    InstrumentSpec,
    write_snapshot,
)
from crypto_bot.portfolio import PositionIntent
from crypto_bot.simulation import backtester as backtester_module
from crypto_bot.simulation import walk_forward
from crypto_bot.simulation.historical_source import HistoricalCandleSource
from crypto_bot.simulation.pnl import PnLSummary, PnLTracker
from crypto_bot.simulation.portfolio_executor import PortfolioExecutor
from crypto_bot.storage.db import Database, Repositories

LAUNCH_BTC = 1_584_230_400_000  # 2020-03-15 00:00 UTC — launchTime BTCUSDT (Task 0', приложение B)
HOUR = 3_600_000

# Формы ответа /v5/market/instruments-info; числа — из снимка Task 0', приложение B
LINEAR_ITEM = {
    "symbol": "BTCUSDT", "contractType": "LinearPerpetual", "status": "Trading",
    "launchTime": str(LAUNCH_BTC),
    "lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001", "minNotionalValue": "5"},
    "priceFilter": {"tickSize": "0.10"},
    "leverageFilter": {"maxLeverage": "100.00"},
}
SPOT_ITEM = {
    "symbol": "BTCUSDT", "status": "Trading", "marginTrading": "utaOnly",
    "lotSizeFilter": {"basePrecision": "0.000001", "minOrderQty": "0.000001", "minOrderAmt": "5"},
    "priceFilter": {"tickSize": "0.01"},
}


def _spec(symbol: str = "BTCUSDT", market: str = "linear", launch: int | None = LAUNCH_BTC) -> InstrumentSpec:
    return InstrumentSpec(
        symbol=symbol, qty_step=0.001, min_order_qty=0.001, min_notional_value=5.0, tick_size=0.1,
        max_leverage=100 if market == "linear" else 1, status="Trading",
        contract_type="LinearPerpetual" if market == "linear" else "Spot",
        market=market, launch_time_ms=launch,
    )


# --------------------------------------------------------------------------- разбор ответа API


def test_linear_item_keeps_its_launch_time() -> None:
    spec = BybitInstrumentsClient._parse_item(LINEAR_ITEM, "linear")

    assert spec.market == "linear"
    assert spec.contract_type == "LinearPerpetual"
    assert spec.launch_time_ms == LAUNCH_BTC
    assert (spec.qty_step, spec.min_notional_value) == (0.001, 5.0)


def test_spot_item_takes_base_precision_and_min_order_amount() -> None:
    spec = BybitInstrumentsClient._parse_item(SPOT_ITEM, "spot")

    assert spec.market == "spot"
    assert spec.contract_type == "Spot"
    assert spec.qty_step == 0.000001  # basePrecision
    assert spec.min_notional_value == 5.0  # minOrderAmt
    assert spec.max_leverage == 1
    assert spec.launch_time_ms is None  # в этой форме ответа launchTime нет


def test_spot_launch_time_is_kept_when_the_api_gives_it() -> None:
    spec = BybitInstrumentsClient._parse_item({**SPOT_ITEM, "launchTime": str(LAUNCH_BTC)}, "spot")

    assert spec.launch_time_ms == LAUNCH_BTC


def test_unknown_market_is_rejected() -> None:
    with pytest.raises(ValueError):
        _spec(market="inverse")


# --------------------------------------------------------------------------- допуск


def test_instrument_is_admitted_only_from_its_launch() -> None:
    cache = InstrumentCache.from_specs([_spec()])

    assert not cache.is_tradable_linear_perpetual("BTC/USDT", at_ms=LAUNCH_BTC - HOUR)
    assert cache.is_tradable_linear_perpetual("BTC/USDT", at_ms=LAUNCH_BTC)
    assert cache.is_tradable_linear_perpetual("BTC/USDT")  # без момента — как раньше


def test_unknown_launch_time_does_not_block() -> None:
    """Старый кэш спецификаций launchTime не хранил: допуск по нему не меняется."""
    cache = InstrumentCache.from_specs([_spec(launch=None)])

    assert cache.is_tradable_linear_perpetual("BTC/USDT", at_ms=0)


def test_markets_are_kept_apart() -> None:
    cache = InstrumentCache.from_specs([_spec(market="spot", launch=None)])

    assert cache.get_spec("BTC/USDT", market="spot") is not None
    assert cache.get_spec("BTC/USDT") is None  # перпетуала в кэше нет
    assert not cache.is_tradable_linear_perpetual("BTC/USDT")
    assert cache.is_tradable("BTC/USDT", market="spot")


# --------------------------------------------------------------------------- снимок


def test_snapshot_round_trip_with_its_hash_and_source(tmp_path) -> None:
    path = tmp_path / "snap.json"
    specs = [_spec(), _spec(market="spot", launch=None)]

    digest = write_snapshot(path, specs, api_base_url="https://api.bybit.com", fetched_at_ms=LAUNCH_BTC)
    cache = InstrumentCache.from_snapshot(path, expected_sha256=digest)

    assert digest == hashlib.sha256(path.read_bytes()).hexdigest()
    assert cache.get_spec("BTC/USDT") == specs[0]
    assert cache.get_spec("BTC/USDT", market="spot") == specs[1]
    assert cache.api_base_url == "https://api.bybit.com"
    assert cache.snapshot_sha256 == digest


def test_snapshot_with_another_hash_is_refused(tmp_path) -> None:
    path = tmp_path / "snap.json"
    write_snapshot(path, [_spec()], api_base_url="https://api.bybit.com", fetched_at_ms=0)

    with pytest.raises(ValueError, match="sha256"):
        InstrumentCache.from_snapshot(path, expected_sha256="0" * 64)


def test_snapshot_is_never_overwritten(tmp_path) -> None:
    path = tmp_path / "snap.json"
    write_snapshot(path, [_spec()], api_base_url="https://api.bybit.com", fetched_at_ms=0)

    with pytest.raises(FileExistsError):
        write_snapshot(path, [_spec()], api_base_url="https://api.bybit.com", fetched_at_ms=1)


def test_legacy_cache_file_loads_as_linear_without_ttl(tmp_path) -> None:
    """Файл живого кэша (как снимок цикла 2 MR) читается как снимок: TTL не применяется."""
    legacy = {"saved_at": 0, "specs": {"BTCUSDT": {
        "symbol": "BTCUSDT", "qty_step": 0.001, "min_order_qty": 0.001, "min_notional_value": 5.0,
        "tick_size": 0.1, "max_leverage": 100, "status": "Trading", "contract_type": "LinearPerpetual",
    }}}
    path = tmp_path / "bybit_instruments_legacy.json"
    path.write_text(json.dumps(legacy), encoding="utf-8")

    cache = InstrumentCache.from_snapshot(path)

    spec = cache.get_spec("BTC/USDT")
    assert spec is not None and spec.market == "linear" and spec.launch_time_ms is None
    assert cache.api_base_url is None  # источник старый файл не записывал
    assert cache.is_tradable_linear_perpetual("BTC/USDT", at_ms=0)


# --------------------------------------------------------------------------- проводка


def test_executor_rejects_an_instrument_before_its_launch(tmp_path) -> None:

    executor = PortfolioExecutor(
        Config(settings=Settings(), env=EnvConfig()), Repositories(Database(tmp_path / "run.db")),
        PnLTracker(), instrument_cache=InstrumentCache.from_specs([_spec()]),
    )
    intent = PositionIntent(symbol="BTC/USDT", side=Side.SHORT, target_weight=0.1, timeframe="1h")

    before = executor.open_position(intent, entry_price=100.0, atr_pct=1.0, timestamp_ms=LAUNCH_BTC - HOUR)
    after = executor.open_position(intent, entry_price=100.0, atr_pct=1.0, timestamp_ms=LAUNCH_BTC)

    assert not before.handled and "not tradable" in before.message
    assert after.handled, after.message


def test_backtester_with_a_snapshot_never_builds_the_live_cache(tmp_path, monkeypatch) -> None:

    async def live_cache(config):
        raise AssertionError("the live instrument cache was built")

    monkeypatch.setattr(backtester_module, "build_instrument_cache", live_cache)
    base = Settings().model_dump()
    base["portfolio"]["strategy_name"] = "cross_sectional_momentum_v0"
    source = HistoricalCandleSource()
    source.load_all("BTC/USDT", "1h", [
        Candle(timestamp=LAUNCH_BTC + i * HOUR, open=100.0, high=100.0, low=100.0, close=100.0, volume=1.0)
        for i in range(6)
    ])
    cache = InstrumentCache.from_specs([_spec()])
    bt = backtester_module.Backtester(
        Config(settings=Settings.model_validate(base), env=EnvConfig()), symbols=["BTC/USDT"],
        timeframes=["1h"], start_ms=LAUNCH_BTC, end_ms=LAUNCH_BTC + 6 * HOUR, source=source,
        db=Database(tmp_path / "bt.db"), strategy_mode=StrategyType.PORTFOLIO, instrument_cache=cache,
    )

    bt.run()

    assert bt._executor._instrument_cache is cache


def test_every_walk_forward_window_gets_the_instrument_cache(monkeypatch) -> None:
    seen = []

    async def capture(*args, **kwargs):
        seen.append(kwargs.get("instrument_cache"))
        return PnLSummary()

    monkeypatch.setattr(walk_forward, "_run_single_window", capture)
    candles = HistoricalCandleSource()
    candles.load_all("BTC/USDT", "1h", [
        Candle(timestamp=LAUNCH_BTC + i * HOUR, open=100.0, high=100.0, low=100.0, close=100.0, volume=1.0)
        for i in range(100)
    ])
    cache = InstrumentCache.from_specs([_spec()])

    walk_forward.run_walk_forward(
        Config(settings=Settings(), env=EnvConfig()), ["BTC/USDT"], "1h", candles, 0.5,
        strategy_mode=StrategyType.PORTFOLIO, three_way=True, validation_ratio=0.2,
        instrument_cache=cache,
    )

    assert seen == [cache, cache, cache]


def test_portfolio_walk_forward_refuses_to_start_without_a_snapshot() -> None:

    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "scripts/walk_forward.py", "--mode", "portfolio", "--symbols", "BTC/USDT"],
        cwd=root, capture_output=True, text=True, encoding="utf-8",
    )

    assert result.returncode == 2
    assert "--instrument-snapshot" in result.stderr
