"""Verdict metrics of the funding/basis cycle.

The definitions are fixed before any measurement in section 8 of
docs/research/funding-basis/cycle-1/1-hypothesis-and-decision-rule.md (and its
amendment 1); this module only computes them from a run database:

- **Daily grid** — the initial capital at the segment start, then the equity
  recorded on the tick at ``start + k`` days, ``k = 1..D``: the 00:00 UTC tick,
  whose funding is credited before the equity is recorded (step 0 of a tick).
  The grid's step comes from the timestamps of the equity table; a grid that
  is not uniform raises :class:`GridError` instead of being annualized anyway.
- **R_ann** = ``(E_end / E_start) ** (365 / D) − 1``, ``D`` the segment length
  in days, 365 a year (crypto trades every day).
- **MaxDD** — the largest fall from a running peak of the mark-to-market
  equity of the equity table, starting from the initial capital.
- **HAC t-statistic** of the mean simple daily return: Newey–West with
  Bartlett weights ``1 − l / (L + 1)``, lag ``L = floor(4 (T / 100) ** (2/9))``,
  no small-sample correction — statsmodels' default for ``cov_type='HAC'``.
- **Sub-window returns** — the daily grid split into equal calendar parts.
- **Open days** — grid instants ``t_k``, ``k = 1..D``, at which a position is
  held: ``opened_at < t_k <= closed_at``, the boundary rule of funding accrual.

Costs, the margin buffer and the USDT balance (conditions C3 and C4) are not
in a run database yet; they come with the per-market costs and the capital
model. ``pnl.sharpe_from_returns`` and ``pnl.subwindow_sharpes`` stay as they
are: the CSM and MR verdicts were computed with them.
"""
from __future__ import annotations

import math
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

MS_PER_HOUR = 3_600_000
MS_PER_DAY = 86_400_000
DAYS_PER_YEAR = 365

EquityPoint = tuple[int, float]


class GridError(ValueError):
    """Equity points do not form the uniform grid a metric is defined on."""


