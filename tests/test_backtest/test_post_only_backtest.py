"""Post-only execution must actually reach the backtest loop.

``open_position`` returns early for post-only: it registers a pending limit
order and leaves the position unopened.  Something has to feed later bars back
in so the order can fill.  For a long time nothing did — the methods existed
but were called only from unit tests, so a mean-reversion backtest placed
orders and produced zero trades while still reporting ``handled=True``.

These tests pin the wiring itself: a portfolio backtest running
``mean_reversion_v0`` with ``entry_execution=post_only`` must open positions,
and those positions must be recorded as LIMIT (maker) orders rather than
market ones.
"""
from __future__ import annotations

import math
import sqlite3

import pytest

from crypto_bot.config.env import Config
from crypto_bot.config.schemas import RegimeConfig
from crypto_bot.core.enums import StrategyType
from crypto_bot.core.types import Candle
from crypto_bot.simulation.backtester import Backtester
from crypto_bot.simulation.historical_source import HistoricalCandleSource
from crypto_bot.storage.db import Database

from ._shared import golden_config

# Полный бэктест на 300 барах × 5 символов (~30 с): warmup z-score (48 баров),
# режима и ребаланс раз в 12 ч требуют горизонта — на 240 барах сделок нет
# вообще, включая market-контроль. Держим вне быстрого цикла, но в полном
# регрессионном прогоне, который обязателен перед любым walk-forward.
pytestmark = pytest.mark.slow

PERIOD_MS = 3_600_000
BASE_TS = 1_700_000_000_000
N_BARS = 300
CYCLE_BARS = 40.0


def _oscillating(phase: float, amplitude: float) -> list[Candle]:
    """Mean-reverting series: a clean sine so z-scores cross the entry threshold."""
    out: list[Candle] = []
    for i in range(N_BARS):
        close_p = 100.0 * (1.0 + amplitude * math.sin(2 * math.pi * (i / CYCLE_BARS) + phase))
        open_p = 100.0 * (1.0 + amplitude * math.sin(2 * math.pi * ((i - 1) / CYCLE_BARS) + phase))
        out.append(Candle(
            timestamp=BASE_TS + i * PERIOD_MS,
            open=open_p,
            high=max(open_p, close_p) * 1.002,
            low=min(open_p, close_p) * 0.998,
            close=close_p,
            volume=1000.0,
        ))
    return out


def _universe() -> dict[str, list[Candle]]:
    """Five phase-shifted oscillators: at any tick some are stretched, some are not."""
    return {
        "A/USDT": _oscillating(0.0, 0.06),
        "B/USDT": _oscillating(1.2, 0.05),
        "C/USDT": _oscillating(2.4, 0.04),
        "D/USDT": _oscillating(3.6, 0.05),
        "E/USDT": _oscillating(4.8, 0.06),
    }


# Backtester НЕ читает settings.regime — конфиг режима доезжает до пайплайна
# только через явный аргумент regime_config=. Без него подставляются хардкод-
# дефолты (trend_period=14, vol_lookback_bars=168 -> нужно 182 бара), и на
# синтетике в 300 баров пайплайн падает на каждом ребалансе.
TEST_REGIME = RegimeConfig(
    enabled=True,
    reference="universe_basket",
    trend_period=5,
    trend_threshold=0.1,
    vol_lookback_bars=10,
    vol_percentile_high=0.75,
)


def _mr_config(entry_execution: str, exit_execution: str) -> Config:
    """golden config switched to mean_reversion_v0 with the given execution mode."""
    config = golden_config()
    mean_reversion = config.settings.portfolio.mean_reversion.model_copy(
        update={"entry_execution": entry_execution, "exit_execution": exit_execution}
    )
    portfolio = config.settings.portfolio.model_copy(
        update={"strategy_name": "mean_reversion_v0", "mean_reversion": mean_reversion}
    )
    settings = config.settings.model_copy(update={"portfolio": portfolio, "regime": TEST_REGIME})
    return Config(settings=settings, env=config.env)


def _run(tmp_path, config: Config, name: str):
    universe = _universe()
    source = HistoricalCandleSource()
    for symbol, candles in universe.items():
        source.load_all(symbol, "1h", candles)
    db = Database(tmp_path / f"{name}.db")
    backtester = Backtester(
        config,
        symbols=list(universe),
        timeframes=["1h"],
        start_ms=BASE_TS,
        end_ms=BASE_TS + N_BARS * PERIOD_MS,
        source=source,
        db=db,
        strategy_mode=StrategyType.PORTFOLIO,
        regime_config=TEST_REGIME,
    )
    summary = backtester.run()
    conn = sqlite3.connect(db.db_path)
    conn.row_factory = sqlite3.Row
    return summary, conn


class TestPostOnlyReachesTheBacktest:
    def test_post_only_mean_reversion_opens_positions(self, tmp_path) -> None:
        """The regression guard: post-only must not silently produce zero trades."""
        _, conn = _run(tmp_path, _mr_config("post_only", "post_only"), "post_only")

        # Считаем ОТКРЫТЫЕ позиции, а не закрытые сделки: guard про то, что
        # pending-заявки доезжают до позиции. Закрытие — отдельный механизм, и
        # его отсутствие не должно выглядеть как несработавший wiring.
        opened = conn.execute("SELECT COUNT(*) AS c FROM positions").fetchone()
        assert int(opened["c"]) > 0, (
            "mean_reversion_v0 with entry_execution=post_only opened no positions — "
            "pending limit orders are never being processed by the bar loop"
        )

    def test_post_only_entries_are_recorded_as_limit_orders(self, tmp_path) -> None:
        """A filled post-only entry is a maker fill, so it must be a LIMIT trade."""
        _, conn = _run(tmp_path, _mr_config("post_only", "post_only"), "post_only_kind")

        order_types = {
            row["order_type"]
            for row in conn.execute("SELECT DISTINCT order_type FROM trades").fetchall()
        }
        assert order_types, "no trades recorded at all"
        assert order_types == {"limit"}, (
            f"post-only entries must fill as limit orders, got {sorted(order_types)}"
        )

    def test_market_execution_still_works(self, tmp_path) -> None:
        """The market path is the control: it never depended on the pending queue."""
        _, conn = _run(tmp_path, _mr_config("market", "market"), "market")

        opened = conn.execute("SELECT COUNT(*) AS c FROM positions").fetchone()
        assert int(opened["c"]) > 0
        order_types = {
            row["order_type"]
            for row in conn.execute("SELECT DISTINCT order_type FROM trades").fetchall()
        }
        assert order_types == {"market"}
