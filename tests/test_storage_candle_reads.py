"""Чтение свечей из БД: границы выборки и политика лимитов.

Эти проверки закрывают класс дефектов, который прожил незамеченным, потому что
единственные DB-backed тесты в проекте помечены ``cross_validate_live`` и
пропускаются, а все бэктест-тесты грузят синтетику через ``load_all()`` в
обход репозитория.

Что было сломано:
- ``fetch_since`` имел дефолт ``limit=400``, поэтому вызовы ``since_ms=0`` —
  а именно так его зовут ``HistoricalCandleSource.load_all_async`` и
  ``walk_forward`` — получали первые 400 баров вместо всей истории. Бэктест на
  реальных данных читал ~1.7% от 23783 баров и промахивался мимо окна.
- ``fetch`` игнорировал собственный ``since_ms``: в SQL уходил жёсткий ``0``.
- ``fetch`` брал первые ``limit`` баров вместо последних и не применял
  ``policy.MAX_CANDLES_LOOKBACK``.
"""
from __future__ import annotations

import pytest

from crypto_bot.core import policy
from crypto_bot.core.types import Candle
from crypto_bot.simulation.historical_source import HistoricalCandleSource
from crypto_bot.storage.db import Database, Repositories

SYMBOL = "BTC/USDT"
TIMEFRAME = "1h"
PERIOD_MS = 3_600_000
BASE_TS = 1_700_000_000_000
# Заведомо больше прежнего дефолта 400 и больше MAX_CANDLES_LOOKBACK.
N_CANDLES = 1200


def _candles(n: int = N_CANDLES) -> list[Candle]:
    return [
        Candle(
            timestamp=BASE_TS + i * PERIOD_MS,
            open=100.0 + i,
            high=101.0 + i,
            low=99.0 + i,
            close=100.5 + i,
            volume=1000.0,
        )
        for i in range(n)
    ]


@pytest.fixture()
def repos(tmp_path):
    db = Database(tmp_path / "candle_reads.db")
    repositories = Repositories(db)
    repositories.candles.upsert_many(SYMBOL, TIMEFRAME, _candles())
    yield repositories
    db.close()


class TestFetchSinceIsUnbounded:
    def test_returns_every_candle_not_just_the_first_page(self, repos) -> None:
        """Регрессия: дефолт limit=400 молча обрезал историю."""
        got = repos.candles.fetch_since(SYMBOL, TIMEFRAME, since_ms=0)

        assert len(got) == N_CANDLES, (
            f"fetch_since(since_ms=0) вернул {len(got)} из {N_CANDLES} свечей — "
            "путь воспроизведения истории снова ограничен лимитом"
        )
        assert got[0].timestamp == BASE_TS
        assert got[-1].timestamp == BASE_TS + (N_CANDLES - 1) * PERIOD_MS

    def test_honours_since_ms(self, repos) -> None:
        cutoff = BASE_TS + 1000 * PERIOD_MS
        got = repos.candles.fetch_since(SYMBOL, TIMEFRAME, since_ms=cutoff)

        assert len(got) == N_CANDLES - 1000
        assert got[0].timestamp == cutoff

    def test_explicit_limit_still_caps(self, repos) -> None:
        """Лимит остаётся доступным явно — просто он больше не дефолт."""
        got = repos.candles.fetch_since(SYMBOL, TIMEFRAME, since_ms=0, limit=50)
        assert len(got) == 50


class TestFetchIsBoundedByPolicy:
    def test_returns_the_newest_candles_ascending(self, repos) -> None:
        """Фиду нужен свежий хвост истории, а не её начало."""
        got = repos.candles.fetch(SYMBOL, TIMEFRAME, limit=10)

        assert [c.timestamp for c in got] == [
            BASE_TS + i * PERIOD_MS for i in range(N_CANDLES - 10, N_CANDLES)
        ]

    def test_limit_is_clamped_to_policy(self, repos) -> None:
        """MAX_CANDLES_LOOKBACK защищает живой путь чтения и должен применяться."""
        got = repos.candles.fetch(SYMBOL, TIMEFRAME, limit=N_CANDLES * 10)

        assert len(got) == policy.MAX_CANDLES_LOOKBACK

    def test_honours_since_ms(self, repos) -> None:
        """Регрессия: в SQL уходил жёсткий 0 вместо since_ms."""
        cutoff = BASE_TS + (N_CANDLES - 5) * PERIOD_MS
        got = repos.candles.fetch(SYMBOL, TIMEFRAME, since_ms=cutoff, limit=100)

        assert len(got) == 5
        assert got[0].timestamp == cutoff


class TestHistoricalSourceLoadsEverything:
    async def test_load_all_async_loads_the_whole_history(self, repos) -> None:
        """Сквозная проверка того самого пути, которым ходит бэктест."""
        source = HistoricalCandleSource(repos.candles)
        await source.load_all_async(SYMBOL, TIMEFRAME)

        loaded = source.slice_between(0, BASE_TS + N_CANDLES * PERIOD_MS, SYMBOL, TIMEFRAME)
        assert len(loaded) == N_CANDLES, (
            f"load_all_async загрузил {len(loaded)} из {N_CANDLES} — "
            "бэктест на реальных данных снова увидит часть истории"
        )

    async def test_window_far_from_the_start_is_not_empty(self, repos) -> None:
        """Ровно тот симптом: окно за пределами первых 400 баров было пустым."""
        source = HistoricalCandleSource(repos.candles)
        await source.load_all_async(SYMBOL, TIMEFRAME)

        window_start = BASE_TS + 900 * PERIOD_MS
        window_end = BASE_TS + 1100 * PERIOD_MS
        got = source.slice_between(window_start, window_end, SYMBOL, TIMEFRAME)

        assert got, "окно за пределами первой страницы выборки снова пустое"
