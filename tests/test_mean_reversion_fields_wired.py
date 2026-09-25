"""Каждое поле MeanReversionConfig должно менять поведение реального прогона.

Класс дефектов, ради которого написан файл: поле объявлено в конфиге, проходит
валидацию, отдаётся через property стратегии — и не влияет ни на что, потому
что реальный потребитель читает другое место. Найдено дважды на разных полях,
поэтому проверяется системно, а не по факту следующей находки.

Метод: выставить заведомо экстремальное, легко отличимое значение и убедиться,
что прогон ведёт себя соответственно. Проверяется ПОВЕДЕНИЕ бэктеста, а не
то, что значение доехало до объекта стратегии: `rebalance_hours` доезжает до
стратегии корректно и всё равно ни на что не влияет.
"""
from __future__ import annotations

import math
import sqlite3

import pytest
from test_backtest._shared import golden_config

from crypto_bot.config.env import Config
from crypto_bot.config.schemas import RegimeConfig
from crypto_bot.core.enums import StrategyType
from crypto_bot.core.types import Candle
from crypto_bot.simulation.backtester import Backtester
from crypto_bot.simulation.historical_source import HistoricalCandleSource
from crypto_bot.storage.db import Database

pytestmark = pytest.mark.slow

PERIOD_MS = 3_600_000
BASE_TS = 1_700_000_000_000
N_BARS = 300
CYCLE_BARS = 40.0

TEST_REGIME = RegimeConfig(
    enabled=True,
    reference="universe_basket",
    trend_period=5,
    trend_threshold=0.1,
    vol_lookback_bars=10,
    vol_percentile_high=0.75,
)


def _oscillating(phase: float, amplitude: float) -> list[Candle]:
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
    return {
        "A/USDT": _oscillating(0.0, 0.06),
        "B/USDT": _oscillating(1.2, 0.05),
        "C/USDT": _oscillating(2.4, 0.04),
        "D/USDT": _oscillating(3.6, 0.05),
        "E/USDT": _oscillating(4.8, 0.06),
    }


def _config(**mr_overrides) -> Config:
    """golden config на mean_reversion_v0 с переопределением полей MR."""
    base = golden_config()
    mean_reversion = base.settings.portfolio.mean_reversion.model_copy(update=mr_overrides)
    portfolio = base.settings.portfolio.model_copy(
        update={"strategy_name": "mean_reversion_v0", "mean_reversion": mean_reversion}
    )
    settings = base.settings.model_copy(update={"portfolio": portfolio, "regime": TEST_REGIME})
    return Config(settings=settings, env=base.env)


def _run(tmp_path, config: Config, name: str) -> sqlite3.Connection:
    universe = _universe()
    source = HistoricalCandleSource()
    for symbol, candles in universe.items():
        source.load_all(symbol, "1h", candles)
    db = Database(tmp_path / f"{name}.db")
    Backtester(
        config,
        symbols=list(universe),
        timeframes=["1h"],
        start_ms=BASE_TS,
        end_ms=BASE_TS + N_BARS * PERIOD_MS,
        source=source,
        db=db,
        strategy_mode=StrategyType.PORTFOLIO,
        regime_config=TEST_REGIME,
    ).run()
    conn = sqlite3.connect(db.db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _positions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT symbol, opened_at_ms, closed_at_ms, status FROM positions ORDER BY opened_at_ms"
    ).fetchall()


def _entry_instants(conn: sqlite3.Connection) -> list[int]:
    return sorted({int(r["opened_at_ms"]) for r in _positions(conn)})


def _peak_concurrent(conn: sqlite3.Connection) -> int:
    """Максимум одновременно открытых позиций за прогон."""
    events: list[tuple[int, int]] = []
    for row in _positions(conn):
        events.append((int(row["opened_at_ms"]), +1))
        if row["closed_at_ms"] is not None:
            events.append((int(row["closed_at_ms"]), -1))
    concurrent = peak = 0
    for _ts, delta in sorted(events):
        concurrent += delta
        peak = max(peak, concurrent)
    return peak


