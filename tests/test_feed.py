"""Feed helpers: universe resolution and Candle -> DataFrame conversion."""
from __future__ import annotations

import pytest

from crypto_bot.config.env import Config
from crypto_bot.core.types import Candle
from crypto_bot.data.feed import build_symbols, discover_symbols, resolve_symbols, to_dataframe


class FakeMarketDataClient:
    async def fetch_tickers(self, symbols=None):
        return {
            "BTC/USDT": {"quoteVolume": 1000},
            "ETH/USDT": {"quoteVolume": 2000},
            "USDC/USDT": {"quoteVolume": 3000},
            "BTCUP/USDT": {"quoteVolume": 4000},
            "SOL/USDT": {"baseVolume": 10, "last": 50},
            "BTC/USDT:USDT": {"quoteVolume": 9000},
        }

    async def available_symbols(self):
        return {
            "BTC/USDT",
            "ETH/USDT",
            "USDC/USDT",
            "BTCUP/USDT",
            "SOL/USDT",
        }


def test_to_dataframe_accepts_slots_candles():
    candles = [
        Candle(timestamp=2, open=1, high=2, low=0.5, close=1.5, volume=10),
        Candle(timestamp=1, open=1, high=2, low=0.5, close=1.4, volume=20),
    ]
    df = to_dataframe(candles)
    assert list(df["timestamp"]) == [1, 2]
    assert list(df.columns) == ["timestamp", "open", "high", "low", "close", "volume"]


def test_build_symbols_honors_exclusions(env_factory, settings_factory):
    settings = settings_factory(
        universe__symbols=["BTC", "ETH", "USDC", "BTCUP"],
        universe__exclude=["ETH"],
    )
    cfg = Config(settings=settings, env=env_factory())
    assert build_symbols(cfg) == ["BTC/USDT"]


@pytest.mark.asyncio
async def test_discover_symbols_filters_and_sorts(env_factory, settings_factory):
    settings = settings_factory(
        universe__symbols=["BTC"],
        universe__auto_discover={"enabled": True, "top_n": 2},
    )
    cfg = Config(settings=settings, env=env_factory())
    discovered = await discover_symbols(FakeMarketDataClient(), cfg)
    assert discovered == ["ETH/USDT", "BTC/USDT"]


@pytest.mark.asyncio
async def test_resolve_symbols_merges_explicit_first(env_factory, settings_factory):
    settings = settings_factory(
        universe__symbols=["BTC"],
        universe__auto_discover={"enabled": True, "top_n": 3},
    )
    cfg = Config(settings=settings, env=env_factory())
    resolved = await resolve_symbols(FakeMarketDataClient(), cfg)
    assert resolved[:3] == ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
