"""Метрики вердикта funding/basis — раздел 8 документа Task 0'.

docs/research/funding-basis/cycle-1/1-hypothesis-and-decision-rule.md фиксирует их до
измерений: дневная сетка эквити, R_ann, MaxDD по часовой MTM, HAC-t (Newey–West,
Бартлетт), доходности под-окон, сутки с открытой позицией. Ответы — синтетика с
известным результатом; HAC-t сверяется с эталоном statsmodels.
"""
from __future__ import annotations

import math
import statistics

import pytest

from crypto_bot.core.enums import Mode, Side, TradeStatus
from crypto_bot.simulation import metrics
from crypto_bot.simulation.metrics import GridError
from crypto_bot.storage.db import Database, Repositories

HOUR = metrics.MS_PER_HOUR
DAY = metrics.MS_PER_DAY
T0 = 1_735_689_600_000  # 2025-01-01 00:00 UTC
INSERT_PAYMENT = "INSERT INTO funding_payments (position_id, funding_time_ms, amount) VALUES (?, ?, ?)"


def _hourly(values: list[float], start: int = T0) -> list[tuple[int, float]]:
    return [(start + i * HOUR, value) for i, value in enumerate(values)]


def _autocorrelated_sample(n: int = 40) -> list[float]:
    """Детерминированная выборка с положительной автокорреляцией — та же, что в эталоне."""
    ys, prev = [], 0.0
    for t in range(1, n + 1):
        y = 0.0004 + 0.5 * (prev - 0.0004) + 0.001 * math.sin(1.7 * t) + 0.0005 * math.cos(0.3 * t)
        ys.append(y)
        prev = y
    return ys


# --------------------------------------------------------------------------- сетка


def test_grid_step_is_derived_from_timestamps() -> None:
    assert metrics.grid_step_ms([T0, T0 + HOUR, T0 + 2 * HOUR]) == HOUR
    assert metrics.grid_step_ms([T0, T0 + 900_000, T0 + 1_800_000]) == 900_000


@pytest.mark.parametrize(
    "timestamps",
    [
        [T0, T0 + HOUR, T0 + 3 * HOUR],  # пропуск
        [T0, T0 + HOUR, T0 + HOUR],      # дубль
        [T0 + HOUR, T0],                 # обратный порядок
        [T0],                            # шаг не из чего вывести
    ],
)
def test_non_uniform_grid_is_an_error(timestamps: list[int]) -> None:
    with pytest.raises(GridError):
        metrics.grid_step_ms(timestamps)


def test_daily_grid_starts_from_initial_capital_and_takes_00_utc_points() -> None:
    values = [10_000.0 + i for i in range(49)]
    values[0] = 9_990.0  # эквити первого тика — не начальный капитал: в сетку не попадает

    grid = metrics.daily_grid(_hourly(values), T0, T0 + 2 * DAY, initial_equity=10_000.0)

    assert grid == [(T0, 10_000.0), (T0 + DAY, 10_024.0), (T0 + 2 * DAY, 10_048.0)]


@pytest.mark.parametrize(
    ("points", "end"),
    [
        (_hourly([10_000.0] * 49)[:30] + _hourly([10_000.0] * 49)[31:], T0 + 2 * DAY),  # нет часа
        (_hourly([10_000.0] * 48), T0 + 2 * DAY),                                       # нет конца
        (_hourly([10_000.0] * 49), T0 + 2 * DAY - HOUR),                                # не целые сутки
    ],
)
def test_daily_grid_with_a_gap_is_an_error(points: list[tuple[int, float]], end: int) -> None:
    with pytest.raises(GridError):
        metrics.daily_grid(points, T0, end, initial_equity=10_000.0)


# --------------------------------------------------------------------------- доходность и просадка


def test_annualized_return_known_answers() -> None:
    assert metrics.annualized_return(10_000.0, 10_800.0, 365) == pytest.approx(0.08, abs=1e-12)
    # 1.04 за полгода: 1.04 ** 2 − 1
    assert metrics.annualized_return(10_000.0, 10_400.0, 182.5) == pytest.approx(0.0816, abs=1e-12)


def test_max_drawdown_known_answer() -> None:
    # пик 11 000 → 9 900: 10%; пик 12 000 → 11 400: 5%
    assert metrics.max_drawdown([10_000.0, 11_000.0, 9_900.0, 10_500.0, 12_000.0, 11_400.0]) == pytest.approx(0.10)
    # первая точка — начальный капитал, просадка от неё тоже считается
    assert metrics.max_drawdown([10_000.0, 9_500.0]) == pytest.approx(0.05)


# --------------------------------------------------------------------------- HAC-t


def test_newey_west_lag_matches_the_registration() -> None:
    # приложение A Task 0': floor(4 × (297 / 100)^(2/9)) = 5
    assert metrics.newey_west_lag(297) == 5
    assert metrics.newey_west_lag(198) == 4


