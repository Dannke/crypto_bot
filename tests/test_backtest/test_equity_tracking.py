"""Тест: current_equity не дрейфует при open→MTM→close→reopen циклах.

После каждого close_position() current_equity должен быть **точно** равен
initial_equity + sum(realized_pnl всех закрытых позиций) — ни дрейфа,
ни двойного учёта нереализованного P&L.
"""
from __future__ import annotations

from datetime import UTC, datetime

from crypto_bot.core.enums import Side
from crypto_bot.simulation.paper_position import PaperPosition
from crypto_bot.simulation.pnl import PnLTracker


def _pos(symbol: str = "BTC/USDT", tf: str = "1h",
         side: Side = Side.LONG, size: float = 1.0,
         entry: float = 100.0, sl: float = 95.0, tp: float = 110.0,
         entry_fee: float = 0.0) -> PaperPosition:
    return PaperPosition(
        symbol=symbol, timeframe=tf, side=side,
        size=size, entry_price=entry, stop_loss=sl, take_profit=tp,
        entry_time=datetime.now(tz=UTC), entry_fee_abs=entry_fee,
    )


def test_equity_zero_fees_profit_then_loss() -> None:
    """Два цикла: прибыль → убыток. Без комиссий. Равенство до цента."""
    tracker = PnLTracker(initial_equity=10000.0)

    # --- Cycle 1: profit ---
    p1 = _pos(entry=100.0, tp=110.0)
    tracker.add_position(p1)

    # MTM at 105 (unrealized +5)
    mtm = tracker.mark_to_market_equity({("BTC/USDT", "1h"): 105.0})
    tracker.record_equity(datetime.now(tz=UTC), mtm)
    assert tracker.current_equity == 10005.0  # 10000 + 5 unrealized

    # Close at 110 (realized +10)
    tracker.close_position(p1, exit_price=110.0, closed_by="take_profit", exit_fee_abs=0.0)
    expected = 10000.0 + p1.pnl_abs  # 10000 + 10.0 = 10010.0
    assert tracker.current_equity == expected, (
        f"cycle1: expected {expected}, got {tracker.current_equity}"
    )

    # --- Cycle 2: loss ---
    p2 = _pos(entry=110.0, sl=105.0, tp=120.0)
    tracker.add_position(p2)

    # MTM at 108 (unrealized -2 relative to entry, but realized basis = 10010)
    mtm = tracker.mark_to_market_equity({("BTC/USDT", "1h"): 108.0})
    tracker.record_equity(datetime.now(tz=UTC), mtm)
    assert tracker.current_equity == 10008.0  # 10010 - 2

    # Close at 105 (realized -5)
    tracker.close_position(p2, exit_price=105.0, closed_by="stop_loss", exit_fee_abs=0.0)
    expected = 10000.0 + p1.pnl_abs + p2.pnl_abs  # 10000 + 10 - 5 = 10005.0
    assert tracker.current_equity == expected, (
        f"cycle2: expected {expected}, got {tracker.current_equity} "
        f"(p1.pnl_abs={p1.pnl_abs}, p2.pnl_abs={p2.pnl_abs})"
    )


def test_equity_with_fees() -> None:
    """Два цикла с ненулевыми комиссиями. Равенство до цента."""
    tracker = PnLTracker(initial_equity=10000.0)

    # Cycle 1: profit with entry_fee=5, exit_fee=3 → net pnl = 10 - 8 = 2
    p1 = _pos(entry=100.0, tp=110.0, entry_fee=5.0)
    tracker.add_position(p1)
    mtm = tracker.mark_to_market_equity({("BTC/USDT", "1h"): 105.0})
    tracker.record_equity(datetime.now(tz=UTC), mtm)
    tracker.close_position(p1, exit_price=110.0, closed_by="take_profit", exit_fee_abs=3.0)

    # p1.pnl_abs = (110-100)*1 - (5+3) = 10 - 8 = 2
    expected_1 = 10000.0 + p1.pnl_abs  # 10002
    assert tracker.current_equity == expected_1, (
        f"cycle1: expected {expected_1}, got {tracker.current_equity}"
    )

    # Cycle 2: loss with entry_fee=5, exit_fee=2 → gross = -5, fees = 7, net = -12
    p2 = _pos(entry=110.0, sl=105.0, tp=120.0, entry_fee=5.0)
    tracker.add_position(p2)
    mtm = tracker.mark_to_market_equity({("BTC/USDT", "1h"): 108.0})
    tracker.record_equity(datetime.now(tz=UTC), mtm)
    tracker.close_position(p2, exit_price=105.0, closed_by="stop_loss", exit_fee_abs=2.0)

    expected_2 = 10000.0 + p1.pnl_abs + p2.pnl_abs
    assert tracker.current_equity == expected_2, (
        f"cycle2: expected {expected_2}, got {tracker.current_equity} "
        f"(p1.pnl_abs={p1.pnl_abs}, p2.pnl_abs={p2.pnl_abs}, "
        f"sum={p1.pnl_abs + p2.pnl_abs})"
    )


