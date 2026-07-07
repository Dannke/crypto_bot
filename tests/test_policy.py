"""Tests for core/policy.py."""
from __future__ import annotations

from crypto_bot.core.policy import is_bar_closed, timeframe_to_seconds


class TestIsBarClosed:
    """is_bar_closed must return False for the current (still forming) bar
    and True for every fully closed bar."""

    def test_returns_true_for_fully_closed_bar(self) -> None:
        # 1h bar that opened 7200s ago and we are at +3600s
        open_ts = 1000000
        tf = "1h"
        as_of = open_ts + timeframe_to_seconds(tf) * 1000  # exactly at close
        assert is_bar_closed(open_ts, tf, as_of) is True

    def test_returns_false_for_still_open_bar(self) -> None:
        open_ts = 1000000
        tf = "1h"
        as_of = open_ts + (timeframe_to_seconds(tf) * 1000) - 1  # 1ms before close
        assert is_bar_closed(open_ts, tf, as_of) is False

    def test_returns_true_for_bar_long_in_the_past(self) -> None:
        open_ts = 1000000
        tf = "1h"
        as_of = open_ts + 10 * timeframe_to_seconds(tf) * 1000  # way past
        assert is_bar_closed(open_ts, tf, as_of) is True

    def test_returns_false_if_as_of_is_open_ts(self) -> None:
        open_ts = 2000000
        tf = "5m"
        as_of = open_ts
        assert is_bar_closed(open_ts, tf, as_of) is False

    def test_mixed_bars(self) -> None:
        """Only the last (open) bar should be rejected."""
        tf = "1h"
        period = timeframe_to_seconds(tf) * 1000  # 3_600_000 ms
        # 5 consecutive 1h bars
        ts = [i * period for i in range(5)]  # 0, 3_600_000, 7_200_000, 10_800_000, 14_400_000
        as_of = 8_000_000  # ms — bar 0 and 1 are closed, bar 2 is still forming (7.2M..10.8M)
        closed = [t for t in ts if is_bar_closed(t, tf, as_of)]
        assert closed == ts[:2], f"expected 2 closed bars, got {len(closed)}"
        open_bars = [t for t in ts if not is_bar_closed(t, tf, as_of)]
        assert open_bars == ts[2:], f"expected 3 open bars, got {len(open_bars)}"