def _utc(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M")


def grid_step_ms(timestamps: Sequence[int]) -> int:
    """The common step of strictly increasing timestamps."""
    if len(timestamps) < 2:
        raise GridError("a step needs at least two points")
    steps = {later - earlier for earlier, later in zip(timestamps, timestamps[1:], strict=False)}
    if len(steps) != 1 or min(steps) <= 0:
        raise GridError(f"timestamps are not a uniform increasing grid: steps {sorted(steps)[:5]} ms")
    return steps.pop()


def segment_days(start_ms: int, end_ms: int) -> int:
    """Length of the segment in whole days."""
    span = end_ms - start_ms
    if span <= 0 or span % MS_PER_DAY:
        raise GridError(f"segment {_utc(start_ms)} .. {_utc(end_ms)} is not a whole number of days")
    return span // MS_PER_DAY


def mtm_points(points: Iterable[EquityPoint], start_ms: int, end_ms: int) -> tuple[list[EquityPoint], int]:
    """Equity points of the segment and their step, checked to run from start to end."""
    segment = sorted((ts, equity) for ts, equity in points if start_ms <= ts <= end_ms)
    if not segment or segment[0][0] != start_ms or segment[-1][0] != end_ms:
        raise GridError(f"equity points do not cover {_utc(start_ms)} .. {_utc(end_ms)}")
    return segment, grid_step_ms([ts for ts, _ in segment])


def daily_grid(
    points: Iterable[EquityPoint], start_ms: int, end_ms: int, initial_equity: float,
) -> list[EquityPoint]:
    """The initial capital at ``start_ms``, then the equity at every following 00:00 point."""
    days = segment_days(start_ms, end_ms)
    segment, step = mtm_points(points, start_ms, end_ms)
    if MS_PER_DAY % step:
        raise GridError(f"a step of {step} ms does not divide a day")
    stride = MS_PER_DAY // step
    return [(start_ms, initial_equity)] + [segment[k * stride] for k in range(1, days + 1)]


def annualized_return(start_equity: float, end_equity: float, days: float) -> float:
    """``(E_end / E_start) ** (365 / days) − 1``."""
    if start_equity <= 0 or days <= 0:
        raise ValueError("annualizing needs positive start equity and days")
    return math.pow(end_equity / start_equity, DAYS_PER_YEAR / days) - 1.0


def max_drawdown(equities: Sequence[float]) -> float:
    """Largest fall from a running peak, as a fraction of that peak."""
    if not equities:
        raise ValueError("no equity points")
    peak, worst = equities[0], 0.0
    for equity in equities:
        peak = max(peak, equity)
        worst = max(worst, (peak - equity) / peak)
    return worst


def simple_returns(values: Sequence[float]) -> list[float]:
    """``v_k / v_{k−1} − 1`` between consecutive values."""
    return [later / earlier - 1.0 for earlier, later in zip(values, values[1:], strict=False)]


def newey_west_lag(n: int) -> int:
    """``floor(4 (n / 100) ** (2/9))`` — the lag rule of the registration."""
    return math.floor(4 * math.pow(n / 100, 2 / 9))


def hac_t_stat(returns: Sequence[float], lags: int) -> float:
    """t-statistic of the mean with a Newey–West (Bartlett) long-run variance."""
    n = len(returns)
    if n < 2 or lags < 0:
        raise ValueError("HAC needs at least two returns and a non-negative lag")
    mean = sum(returns) / n
    resid = [r - mean for r in returns]
    long_run = sum(u * u for u in resid)
    for lag in range(1, lags + 1):
        weight = 1.0 - lag / (lags + 1)
        long_run += 2.0 * weight * sum(resid[t] * resid[t - lag] for t in range(lag, n))
    variance_of_mean = long_run / (n * n)
    if variance_of_mean <= 0:
        raise ValueError("the long-run variance is not positive")
    return mean / math.sqrt(variance_of_mean)


def subwindow_returns(grid: Sequence[EquityPoint], parts: int) -> list[float]:
    """Return of each of ``parts`` equal calendar parts of a daily grid."""
    days = len(grid) - 1
    if parts < 1 or days < parts or days % parts:
        raise GridError(f"{days} days do not split into {parts} equal parts")
    size = days // parts
    return [grid[(j + 1) * size][1] / grid[j * size][1] - 1.0 for j in range(parts)]


def open_days(held: Iterable[tuple[int, int | None]], instants: Sequence[int]) -> int:
    """Instants at which at least one ``(opened_at, closed_at)`` position is held."""
    intervals = list(held)
    return sum(
        any(opened < t and (closed is None or t <= closed) for opened, closed in intervals)
        for t in instants
    )


@dataclass(frozen=True, slots=True)
class SegmentMetrics:
    """Section 8 quantities of one segment that a run database holds."""

    start_ms: int
    end_ms: int
    days: int
    initial_equity: float
    final_equity: float
    r_ann: float
    max_drawdown: float  # fraction of the running peak
    mtm_step_ms: int  # step of the equity points the drawdown is taken on
    daily_returns: tuple[float, ...]
    hac_lag: int
    hac_t: float
    subwindow_returns: tuple[float, ...]
    open_days: int
    funding_received: float  # USDT credited by settlements in (start, end]; negative = paid


def segment_metrics(
    db_path: str | Path, start_ms: int, end_ms: int, initial_equity: float, *, subwindows: int = 3,
) -> SegmentMetrics:
    """Every section 8 quantity a run database holds, for the segment ``[start_ms, end_ms]``.

    The database is opened read-only: opening it through ``Database`` would
    migrate it.
    """
    conn = sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True)
    try:
        points = conn.execute("SELECT ts_ms, equity FROM equity ORDER BY ts_ms, id").fetchall()
        held = conn.execute("SELECT opened_at_ms, closed_at_ms FROM positions").fetchall()
        paid = conn.execute(
            "SELECT COALESCE(SUM(amount), 0.0) FROM funding_payments "
            "WHERE funding_time_ms > ? AND funding_time_ms <= ?",
            (start_ms, end_ms),
        ).fetchone()[0]
    finally:
        conn.close()

    segment, step = mtm_points(points, start_ms, end_ms)
    grid = daily_grid(segment, start_ms, end_ms, initial_equity)
    days = len(grid) - 1
    returns = simple_returns([equity for _, equity in grid])
    lag = newey_west_lag(days)
    return SegmentMetrics(
        start_ms=start_ms,
        end_ms=end_ms,
        days=days,
        initial_equity=initial_equity,
        final_equity=grid[-1][1],
        r_ann=annualized_return(initial_equity, grid[-1][1], days),
        max_drawdown=max_drawdown([initial_equity] + [equity for _, equity in segment]),
        mtm_step_ms=step,
        daily_returns=tuple(returns),
        hac_lag=lag,
        hac_t=hac_t_stat(returns, lag),
        subwindow_returns=tuple(subwindow_returns(grid, subwindows)),
        open_days=open_days(held, [ts for ts, _ in grid[1:]]),
        funding_received=-float(paid),  # the journal holds the payment by the position
    )
