"""Sharpe helpers behind PnLSummary.sharpe_ratio and the sub-window sign check.

Condition 4 of the MR cycle 2 decision rule (Sharpe > 0 in at least 2 of 3
sub-windows of the test segment) must use exactly the statistic the rest of the
rule uses, so both go through sharpe_from_returns.
"""
from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest

from crypto_bot.core.enums import Side
from crypto_bot.simulation.paper_position import PaperPosition
from crypto_bot.simulation.pnl import (
    PnLTracker,
    equity_returns,
    sharpe_from_returns,
    subwindow_sharpes,
)

HOUR = 3_600_000
T0 = 1_750_000_000_000 - 1_750_000_000_000 % HOUR


def test_sharpe_formula_units_and_outlier_filter() -> None:
    returns = [0.01, -0.005, 0.02, 0.7]      # 0.7 is dropped as an outlier
    kept = returns[:3]
    mean = sum(kept) / 3
    std = math.sqrt(sum((r - mean) ** 2 for r in kept) / 2)
    assert sharpe_from_returns(returns) == pytest.approx(mean / std * math.sqrt(365))
    assert sharpe_from_returns([0.01]) == 0.0
    assert sharpe_from_returns([0.01, 0.01]) == 0.0      # zero variance


def test_pnl_summary_reports_the_same_statistic() -> None:
    tracker = PnLTracker()
    position = PaperPosition(symbol="BTC/USDT", timeframe="1h", side=Side.LONG, size=1.0,
                             entry_price=100.0, stop_loss=95.0, take_profit=110.0)
    tracker.add_position(position)
    tracker.close_position(position, 105.0)     # get_summary needs a closed position
    for i, equity in enumerate([10_000.0, 10_050.0, 10_020.0, 10_090.0, 10_040.0]):
        tracker.record_equity(datetime.fromtimestamp((T0 + i * HOUR) / 1000, tz=UTC), equity)

    # close_position appended its own record (wall-clock time, realized equity
    # only) ahead of the ticks: the series get_summary sees is not the tick series.
    history = [e for _, e in tracker.equity_history]
    assert history == [10_005.0, 10_000.0, 10_050.0, 10_020.0, 10_090.0, 10_040.0]

    returns = equity_returns(history)
    mean = sum(returns) / len(returns)
    std = math.sqrt(sum((r - mean) ** 2 for r in returns) / (len(returns) - 1))
    by_hand = round(mean / std * math.sqrt(365), 2)
    assert by_hand != 0.0
    assert tracker.get_summary().sharpe_ratio == by_hand
    assert tracker.get_summary().sharpe_ratio == round(sharpe_from_returns(returns), 2)


def test_subwindows_partition_the_returns_by_later_tick() -> None:
    # 9 hourly ticks over a 9-hour window -> parts of 3 hours each.
    # Window 1 rises, window 2 falls, window 3 rises.
    path = [100.0, 101.0, 102.5, 103.0, 102.0, 100.5, 100.0, 101.0, 101.5, 103.0]
    points = [(T0 + i * HOUR, e) for i, e in enumerate(path)]
    sharpes = subwindow_sharpes(points, T0, T0 + 9 * HOUR)

    returns = equity_returns(path)
    # return i (from tick i to tick i+1) belongs to the part holding tick i+1
    by_part: list[list[float]] = [[], [], []]
    for i, r in enumerate(returns):
        tick = (i + 1) * HOUR
        by_part[min(tick // (3 * HOUR), 2) if tick < 9 * HOUR else 2].append(r)
    assert sharpes == [pytest.approx(sharpe_from_returns(p)) for p in by_part]
    assert [s > 0 for s in sharpes] == [True, False, True]


def test_window_end_tick_belongs_to_the_last_part() -> None:
    points = [(T0 + i * HOUR, 100.0 + i) for i in range(7)]   # ticks T0 .. T0+6h
    sharpes = subwindow_sharpes(points, T0, T0 + 6 * HOUR)
    # 6 returns, ticks 1..6: parts [0,2h) -> tick 1; [2h,4h) -> ticks 2,3; [4h,6h] -> 4,5,6
    assert sharpes[0] == 0.0                     # a single return: no Sharpe
    assert sharpes[1] > 0 and sharpes[2] > 0


def test_invalid_window_rejected() -> None:
    with pytest.raises(ValueError):
        subwindow_sharpes([(T0, 1.0)], T0, T0)
