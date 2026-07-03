"""Tests for paper trading simulation."""
from __future__ import annotations

from crypto_bot.core.enums import Side
from crypto_bot.simulation.fees import FeeCalculator
from crypto_bot.simulation.paper_position import PaperPosition
from crypto_bot.simulation.pnl import PnLTracker
from crypto_bot.simulation.sl_tp import SLTPCalculator


def test_sltp_long_levels():
    levels = SLTPCalculator(max_stop_distance_pct=3.0, reward_risk_ratio=2.0).calculate(
        entry_price=100.0,
        side=Side.LONG,
        atr_pct=1.0,
    )
    assert levels.stop_loss < 100.0
    assert levels.take_profit > 100.0
    assert levels.reward_risk_ratio == 2.0


def test_paper_position_unrealized_pnl():
    pos = PaperPosition(
        symbol="BTC/USDT",
        side=Side.LONG,
        size=1.0,
        entry_price=100.0,
        stop_loss=95.0,
        take_profit=110.0,
    )
    assert pos.unrealized_pnl_pct(105.0) == 5.0
    assert pos.unrealized_pnl_abs(105.0) == 5.0


def test_pnl_tracker_summary_after_close():
    tracker = PnLTracker(initial_equity=10000.0)
    pos = PaperPosition(
        symbol="BTC/USDT",
        side=Side.LONG,
        size=1.0,
        entry_price=100.0,
        stop_loss=95.0,
        take_profit=110.0,
    )
    tracker.add_position(pos)
    tracker.close_position(pos, 105.0)
    summary = tracker.get_summary()
    assert summary.total_trades == 1
    assert summary.winning_trades == 1
    assert summary.total_pnl_abs == 5.0


def test_fee_calculator():
    fee = FeeCalculator().calculate(amount=1.0, price=100.0, is_maker=False)
    assert fee.fee_abs > 0
    assert fee.net_amount < 100.0
