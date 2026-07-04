"""End-to-end tests for DecisionPipeline."""
from __future__ import annotations

from crypto_bot.config.schemas import Settings
from crypto_bot.core.enums import Side, Signal
from crypto_bot.core.types import FeatureSet
from crypto_bot.pipeline.factory import build_decision_pipeline, build_strategy_manager, get_active_strategy


def _feature(symbol: str, tf: str, ema_sign: float = 1.0) -> FeatureSet:
    return FeatureSet(
        symbol=symbol,
        timeframe=tf,
        trend_score=0.9,
        momentum_score=0.8,
        volatility_score=0.7,
        volume_score=0.8,
        adx=30.0,
        rsi=55.0,
        atr_pct=1.5,
        ema_fast=100.0,
        ema_mid=99.0,
        ema_slow=98.0,
        liquidity_score=0.8,
        spread_pct=0.05,
        volume=5000.0,
        close=100.0,
        extras={"ema_state": ema_sign, "last_close": 100.0},
    )


def test_decision_pipeline_selects_top_candidate():
    settings = Settings.model_validate(
        {
            **Settings().model_dump(),
            "scoring": {
                **Settings().scoring.model_dump(),
                "min_score": 0.0,
                "min_confidence": 0.0,
                "max_candidates_per_cycle": 1,
            },
        }
    )
    pipeline = build_decision_pipeline(settings)
    strategy = get_active_strategy(build_strategy_manager(settings))
    features = {
        "BTC/USDT": {
            "1m": _feature("BTC/USDT", "1m"),
            "15m": _feature("BTC/USDT", "15m"),
            "1h": _feature("BTC/USDT", "1h"),
            "4h": _feature("BTC/USDT", "4h"),
        },
        "ETH/USDT": {
            "1m": _feature("ETH/USDT", "1m", ema_sign=0.0),
            "15m": _feature("ETH/USDT", "15m", ema_sign=0.0),
            "1h": _feature("ETH/USDT", "1h", ema_sign=0.0),
            "4h": _feature("ETH/USDT", "4h", ema_sign=0.0),
        },
    }
    result = pipeline.process(features, strategy)
    assert result["stats"]["selected_count"] == 1
    assert result["selected"][0].signal == Signal.BUY
    assert result["selected"][0].side == Side.LONG
    assert result["selected"][0].explanation
