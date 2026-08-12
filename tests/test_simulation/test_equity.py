"""Tests for equity/drawdown correctness — mark-to-market PnL."""

from __future__ import annotations

from datetime import UTC, datetime

from crypto_bot.core.enums import Side
from crypto_bot.simulation.paper_position import PaperPosition
from crypto_bot.simulation.pnl import PnLTracker

# --------------------------------------------------------------------------- #
# unrealized_pnl — unit scenarios
# --------------------------------------------------------------------------- #

def test_unrealized_pnl_empty_positions():
    tracker = PnLTracker()
    assert tracker.unrealized_pnl({}) == 0.0


def test_unrealized_pnl_all_closed():
    tracker = PnLTracker()
    pos = PaperPosition(symbol="BTC/USDT", timeframe="1h", side=Side.LONG,
                        size=1.0, entry_price=100.0, stop_loss=95.0, take_profit=110.0)
    tracker.add_position(pos)
    tracker.close_position(pos, 105.0)
    assert tracker.unrealized_pnl({("BTC/USDT", "1h"): 200.0}) == 0.0


def test_unrealized_pnl_long_up():
    tracker = PnLTracker()
    pos = PaperPosition(symbol="BTC/USDT", timeframe="1h", side=Side.LONG,
                        size=2.0, entry_price=100.0, stop_loss=95.0, take_profit=110.0)
    tracker.add_position(pos)
    pnl = tracker.unrealized_pnl({("BTC/USDT", "1h"): 110.0})
    assert pnl == 20.0  # (110 - 100) * 2


def test_unrealized_pnl_long_down():
    tracker = PnLTracker()
    pos = PaperPosition(symbol="BTC/USDT", timeframe="1h", side=Side.LONG,
                        size=1.0, entry_price=100.0, stop_loss=95.0, take_profit=110.0)
    tracker.add_position(pos)
    pnl = tracker.unrealized_pnl({("BTC/USDT", "1h"): 92.0})
    assert pnl == -8.0  # (92 - 100) * 1


def test_unrealized_pnl_short_up():
    tracker = PnLTracker()
    pos = PaperPosition(symbol="BTC/USDT", timeframe="1h", side=Side.SHORT,
                        size=1.0, entry_price=100.0, stop_loss=105.0, take_profit=95.0)
    tracker.add_position(pos)
    pnl = tracker.unrealized_pnl({("BTC/USDT", "1h"): 110.0})
    assert pnl == -10.0  # (100 - 110) * 1


def test_unrealized_pnl_short_down():
    tracker = PnLTracker()
    pos = PaperPosition(symbol="BTC/USDT", timeframe="1h", side=Side.SHORT,
                        size=2.0, entry_price=100.0, stop_loss=105.0, take_profit=95.0)
    tracker.add_position(pos)
    pnl = tracker.unrealized_pnl({("BTC/USDT", "1h"): 90.0})
    assert pnl == 20.0  # (100 - 90) * 2


def test_unrealized_pnl_multi_symbol():
    tracker = PnLTracker()
    btc = PaperPosition(symbol="BTC/USDT", timeframe="1h", side=Side.LONG,
                        size=1.0, entry_price=100.0, stop_loss=95.0, take_profit=110.0)
    eth = PaperPosition(symbol="ETH/USDT", timeframe="1h", side=Side.SHORT,
                        size=2.0, entry_price=200.0, stop_loss=210.0, take_profit=180.0)
    tracker.add_position(btc)
    tracker.add_position(eth)
    prices = {("BTC/USDT", "1h"): 110.0, ("ETH/USDT", "1h"): 190.0}
    assert tracker.unrealized_pnl(prices) == 30.0  # 10 + 20


def test_unrealized_pnl_missing_price():
    tracker = PnLTracker()
    pos = PaperPosition(symbol="BTC/USDT", timeframe="1h", side=Side.LONG,
                        size=1.0, entry_price=100.0, stop_loss=95.0, take_profit=110.0)
    tracker.add_position(pos)
    # Position is open but no price supplied → skip
    assert tracker.unrealized_pnl({}) == 0.0
    # Price for different key → skip
    assert tracker.unrealized_pnl({("ETH/USDT", "1h"): 200.0}) == 0.0


# --------------------------------------------------------------------------- #
# mark_to_market_equity
# --------------------------------------------------------------------------- #

def test_mtm_equity_no_open_positions():
    tracker = PnLTracker(initial_equity=10000.0, current_equity=10000.0, peak_equity=10000.0)
    assert tracker.mark_to_market_equity({}) == 10000.0


def test_mtm_equity_with_unrealized():
    tracker = PnLTracker(initial_equity=100.0, current_equity=100.0, peak_equity=100.0)
    pos = PaperPosition(symbol="BTC/USDT", timeframe="1h", side=Side.LONG,
                        size=1.0, entry_price=100.0, stop_loss=95.0, take_profit=110.0)
    tracker.add_position(pos)
    mtm = tracker.mark_to_market_equity({("BTC/USDT", "1h"): 108.0})
    assert mtm == 108.0  # 100 + 8 unrealized


# --------------------------------------------------------------------------- #
# record_equity / equity_history
# --------------------------------------------------------------------------- #