def test_equity_no_mtm_calls_between_closes() -> None:
    """Закрытие двух позиций подряд без промежуточных record_equity — дрейфа нет."""
    tracker = PnLTracker(initial_equity=10000.0)

    p1 = _pos(entry=100.0, tp=110.0)
    tracker.add_position(p1)
    tracker.close_position(p1, exit_price=110.0, closed_by="take_profit", exit_fee_abs=0.0)

    p2 = _pos(entry=110.0, sl=105.0, tp=120.0)
    tracker.add_position(p2)
    tracker.close_position(p2, exit_price=105.0, closed_by="stop_loss", exit_fee_abs=0.0)

    expected = 10000.0 + p1.pnl_abs + p2.pnl_abs  # 10000 + 10 - 5 = 10005
    assert tracker.current_equity == expected, (
        f"expected {expected}, got {tracker.current_equity}"
    )


def test_equity_mtm_interleaved_zero_fees() -> None:
    """MTM-вызовы между каждым шагом — дрейфа нет (самый жёсткий сценарий)."""
    tracker = PnLTracker(initial_equity=10000.0)

    # Open → MTM → MTM → Close
    p1 = _pos(entry=100.0, tp=110.0)
    tracker.add_position(p1)
    m1 = tracker.mark_to_market_equity({("BTC/USDT", "1h"): 102.0})
    tracker.record_equity(datetime.now(tz=UTC), m1)
    m2 = tracker.mark_to_market_equity({("BTC/USDT", "1h"): 108.0})
    tracker.record_equity(datetime.now(tz=UTC), m2)
    tracker.close_position(p1, exit_price=110.0, closed_by="take_profit", exit_fee_abs=0.0)

    # Open → MTM → MTM → Close
    p2 = _pos(entry=110.0, sl=105.0, tp=120.0)
    tracker.add_position(p2)
    m3 = tracker.mark_to_market_equity({("BTC/USDT", "1h"): 112.0})
    tracker.record_equity(datetime.now(tz=UTC), m3)
    m4 = tracker.mark_to_market_equity({("BTC/USDT", "1h"): 106.0})
    tracker.record_equity(datetime.now(tz=UTC), m4)
    tracker.close_position(p2, exit_price=105.0, closed_by="stop_loss", exit_fee_abs=0.0)

    expected = 10000.0 + p1.pnl_abs + p2.pnl_abs  # 10000 + 10 - 5 = 10005
    assert tracker.current_equity == expected, (
        f"mtm-interleaved: expected {expected}, got {tracker.current_equity} "
        f"(p1.pnl_abs={p1.pnl_abs}, p2.pnl_abs={p2.pnl_abs})"
    )


def test_equity_three_cycles_mixed_fees() -> None:
    """Три цикла: profit, loss, profit — реалистичные комиссии. Равенство до цента."""
    tracker = PnLTracker(initial_equity=10000.0)

    # Cycle 1
    p1 = _pos(entry=100.0, tp=110.0, entry_fee=2.0)
    tracker.add_position(p1)
    mtm = tracker.mark_to_market_equity({("BTC/USDT", "1h"): 103.0})
    tracker.record_equity(datetime.now(tz=UTC), mtm)
    tracker.close_position(p1, exit_price=110.0, closed_by="take_profit", exit_fee_abs=1.5)

    # Cycle 2
    p2 = _pos(entry=110.0, sl=105.0, tp=120.0, entry_fee=2.0)
    tracker.add_position(p2)
    mtm = tracker.mark_to_market_equity({("BTC/USDT", "1h"): 107.0})
    tracker.record_equity(datetime.now(tz=UTC), mtm)
    tracker.close_position(p2, exit_price=105.0, closed_by="stop_loss", exit_fee_abs=1.5)

    # Cycle 3
    p3 = _pos(entry=100.0, tp=115.0, entry_fee=2.0)
    tracker.add_position(p3)
    mtm = tracker.mark_to_market_equity({("BTC/USDT", "1h"): 105.0})
    tracker.record_equity(datetime.now(tz=UTC), mtm)
    tracker.close_position(p3, exit_price=115.0, closed_by="take_profit", exit_fee_abs=1.5)

    expected = 10000.0 + p1.pnl_abs + p2.pnl_abs + p3.pnl_abs
    assert tracker.current_equity == expected, (
        f"3-cycle: expected {expected}, got {tracker.current_equity} "
        f"individual: p1={p1.pnl_abs} p2={p2.pnl_abs} p3={p3.pnl_abs} "
        f"sum={p1.pnl_abs + p2.pnl_abs + p3.pnl_abs}"
    )