@pytest.mark.parametrize(
    ("lags", "reference"),
    [
        (0, 2.499605968957595),
        (1, 2.0977149080360658),
        (3, 1.8655973695718742),
        (5, 1.6302805286586615),
    ],
)
def test_hac_t_matches_statsmodels(lags: int, reference: float) -> None:
    """Эталон — statsmodels 0.14.5, numpy 2.2.6, 2026-10-05:

        x = np.array(_autocorrelated_sample())
        sm.OLS(x, np.ones_like(x)).fit(cov_type="HAC", cov_kwds={"maxlags": lags}).tvalues[0]

    По умолчанию statsmodels для HAC поправку на малую выборку не применяет:
    значения совпали с use_correction=False на всех четырёх лагах.
    """
    assert metrics.hac_t_stat(_autocorrelated_sample(), lags) == pytest.approx(reference, rel=1e-12)


def test_hac_t_without_lags_is_the_t_on_population_variance() -> None:
    sample = _autocorrelated_sample()
    plain_t = statistics.fmean(sample) / (statistics.pstdev(sample) / math.sqrt(len(sample)))

    assert metrics.hac_t_stat(sample, 0) == pytest.approx(plain_t, rel=1e-12)


# --------------------------------------------------------------------------- под-окна и открытые сутки


def test_subwindow_returns_split_the_daily_grid_into_equal_parts() -> None:
    grid = [(T0 + d * DAY, value) for d, value in enumerate([100.0, 101.0, 102.0, 99.0, 98.0, 98.0, 103.0])]

    returns = metrics.subwindow_returns(grid, 3)

    assert returns == pytest.approx([0.02, 98.0 / 102.0 - 1, 103.0 / 98.0 - 1])


def test_subwindows_that_do_not_divide_the_segment_are_an_error() -> None:
    grid = [(T0 + d * DAY, 100.0) for d in range(8)]  # 7 суток
    with pytest.raises(GridError):
        metrics.subwindow_returns(grid, 3)


def test_open_days_use_the_funding_boundary() -> None:
    instants = [T0 + k * DAY for k in range(1, 5)]
    held = [
        (T0 + DAY, T0 + DAY + HOUR),  # открыта ровно в t1 — в t1 не держится
        (T0 + DAY + HOUR, T0 + 3 * DAY),  # закрыта ровно в t3 — в t2 и t3 держится
    ]

    assert metrics.open_days(held, instants) == 2
    assert metrics.open_days([(T0 + 3 * DAY + HOUR, None)], instants) == 1  # открыта до конца


# --------------------------------------------------------------------------- БД прогона


def test_segment_metrics_from_a_run_database(tmp_path) -> None:
    db = Database(tmp_path / "run.db")
    repos = Repositories(db)
    # 3 суток часовой эквити: рост на 10 USDT в час, провал −300 в середине вторых суток
    values = [10_000.0 + 10.0 * i for i in range(73)]
    values[36] -= 300.0
    for ts, equity in _hourly(values):
        repos.equity.insert(currency="USDT", equity=equity, drawdown_pct=0.0, mode=Mode.PAPER, ts_ms=ts)
    first = repos.positions.insert(
        symbol="BTC/USDT", timeframe="1h", side=Side.SHORT, size=1.0, entry_price=100.0,
        stop=110.0, take=90.0, mode=Mode.PAPER, opened_at_ms=T0 + HOUR, status=TradeStatus.OPEN,
    )
    db.conn.execute("UPDATE positions SET status='closed', closed_at_ms=? WHERE id=?", (T0 + 2 * DAY, first))
    repos.positions.insert(
        symbol="ETH/USDT", timeframe="1h", side=Side.SHORT, size=1.0, entry_price=100.0,
        stop=110.0, take=90.0, mode=Mode.PAPER, opened_at_ms=T0 + 2 * DAY + HOUR, status=TradeStatus.OPEN,
    )
    db.conn.executemany(INSERT_PAYMENT, [
        (first, T0, 9.9),              # τ = начало сегмента — не в (start, end]
        (first, T0 + 8 * HOUR, -0.5),  # получено 0.5
        (first, T0 + 16 * HOUR, 0.2),  # заплачено 0.2
    ])
    db.conn.commit()
    db.close()

    result = metrics.segment_metrics(tmp_path / "run.db", T0, T0 + 3 * DAY, initial_equity=10_000.0)

    assert result.days == 3
    assert result.final_equity == 10_720.0
    assert result.mtm_step_ms == HOUR
    assert result.r_ann == pytest.approx((10_720.0 / 10_000.0) ** (365 / 3) - 1)
    # пик 10 350 (час 35) → 10 060 (час 36)
    assert result.max_drawdown == pytest.approx(290.0 / 10_350.0)
    assert result.daily_returns == pytest.approx((0.024, 10_480.0 / 10_240.0 - 1, 10_720.0 / 10_480.0 - 1))
    assert result.hac_lag == metrics.newey_west_lag(3)
    assert result.hac_t == pytest.approx(metrics.hac_t_stat(list(result.daily_returns), result.hac_lag))
    assert result.subwindow_returns == pytest.approx(result.daily_returns)
    assert result.open_days == 3  # t1 и t2 — первая позиция (закрыта ровно в t2), t3 — вторая
    assert result.funding_received == pytest.approx(0.3)