def test_record_equity_updates_peak():
    tracker = PnLTracker(initial_equity=100.0, current_equity=100.0, peak_equity=100.0)
    tracker.record_equity(datetime.now(tz=UTC), 100.0)
    assert tracker.peak_equity == 100.0
    tracker.record_equity(datetime.now(tz=UTC), 120.0)
    assert tracker.peak_equity == 120.0
    tracker.record_equity(datetime.now(tz=UTC), 110.0)
    assert tracker.peak_equity == 120.0
    assert len(tracker.equity_history) == 3


# --------------------------------------------------------------------------- #
# Golden test — intra-trade drawdown
# --------------------------------------------------------------------------- #

def test_golden_intratrade_drawdown():
    """Synthetic scenario: position opens, price goes against it ~8%,
    recovers and closes at +1%.  max_drawdown_pct must reflect the ~8%
    intra-trade drawdown, NOT near-zero as closed-trades-only logic would.

    This test MUST fail on the old equity logic (no per-bar MTM recording,
    no running peak in drawdown calc).  Verify by reverting the changes in
    ``pnl.py`` and ``backtester.py`` — the assertion on max_drawdown_pct
    will break.
    """
    tracker = PnLTracker(initial_equity=100.0, current_equity=100.0, peak_equity=100.0)

    pos = PaperPosition(
        symbol="BTC/USDT", timeframe="1h",
        side=Side.LONG, size=1.0,
        entry_price=100.0, stop_loss=80.0, take_profit=120.0,
    )
    tracker.add_position(pos)

    # Bar 1 — price goes to 105 (unrealized +5, peak=105)
    now = datetime.now(tz=UTC)
    mtm = tracker.mark_to_market_equity({("BTC/USDT", "1h"): 105.0})
    tracker.record_equity(now, mtm)

    # Bar 2 — price drops to 92 (unrealized -8, drawdown from 105: ~12.4%)
    mtm = tracker.mark_to_market_equity({("BTC/USDT", "1h"): 92.0})
    tracker.record_equity(now, mtm)

    # Bar 3 — slight recovery (unrealized -5)
    mtm = tracker.mark_to_market_equity({("BTC/USDT", "1h"): 95.0})
    tracker.record_equity(now, mtm)

    # Close at 101 → small +1% profit
    tracker.close_position(pos, 101.0)

    summary = tracker.get_summary()

    # The closed-trade-only logic yields max_drawdown ~0% at +1% total PnL.
    # Per-bar MTM should capture drawdown >> 5%.
    assert summary.total_pnl_abs >= 0.0, "test sanity: must not be a loss"
    assert summary.max_drawdown_pct > 5.0, (
        f"max_drawdown_pct={summary.max_drawdown_pct} — expected >5% "
        "intra-trade drawdown. Check that per-bar MTM equity is recorded "
        "and get_summary() uses a running peak."
    )


# --------------------------------------------------------------------------- #
# Golden test — daily drawdown resets at day boundary
# --------------------------------------------------------------------------- #

def test_golden_daily_drawdown_resets():
    """daily_peak_equity must reset at the start of each new day, while
    all-time peak_equity continues to track the all-time high.

    Scenario:
        Day 1: equity 100 → 120 → 110  (daily peak=120, all-time peak=120)
        Day 2: equity starts at 110 → 105  (daily peak=110, all-time peak=120)
    """
    tracker = PnLTracker(initial_equity=100.0, current_equity=100.0,
                         peak_equity=100.0, daily_peak_equity=100.0)

    day1 = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    day2 = datetime(2026, 7, 11, 8, 0, tzinfo=UTC)

    # Seed _last_reset_day (first decision on day1 in real flow)
    tracker.reset_daily_peak_if_day_changed(day1)

    # Day 1: equity rises to 120, then drops to 110 (realized = MTM here)
    tracker.current_equity = 120.0
    tracker.record_equity(day1, 120.0)
    assert tracker.peak_equity == 120.0, "all-time peak = 120"
    assert tracker.daily_peak_equity == 120.0, "daily peak = 120"

    tracker.current_equity = 110.0
    tracker.record_equity(day1, 110.0)
    assert tracker.peak_equity == 120.0, "all-time peak stays 120"
    assert tracker.daily_peak_equity == 120.0, "daily peak stays 120"

    # Day 2 boundary: reset_daily_peak... called before check
    tracker.reset_daily_peak_if_day_changed(day2)
    assert tracker.daily_peak_equity == 110.0, "daily peak resets to current equity (110)"

    # Day 2: equity drops to 105 (realized = MTM)
    tracker.current_equity = 105.0
    tracker.record_equity(day2, 105.0)
    assert tracker.peak_equity == 120.0, "all-time peak still 120"
    assert tracker.daily_peak_equity == 110.0, "daily peak capped at day-2 start (110)"

    # Daily drawdown from 110 → 105 = 4.55%, NOT from 120
    daily_dd = (tracker.daily_peak_equity - tracker.current_equity) / tracker.daily_peak_equity * 100
    all_time_dd = (tracker.peak_equity - tracker.current_equity) / tracker.peak_equity * 100
    assert abs(daily_dd - 4.55) < 0.01, f"daily_dd={daily_dd:.2f} expected ~4.55"
    assert abs(all_time_dd - 12.5) < 0.1, f"all_time_dd={all_time_dd:.2f} expected 12.5"
