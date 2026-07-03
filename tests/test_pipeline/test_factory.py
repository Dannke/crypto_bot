"""Tests for pipeline composition factory."""
from __future__ import annotations

from crypto_bot.config.schemas import Settings
from crypto_bot.core.enums import Side, Signal
from crypto_bot.core.types import FeatureSet
from crypto_bot.pipeline.factory import (
    DEFAULT_STRATEGY_NAME,
    build_decision_pipeline,
    build_filters,
    build_strategy_manager,
    scoring_weights_from_settings,
)
from crypto_bot.strategy.signal_engine import SignalEngine


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
        extras={"ema_state": ema_sign, "last_close": 100.0},
    )


def test_scoring_weights_from_settings_normalizes():
    settings = Settings()
    weights = scoring_weights_from_settings(settings)
    assert abs(weights.total() - 1.0) < 1e-9


def test_build_filters_returns_all_quality_filters():
    settings = Settings()
    filters = build_filters(settings)
    names = {f.name for f in filters}
    assert names == {
        "blacklist",
        "cooldown",
        "liquidity",
        "spread",
        "trend",
        "volatility",
        "volume",
    }


def test_build_strategy_manager_activates_confluence():
    settings = Settings()
    manager = build_strategy_manager(settings)
    strategy = manager.get_default_strategy()
    assert isinstance(strategy, SignalEngine)
    assert DEFAULT_STRATEGY_NAME in manager.list_active_strategies()


def test_build_decision_pipeline_processes_batch():
    settings = Settings()
    settings = Settings.model_validate(
        {
            **settings.model_dump(),
            "scoring": {
                **settings.scoring.model_dump(),
                "min_score": 0.0,
                "min_confidence": 0.0,
            },
        }
    )
    pipeline = build_decision_pipeline(settings)
    manager = build_strategy_manager(settings)
    strategy = manager.get_default_strategy()
    assert strategy is not None

    features_by_symbol = {
        "BTC/USDT": {
            "15m": _feature("BTC/USDT", "15m"),
            "1h": _feature("BTC/USDT", "1h"),
            "4h": _feature("BTC/USDT", "4h"),
        }
    }
    result = pipeline.process(features_by_symbol, strategy)
    assert result["total_processed"] == 1
    assert len(result["selected"]) == 1
    selected = result["selected"][0]
    assert selected.signal == Signal.BUY
    assert selected.side == Side.LONG
    assert selected.explanation