class TestFieldsThatAreWired:
    def test_entry_threshold_blocks_entries_when_unreachable(self, tmp_path) -> None:
        """Порог входа выше любого достижимого |z| — позиций быть не должно."""
        permissive = _run(tmp_path, _config(entry_threshold=1.5), "thr_low")
        blocked = _run(tmp_path, _config(entry_threshold=99.0), "thr_high")

        assert len(_positions(permissive)) > 0, "фикстура не порождает сигналов вовсе"
        assert len(_positions(blocked)) == 0, (
            "entry_threshold=99 не остановил входы — порог не доезжает до отбора"
        )

    def test_exit_threshold_changes_the_book(self, tmp_path) -> None:
        """Регрессия на потерю `closes` в fusion (portfolio_fusion.py).

        Разрыв был в пересборке PortfolioIntent без поля `closes`: выходы
        стратегии не доезжали до книги, и поведение не зависело от
        exit_threshold вовсе.
        """
        tight = _run(tmp_path, _config(entry_threshold=3.0, exit_threshold=0.01), "exit_tight")
        loose = _run(tmp_path, _config(entry_threshold=3.0, exit_threshold=2.5), "exit_loose")

        as_tuples = lambda conn: [tuple(r) for r in _positions(conn)]  # noqa: E731
        assert as_tuples(tight) != as_tuples(loose), "exit_threshold не влияет на прогон"

    def test_strategy_exit_reasons_reach_the_book(self, tmp_path) -> None:
        """Выход стратегии должен закрываться своей причиной, не `rebalance`.

        Это наблюдаемая сторона того же разрыва: пока `closes` терялся в
        fusion, закрытия доходили до книги обезличенными, и атрибуция
        выходов (`positions.closed_by`) была непригодна для статистики.
        """
        conn = _run(tmp_path, _config(entry_threshold=1.5, exit_threshold=0.5), "attribution")
        reasons = {
            row["closed_by"]
            for row in conn.execute(
                "SELECT DISTINCT closed_by FROM positions WHERE closed_by IS NOT NULL"
            ).fetchall()
        }

        assert reasons, "ни одна позиция не закрыта — фикстура не порождает выходов"
        assert reasons & {"reversion", "time_stop"}, (
            f"все закрытия обезличены, причины стратегии не доезжают до книги: {reasons}"
        )

    def test_rebalance_hours_changes_decision_cadence(self, tmp_path) -> None:
        """Регрессия на чтение чужой каденции (factory.resolve_rebalance_hours).

        Разрыв был в том, что бэктестер и живой оркестратор читали
        `csm.rebalance_hours` независимо от активной стратегии, поэтому
        mean_reversion всегда ребалансировался с каденцией CSM.
        """
        fast = _run(tmp_path, _config(rebalance_hours=2), "rb_fast")
        slow = _run(tmp_path, _config(rebalance_hours=48), "rb_slow")

        assert len(_entry_instants(fast)) > len(_entry_instants(slow)), (
            "каденция ребаланса не зависит от mean_reversion.rebalance_hours"
        )


class TestFieldsThatAreNotWired:
    """Подтверждённые разрывы проводки. strict=True — если починят, тест упадёт."""

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "варьируется ТОЛЬКО mean_reversion.max_positions; "
            "risk.max_open_positions (5) и risk.max_positions (5) зафиксированы. "
            "Пик одновременно открытых одинаков при 1 и 5, то есть поле стратегии "
            "декоративно — в evaluate_market оно не читается. Риск-лимит этим НЕ "
            "затронут: он живёт в risk.* и покрыт отдельно "
            "(test_portfolio_constraints.py::test_max_open_positions_caps_the_canonical_book)"
        ),
    )
    def test_max_positions_limits_concurrent_book(self, tmp_path) -> None:
        narrow = _peak_concurrent(_run(tmp_path, _config(max_positions=1), "mp_one"))
        wide = _peak_concurrent(_run(tmp_path, _config(max_positions=5), "mp_five"))

        assert wide > narrow, f"пик одинаков при max_positions=1 и 5 (оба {narrow})"
