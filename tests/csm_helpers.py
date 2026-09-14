"""Shared fixtures and builders for the CSM research tests (task 11).

The canonical synthetic universe is five coins whose cumulative returns
over the lookback window are +20% / +10% / 0% / -10% / -20%.  With a
40%/40% top/bottom cut the expected selection is A/B LONG and D/E SHORT,
with C (the neutral coin) never selected.
"""
from __future__ import annotations

from crypto_bot.core.enums import Mode
from crypto_bot.core.types import Candle
from crypto_bot.portfolio import (
    MarketSnapshot,
    PortfolioState,
    get_market_snapshot,
    get_universe_snapshot,
)
from crypto_bot.simulation.historical_source import HistoricalCandleSource
from crypto_bot.strategy import CrossSectionalMomentumStrategy, StrategyContext

PERIOD_MS = 3_600_000
BASE_TS = 1_700_000_000_000
LOOKBACK = 5
MIN_BARS = LOOKBACK + 2  # 7 bars: lookback + the bar needed to anchor

# Canonical synthetic universe: A/B top, D/E bottom, C neutral.
FIVE_COIN_RETS = {
    "A/USDT": 0.20,
    "B/USDT": 0.10,
    "C/USDT": 0.00,
    "D/USDT": -0.10,
    "E/USDT": -0.20,
}
FIVE_COIN_SYMBOLS = ["A/USDT", "B/USDT", "C/USDT", "D/USDT", "E/USDT"]

# Cuts that select exactly the top two and the bottom two of the five.
TOP_FRACTION = 0.4
SHORT_FRACTION = 0.4


def momentum_context() -> StrategyContext:
    return StrategyContext(
        scoring_weights={},
        min_score=0.0,
        min_confidence=0.0,
        adx_min=0.0,
        rsi_long=(0.0, 100.0),
        rsi_short=(0.0, 100.0),
        atr_min_pct=0.0,
        atr_max_pct=100.0,
        take_profit_risk_multiple=2.0,
    )


def momentum(**kwargs) -> CrossSectionalMomentumStrategy:
    params: dict[str, object] = {}
    if "lookbacks_bars" not in kwargs:
        params["lookback_bars"] = LOOKBACK
    params.update(kwargs)
    return CrossSectionalMomentumStrategy(momentum_context(), ["1h"], **params)


def _candles_from_closes(closes: list[float], *, open_price: float = 100.0) -> list[Candle]:
    return [
        Candle(
            timestamp=BASE_TS + i * PERIOD_MS,
            open=open_price,
            high=max(open_price, c) * 1.001,
            low=min(open_price, c) * 0.999,
            close=c,
            volume=1000.0,
        )
        for i, c in enumerate(closes)
    ]


def closes_snapshot(
    closes_by_symbol: dict[str, list[float]],
    *,
    anchor_offset_bars: int = 0,
    open_price: float = 100.0,
) -> MarketSnapshot:
    """MarketSnapshot whose bars follow the given per-symbol close series.

    ``anchor_offset_bars`` moves the observation instant forward by whole
    bars without changing the data, which models a later rebalance tick.
    """
    source = HistoricalCandleSource()
    symbols = list(closes_by_symbol)
    n_bars = len(next(iter(closes_by_symbol.values())))
    for symbol, closes in closes_by_symbol.items():
        source.load_all(symbol, "1h", _candles_from_closes(closes, open_price=open_price))
    anchor = BASE_TS + (n_bars + anchor_offset_bars) * PERIOD_MS
    universe = get_universe_snapshot(source, symbols, "1h", anchor)
    return get_market_snapshot(source, universe, "1h")


def snapshot(
    rets: dict[str, float],
    *,
    n_bars: int = MIN_BARS,
    anchor_offset_bars: int = 0,
) -> MarketSnapshot:
    """Closed-bars-only snapshot; every symbol jumps by its return on the last bar."""
    return closes_snapshot(
        {s: [100.0] * (n_bars - 1) + [100.0 * (1.0 + ret)] for s, ret in rets.items()},
        anchor_offset_bars=anchor_offset_bars,
    )


def five_coin_closes(n_bars: int = MIN_BARS) -> dict[str, list[float]]:
    return {s: [100.0] * (n_bars - 1) + [100.0 * (1 + ret)] for s, ret in FIVE_COIN_RETS.items()}


def state(anchor_ms: int | None = None) -> PortfolioState:
    return PortfolioState(
        as_of_ms=anchor_ms if anchor_ms is not None else BASE_TS + MIN_BARS * PERIOD_MS,
        mode=Mode.PAPER,
        equity=10_000.0,
        cash=10_000.0,
    )
