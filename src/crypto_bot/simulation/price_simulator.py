"""Simulate intra-candle price ticks for paper trading.

Between real updates (ticker / candle close) the price does not sit still —
it follows a mean-reverting random walk in log-price (Ornstein-Uhlenbeck
discretised via Euler-Maruyama). Tick volatility is scaled from ATR% by
the sqrt(time) rule so tick jitter matches the symbol's real volatility.
"""
from __future__ import annotations

import asyncio
import math
import random
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass

from ..core.logging_setup import get_logger

_log = get_logger(__name__)


@dataclass
class PriceSimulatorConfig:
    tick_interval_sec: float = 1.0
    theta: float = 0.05
    atr_to_sigma_factor: float = 0.6
    default_candle_seconds: float = 60.0
    min_sigma_tick: float = 0.00005
    max_sigma_tick: float = 0.01
    jump_probability: float = 0.0
    jump_sigma_multiplier: float = 6.0
    max_dt_ticks: float = 30.0


@dataclass
class _SymbolState:
    anchor_price: float
    simulated_price: float
    sigma_tick: float
    tick_size: float | None = None
    last_update_ts: float = 0.0  # epoch seconds, 0 = not yet initialised


class PriceSimulator:
    """Tick generator of "current" price for open paper positions.

    One instance serves any number of symbols concurrently. No network calls
    — works purely from pre-computed ATR% and the last known real price.
    """

    def __init__(
        self,
        config: PriceSimulatorConfig | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._config = config or PriceSimulatorConfig()
        self._rng = rng or random.Random()
        self._states: dict[str, _SymbolState] = {}

    def _calc_sigma_tick(self, atr_pct: float, candle_seconds: float) -> float:
        cfg = self._config
        n_ticks = max(candle_seconds / cfg.tick_interval_sec, 1.0)
        sigma = (abs(atr_pct) * cfg.atr_to_sigma_factor) / math.sqrt(n_ticks)
        return min(max(sigma, cfg.min_sigma_tick), cfg.max_sigma_tick)

    def set_anchor(
        self,
        symbol: str,
        price: float,
        atr_pct: float | None = None,
        candle_seconds: float | None = None,
        tick_size: float | None = None,
        reference_ts: float | None = None,
    ) -> None:
        if price <= 0:
            raise ValueError(f"price must be positive, got {price}")
        cfg = self._config
        candle_seconds = candle_seconds or cfg.default_candle_seconds
        sigma_tick = (
            self._calc_sigma_tick(atr_pct, candle_seconds)
            if atr_pct is not None
            else cfg.min_sigma_tick
        )
        now_ts = reference_ts if reference_ts is not None else time.time()
        state = self._states.get(symbol)
        if state is None:
            self._states[symbol] = _SymbolState(
                anchor_price=price,
                simulated_price=price,
                sigma_tick=sigma_tick,
                tick_size=tick_size,
                last_update_ts=now_ts,
            )
        else:
            state.anchor_price = price
            state.sigma_tick = sigma_tick
            state.last_update_ts = now_ts
            if tick_size is not None:
                state.tick_size = tick_size

    def tick(self, symbol: str, dt: float | None = None, reference_ts: float | None = None) -> float | None:
        state = self._states.get(symbol)
        if state is None:
            return None
        cfg = self._config
        now_ts = reference_ts if reference_ts is not None else time.time()
        if dt is None:
            if state.last_update_ts > 0:
                elapsed = max(now_ts - state.last_update_ts, 0.0)
                dt = elapsed / cfg.tick_interval_sec if elapsed > 0 else 1.0
            else:
                dt = 1.0
        state.last_update_ts = now_ts
        dt = min(dt, cfg.max_dt_ticks)
        log_anchor = math.log(state.anchor_price)
        log_price = math.log(state.simulated_price)
        reversion = min(cfg.theta * dt, 1.0) * (log_anchor - log_price)
        shock = state.sigma_tick * math.sqrt(dt) * self._rng.gauss(0.0, 1.0)
        if cfg.jump_probability > 0 and self._rng.random() < cfg.jump_probability:
            shock += state.sigma_tick * cfg.jump_sigma_multiplier * self._rng.choice([-1.0, 1.0])
        new_price = math.exp(log_price + reversion + shock)
        if state.tick_size:
            new_price = round(new_price / state.tick_size) * state.tick_size
        state.simulated_price = new_price
        return new_price

    def get_price(self, symbol: str) -> float | None:
        state = self._states.get(symbol)
        return state.simulated_price if state else None

    def get_current_prices(
        self, symbol_timeframes: Mapping[str, Iterable[str]]
    ) -> dict[str, dict[str, float]]:
        result: dict[str, dict[str, float]] = {}
        for symbol, timeframes in symbol_timeframes.items():
            price = self.get_price(symbol)
            if price is None:
                continue
            result[symbol] = {tf: price for tf in timeframes}
        return result

    def remove(self, symbol: str) -> None:
        self._states.pop(symbol, None)

    def tracked_symbols(self) -> list[str]:
        return list(self._states.keys())

    async def run_forever(
        self,
        symbols_provider: Callable[[], Iterable[str]],
        on_tick: Callable[[str, float], Awaitable[None] | None] | None = None,
        on_cycle_end: Callable[[], Awaitable[None] | None] | None = None,
    ) -> None:
        while True:
            for symbol in symbols_provider():
                try:
                    price = self.tick(symbol)
                    if price is not None and on_tick is not None:
                        result = on_tick(symbol, price)
                        if asyncio.iscoroutine(result):
                            await result
                except Exception as exc:
                    _log.error("price_sim: tick failed for %s: %s", symbol, exc, exc_info=True)
            if on_cycle_end is not None:
                try:
                    result = on_cycle_end()
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as exc:
                    _log.error("price_sim: on_cycle_end crashed: %s", exc, exc_info=True)
            await asyncio.sleep(self._config.tick_interval_sec)
