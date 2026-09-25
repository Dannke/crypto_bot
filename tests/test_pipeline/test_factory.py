"""Tests for pipeline composition factory."""
from __future__ import annotations

import pytest

from crypto_bot.config.schemas import Settings
from crypto_bot.core.enums import Side, Signal
from crypto_bot.core.types import FeatureSet
from crypto_bot.pipeline.factory import (
    DEFAULT_STRATEGY_NAME,
    build_decision_pipeline,
    build_filters,
    build_portfolio_strategy,
    build_strategy_manager,
    resolve_rebalance_hours,
    scoring_weights_from_settings,
)
from crypto_bot.strategy.portfolio_strategies import (
    MEAN_REVERSION_V0_STRATEGY_NAME,
    MOMENTUM_V0_STRATEGY_NAME,
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
            "1m": _feature("BTC/USDT", "1m"),
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


def _settings_with(strategy_name: str, csm_hours: int, mr_hours: int) -> Settings:
    """Settings, где каденции CSM и mean_reversion заведомо различимы."""
    base = Settings()
    portfolio = base.portfolio.model_copy(
        update={
            "strategy_name": strategy_name,
            "csm": base.portfolio.csm.model_copy(update={"rebalance_hours": csm_hours}),
            "mean_reversion": base.portfolio.mean_reversion.model_copy(
                update={"rebalance_hours": mr_hours}
            ),
        }
    )
    return base.model_copy(update={"portfolio": portfolio})


def test_resolve_rebalance_hours_uses_mean_reversion_block():
    """Активная MR-стратегия читает свою каденцию, а не CSM-блок."""
    settings = _settings_with(MEAN_REVERSION_V0_STRATEGY_NAME, csm_hours=24, mr_hours=12)
    assert resolve_rebalance_hours(settings) == 12


def test_resolve_rebalance_hours_uses_csm_block_for_momentum():
    """CSM-стратегия остаётся на своей каденции — прежнее поведение."""
    settings = _settings_with(MOMENTUM_V0_STRATEGY_NAME, csm_hours=24, mr_hours=12)
    assert resolve_rebalance_hours(settings) == 24


def test_resolve_rebalance_hours_does_not_leak_between_strategies():
    """Одна и та же конфигурация даёт разную каденцию под разные стратегии.

    Прямая регрессия на дефект: оба потребителя читали csm.rebalance_hours
    независимо от активной стратегии, поэтому MR молча ребалансировался с
    каденцией CSM.
    """
    csm_hours, mr_hours = 24, 6
    momentum = _settings_with(MOMENTUM_V0_STRATEGY_NAME, csm_hours, mr_hours)
    mean_reversion = _settings_with(MEAN_REVERSION_V0_STRATEGY_NAME, csm_hours, mr_hours)

    assert resolve_rebalance_hours(momentum) != resolve_rebalance_hours(mean_reversion)


def _mr_settings(**mr_overrides) -> Settings:
    """Settings на mean_reversion_v0 с переопределением полей MR."""
    base = Settings()
    mean_reversion = base.portfolio.mean_reversion.model_copy(update=mr_overrides)
    portfolio = base.portfolio.model_copy(
        update={"strategy_name": MEAN_REVERSION_V0_STRATEGY_NAME,
                "mean_reversion": mean_reversion}
    )
    return base.model_copy(update={"portfolio": portfolio})


def test_mr_percentiles_come_from_config():
    """Доли отбора берутся из конфига, а не из литералов в фабрике."""
    strategy = build_portfolio_strategy(
        _mr_settings(long_percentile=0.70, short_percentile=0.30)
    )
    assert strategy.top_fraction == pytest.approx(0.30)
    assert strategy.short_fraction == pytest.approx(0.30)


def test_mr_default_percentiles_are_symmetric():
    """Дефолтный отбор симметричен: перекос книги в шорт был дефектом.

    Прямая регрессия: в фабрике стояли литералы 1.0 - 0.90 и 0.2, то есть
    10% длинных кандидатов против 20% коротких. На train это дало книгу
    26 LONG против 88 SHORT — уже не то явление, которое описывает гипотеза
    о cross-sectional возврате к среднему.
    """
    strategy = build_portfolio_strategy(_mr_settings())

    assert strategy.top_fraction == pytest.approx(strategy.short_fraction), (
        f"доли отбора асимметричны: long={strategy.top_fraction} "
        f"short={strategy.short_fraction}"
    )


def test_mr_percentiles_give_equal_sides_on_the_registered_universe():
    """На зарегистрированном универсуме из 9 символов стороны равны по слотам."""
    from math import ceil

    strategy = build_portfolio_strategy(_mr_settings())
    n_symbols = 9
    n_long = ceil(strategy.top_fraction * n_symbols)
    n_short = ceil(strategy.short_fraction * n_symbols)

    assert n_long == n_short, f"{n_long} long против {n_short} short на {n_symbols} символах"
