"""Simulation clock for replay-based backtesting.

Advances a cursor over an ordered sequence of bar-close timestamps so the
replay loop sees a monotonically increasing ``as_of_ms`` on each iteration.
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field


@dataclass
class BacktestClock:
    """Steps through historical bar-close timestamps in order.

    Each ``as_of_ms`` represents the moment *after* a bar has fully closed,
    which is when the live scan loop would normally run its feature/decision
    computation.

    Usage::

        clock = BacktestClock.from_candle_timestamps([1000, 2000, 3000])
        for as_of in clock:
            print(as_of)   # 2000, 3000  (skip the first — no prior bar yet)
    """

    timestamps: list[int] = field(default_factory=list)
    index: int = 0

    @property
    def as_of_ms(self) -> int:
        """Current simulation time (ms epoch)."""
        return self.timestamps[self.index]

    def advance(self) -> bool:
        """Move to the next timestamp.  Returns ``False`` when exhausted."""
        self.index += 1
        return self.index < len(self.timestamps)

    def __iter__(self) -> Iterator[int]:
        """Iterate over all timestamps, skipping the first."""
        self.index = 0
        while self.index < len(self.timestamps):
            yield self.timestamps[self.index]
            if not self.advance():
                break

    @classmethod
    def from_candle_timestamps(cls, timestamps: list[int]) -> BacktestClock:
        """Build from a sorted list of candle-open timestamps.

        Each step of the clock corresponds to the close of that bar, i.e.
        ``candle_open_ts + period``.  The very first timestamp has no
        prior bar to compute features from, so it's skipped.
        """
        return cls(timestamps=sorted(timestamps), index=0)

    @classmethod
    def from_candles(cls, candles: list, timeframe_seconds: int) -> BacktestClock:
        """Build from a ``Candle`` list and the bar duration in seconds.

        The clock emits close-of-bar timestamps: ``candle.timestamp + period_ms``
        for every bar that is followed by at least one more bar (the *last* bar
        is the "current" bar that may still be forming).
        """
        period_ms = timeframe_seconds * 1000
        timestamps = [c.timestamp + period_ms for c in candles]
        #  The *very first* timestamp has no prior bar to build features from,
        # so we always start from index 0 (the first close moment).
        return cls(timestamps=timestamps, index=0)
