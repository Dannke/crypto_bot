"""Tests for PriceSimulator: anchor init, tick generation, re-anchor convergence."""
from __future__ import annotations

import random

import pytest

from crypto_bot.simulation.price_simulator import PriceSimulator, PriceSimulatorConfig


def make_sim(**overrides) -> PriceSimulator:
    cfg = PriceSimulatorConfig(**overrides)
    return PriceSimulator(config=cfg, rng=random.Random(42))


def test_tick_without_anchor_returns_none():
    sim = make_sim()
    assert sim.tick("BTC/USDT", dt=1.0) is None
    assert sim.get_price("BTC/USDT") is None


def test_set_anchor_initializes_price_exactly():
    sim = make_sim()
    sim.set_anchor("BTC/USDT", price=65000.0, atr_pct=0.006, candle_seconds=60)
    assert sim.get_price("BTC/USDT") == pytest.approx(65000.0)


def test_set_anchor_rejects_non_positive_price():
    sim = make_sim()
    with pytest.raises(ValueError):
        sim.set_anchor("BTC/USDT", price=0.0)
    with pytest.raises(ValueError):
        sim.set_anchor("BTC/USDT", price=-100.0)


def test_ticks_move_price_and_stay_bounded():
    sim = make_sim(theta=0.1)
    sim.set_anchor("BTC/USDT", price=65000.0, atr_pct=0.02, candle_seconds=60)
    prices = [sim.tick("BTC/USDT", dt=1.0) for _ in range(500)]
    assert all(50000 < p < 80000 for p in prices)
    assert len(set(round(p, 2) for p in prices)) > 50


def test_reanchor_pulls_price_toward_new_anchor():
    sim = make_sim(theta=0.3)
    sim.set_anchor("BTC/USDT", price=65000.0, atr_pct=0.001, candle_seconds=60)
    for _ in range(20):
        sim.tick("BTC/USDT", dt=1.0)
    before = sim.get_price("BTC/USDT")
    sim.set_anchor("BTC/USDT", price=70000.0, atr_pct=0.001, candle_seconds=60)
    for _ in range(300):
        sim.tick("BTC/USDT", dt=1.0)
    after = sim.get_price("BTC/USDT")
    assert abs(after - 70000.0) < abs(before - 70000.0)
    assert abs(after - 70000.0) < 500


def test_sigma_scales_with_atr_and_inversely_with_tick_rate():
    sim = make_sim()
    low_vol = sim._calc_sigma_tick(atr_pct=0.002, candle_seconds=60)
    high_vol = sim._calc_sigma_tick(atr_pct=0.02, candle_seconds=60)
    assert high_vol > low_vol
    fast_ticks = sim._calc_sigma_tick(atr_pct=0.01, candle_seconds=3600)
    slow_ticks = sim._calc_sigma_tick(atr_pct=0.01, candle_seconds=60)
    assert fast_ticks < slow_ticks


def test_tick_size_quantizes_simulated_price():
    sim = make_sim(theta=0.2)
    sim.set_anchor("BTC/USDT", price=65000.37, atr_pct=0.01, candle_seconds=60, tick_size=0.5)
    for _ in range(50):
        price = sim.tick("BTC/USDT", dt=1.0)
        assert round(price / 0.5) == pytest.approx(price / 0.5)


def test_get_current_prices_broadcasts_same_price_across_timeframes():
    sim = make_sim()
    sim.set_anchor("BTC/USDT", price=65000.0, atr_pct=0.006, candle_seconds=60)
    sim.set_anchor("ETH/USDT", price=3400.0, atr_pct=0.01, candle_seconds=60)
    sim.tick("BTC/USDT", dt=1.0)
    sim.tick("ETH/USDT", dt=1.0)
    result = sim.get_current_prices({"BTC/USDT": ["1m", "15m", "1h"], "ETH/USDT": ["1m"]})
    assert set(result["BTC/USDT"].values()) == {sim.get_price("BTC/USDT")}
    assert list(result["ETH/USDT"].keys()) == ["1m"]


def test_remove_and_tracked_symbols():
    sim = make_sim()
    sim.set_anchor("BTC/USDT", price=65000.0)
    sim.set_anchor("ETH/USDT", price=3400.0)
    assert set(sim.tracked_symbols()) == {"BTC/USDT", "ETH/USDT"}
    sim.remove("BTC/USDT")
    assert sim.tracked_symbols() == ["ETH/USDT"]
    assert sim.get_price("BTC/USDT") is None


@pytest.mark.asyncio
async def test_run_forever_calls_on_tick_for_each_symbol():
    import asyncio

    sim = make_sim(tick_interval_sec=0.01)
    sim.set_anchor("BTC/USDT", price=65000.0, atr_pct=0.006, candle_seconds=60)
    seen: list[tuple[str, float]] = []

    async def on_tick(symbol: str, price: float) -> None:
        seen.append((symbol, price))
        if len(seen) >= 3:
            task.cancel()

    task = asyncio.ensure_future(
        sim.run_forever(symbols_provider=lambda: ["BTC/USDT"], on_tick=on_tick, on_cycle_end=None)
    )
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(seen) >= 3
    assert all(symbol == "BTC/USDT" for symbol, _ in seen)


def test_simulated_spike_triggers_stop_loss():
    """Цена пробивает SL между скан-циклами — позиция закрывается.

    Воспроизводит сценарий, ради которого писался PriceSimulator:
    скачок цены уходит ниже SL и возвращается обратно; проверка,
    что check_stop_loss срабатывает в момент касания, а не постфактум
    по усреднённой цене.
    """
    from crypto_bot.core.enums import Side
    from crypto_bot.simulation.paper_position import PaperPosition
    from crypto_bot.simulation.pnl import PnLTracker

    # atr_pct=3.0 даёт sigma_tick ~ (3.0% * 0.6) / sqrt(60) ≈ 0.23%, что
    # упирается в max_sigma_tick=0.01 (1%). Это форсирует предельную
    # волатильность тика — пробитие SL=1% за 5000 шагов гарантировано
    # вне зависимости от seed'а. В реале sigma_tick будет ~0.02–0.05%.
    sim = make_sim(theta=0.001, tick_interval_sec=1.0)
    entry = 100.0
    stop = 99.0
    take = 105.0

    sim.set_anchor("ASSET/USDT", price=entry, atr_pct=3.0, candle_seconds=60)

    tracker = PnLTracker()
    pos = PaperPosition(
        symbol="ASSET/USDT", timeframe="1h", side=Side.LONG,
        size=10.0, entry_price=entry, stop_loss=stop, take_profit=take,
    )
    tracker.add_position(pos)
    assert pos.is_open

    all_prices = []
    for _ in range(5000):
        price = sim.tick("ASSET/USDT", dt=1.0)
        if price is not None:
            all_prices.append(price)
            if pos.check_stop_loss(price):
                tracker.close_position(pos, price, closed_by="stop_loss")
                break

    assert pos.is_closed, (
        f"price never hit SL={stop}; min={min(all_prices):.4f} "
        f"max={max(all_prices):.4f} over {len(all_prices)} ticks"
    )
    assert pos.closed_by == "stop_loss"
    assert pos.pnl_pct is not None and pos.pnl_pct < 0
    assert "ASSET/USDT" not in tracker.open_symbols()
