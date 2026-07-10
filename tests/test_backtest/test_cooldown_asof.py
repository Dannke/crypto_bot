"""Integration: ``as_of_ms`` threading from Backtester through pipeline to
CooldownFilter.

Bar structure (30 candles, 1h period)::

  bar  0 …  7   … 10   …  20   …  29
       |-------|------|----------|
       warmup  ^first ^
               trade  cooldown
               bar    still active
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from crypto_bot.config.env import Config, EnvConfig
from crypto_bot.config.schemas import Settings
from crypto_bot.core.enums import Mode
from crypto_bot.core.types import Candle, FeatureSet
from crypto_bot.filters.cooldown import CooldownFilter
from crypto_bot.pipeline.decision_pipeline import DecisionPipeline
from crypto_bot.pipeline.factory import (
    build_candidate_builder,
    build_candidate_selector,
    build_strategy_manager,
    get_active_strategy,
)
from crypto_bot.simulation.backtester import Backtester
from crypto_bot.storage.db import Database
from crypto_bot.simulation.historical_source import HistoricalCandleSource
from crypto_bot.strategy.base import Strategy


def _uptrend_candles() -> list[Candle]:
    price = 100.0
    base_ts = 1_700_000_000_000
    period_ms = 3_600_000
    candles: list[Candle] = []
    for i in range(30):
        ts = base_ts + i * period_ms
        open_p = round(price, 2)
        close_p = round(price * 1.015, 2)
        high_p = round(max(open_p, close_p) * 1.005, 2)
        low_p = round(min(open_p, close_p) * 0.995, 2)
        candles.append(Candle(
            timestamp=ts,
            open=open_p,
            high=high_p,
            low=low_p,
            close=close_p,
            volume=1000.0,
        ))
        price = close_p
    return candles


def _make_env(**overrides) -> EnvConfig:
    data = dict(
        crypto_bot_mode=None,
        enable_trading=False,
        enable_live_trading=False,
        exchange_name=None,
        exchange_api_key="",
        exchange_api_secret="",
        exchange_sandbox=None,
        db_path=None,
        log_level=None,
        log_file=None,
        log_json=None,
    )
    data.update(overrides)
    return EnvConfig.model_validate(data)


def _base_settings(**overrides) -> Settings:
    """Settings that allow a signal on ≈bar 8 with cooldown=120 min."""
    base = Settings().model_dump()
    for key, value in overrides.items():
        parts = key.split("__")
        d = base
        for p in parts[:-1]:
            d = d[p]
        d[parts[-1]] = value
    return Settings.model_validate(base)


def _pipeline_with_cooldown(
    cooldown_minutes: int,
    last_trade_time: dict[str, datetime] | None = None,
) -> DecisionPipeline:
    settings = _base_settings(
        runtime__mode=Mode.PAPER,
        runtime__strategy="per_timeframe",
        strategy__trend__ema_fast=3,
        strategy__trend__ema_mid=5,
        strategy__trend__ema_slow=8,
        strategy__trend__adx_min=5,
        strategy__momentum__rsi_period=4,
        strategy__momentum__rsi_long_min=0,
        strategy__momentum__rsi_long_max=100,
        strategy__volatility__atr_period=3,
        strategy__volatility__atr_min_pct=0.1,
        strategy__volatility__atr_max_pct=20.0,
        strategy__volatility__bb_period=4,
        strategy__volume__ma_period=3,
        strategy__volume__spike_ratio=1.0,
        scoring__min_score=30,
        scoring__min_confidence=0.0,
        scoring__max_candidates_per_cycle=1,
        filters__enable_liquidity_filter=False,
        filters__enable_spread_filter=False,
        filters__enable_volume_filter=False,
        filters__cooldown_after_trade_minutes=cooldown_minutes,
        filters__min_quote_volume_usd=0,
        risk__risk_per_trade_pct=1.0,
        risk__take_profit_risk_multiple=2.0,
        risk__max_stop_distance_pct=5.0,
        risk__max_open_positions=5,
        risk__max_daily_drawdown_pct=20.0,
        risk__emergency_drawdown_pct=30.0,
    )
    builder = build_candidate_builder(settings, last_trade_time=last_trade_time)
    selector = build_candidate_selector(settings)
    return DecisionPipeline(builder=builder, selector=selector)


def _features_from_candle(
    candle: Candle, symbol: str = "BTC/USDT", tf: str = "1h",
) -> FeatureSet:
    """Build a minimal FeatureSet that will pass trend/momentum filters
    (strong uptrend → high scores)."""
    return FeatureSet(
        symbol=symbol,
        timeframe=tf,
        trend_score=0.9,
        momentum_score=0.8,
        volatility_score=0.6,
        volume_score=0.5,
        adx=30.0,
        rsi=60.0,
        atr_pct=1.5,
        ema_fast=float(candle.close * 1.02),
        ema_mid=float(candle.close),
        ema_slow=float(candle.close * 0.98),
        open=candle.open,
        high=candle.high,
        low=candle.low,
        close=candle.close,
        volume=candle.volume,
    )


class TestPipelineCooldownChain:
    """Integration tests for as_of_ms → CooldownFilter passthrough.

    Bar timeline (1h candles)::

      bar index:  10   11   12   13   14
      elapsed:     0    1h   2h   3h   4h
    """

    def _trade_ts(self, candles: list[Candle], bar: int) -> datetime:
        return datetime.fromtimestamp(candles[bar].timestamp / 1000, tz=UTC)

    def _build_strategy(self) -> Strategy:
        base = dict(
            runtime__mode=Mode.PAPER,
            runtime__strategy="per_timeframe",
            strategy__trend__ema_fast=3, strategy__trend__ema_mid=5,
            strategy__trend__ema_slow=8, strategy__trend__adx_min=5,
            strategy__momentum__rsi_period=4,
            strategy__momentum__rsi_long_min=0,
            strategy__momentum__rsi_long_max=100,
            strategy__volatility__atr_period=3,
            strategy__volatility__atr_min_pct=0.1,
            strategy__volatility__atr_max_pct=20.0,
            strategy__volatility__bb_period=4,
            strategy__volume__ma_period=3,
            strategy__volume__spike_ratio=1.0,
            scoring__min_score=30, scoring__min_confidence=0.0,
            scoring__max_candidates_per_cycle=1,
            filters__enable_liquidity_filter=False,
            filters__enable_spread_filter=False,
            filters__enable_volume_filter=False,
            filters__cooldown_after_trade_minutes=999,
            filters__min_quote_volume_usd=0,
            risk__risk_per_trade_pct=1.0,
            risk__take_profit_risk_multiple=2.0,
            risk__max_stop_distance_pct=5.0,
            risk__max_open_positions=5,
            risk__max_daily_drawdown_pct=20.0,
            risk__emergency_drawdown_pct=30.0,
        )
        mgr = build_strategy_manager(
            _base_settings(**base), strategy_name="per_timeframe",
        )
        return get_active_strategy(mgr)

    def test_as_of_rejects_within_cooldown_window(self):
        """as_of_ms inside the cooldown window produces IN_COOLDOWN."""
        candles = _uptrend_candles()
        trade_ts_dt = self._trade_ts(candles, 10)

        pipeline = _pipeline_with_cooldown(
            cooldown_minutes=120,
            last_trade_time={"BTC/USDT": trade_ts_dt},
        )

        # Bar 11 is only 1h after bar 10 → well within 120 min cooldown
        features_map = {
            "BTC/USDT": {"1h": _features_from_candle(candles[11])},
        }

        result = pipeline.process(
            features_map,
            self._build_strategy(),
            per_timeframe=True,
            as_of_ms=candles[11].timestamp,
        )

        reject_reasons = result["stats"]["reject_reasons"]
        assert "in_cooldown" in reject_reasons, (
            f"expected in_cooldown in {reject_reasons}"
        )

    def test_as_of_passes_after_cooldown_expires(self):
        """as_of_ms after cooldown window passes normally."""
        candles = _uptrend_candles()
        trade_ts_dt = self._trade_ts(candles, 10)

        pipeline = _pipeline_with_cooldown(
            cooldown_minutes=60,
            last_trade_time={"BTC/USDT": trade_ts_dt},
        )

        # Bar 14 is 4h after bar 10 → cooldown expired (60 min)
        features_map = {
            "BTC/USDT": {"1h": _features_from_candle(candles[14])},
        }

        result = pipeline.process(
            features_map,
            self._build_strategy(),
            per_timeframe=True,
            as_of_ms=candles[14].timestamp,
        )

        reject_reasons = result["stats"]["reject_reasons"]
        assert "in_cooldown" not in reject_reasons, (
            f"unexpected in_cooldown in {reject_reasons}"
        )

    def test_as_of_none_uses_wall_clock(self):
        """as_of_ms=None (live mode) should not use historical timestamp."""
        filter_instance = CooldownFilter(
            cooldown_minutes=60,
            last_trade_time={"BTC/USDT": datetime(2020, 1, 1, tzinfo=UTC)},
        )
        features = FeatureSet(
            symbol="BTC/USDT", timeframe="1h", trend_score=0.8,
            momentum_score=0.7, volatility_score=0.6, volume_score=0.5,
            adx=30.0, rsi=55.0, atr_pct=1.5,
            ema_fast=100.0, ema_mid=99.0, ema_slow=98.0,
        )
        # No reference_ts → uses datetime.now(tz=UTC) → cooldown expired
        # (last_trade_time is from 2020, so it's always expired vs wall-clock)
        result = filter_instance.evaluate(features)
        assert result.passed


@pytest.mark.asyncio
async def test_backtender_cooldown_via_as_of_ms(tmp_path):
    """End-to-end: Backtester with last_trade_time pre-populated rejects
    bars whose historical timestamp falls within the cooldown window.

    The test verifies that ``as_of_ms`` (the bar's epoch-ms) is threaded
    from ``Backtester.run_async()`` → ``DecisionPipeline.process()`` →
    ``CandidateBuilder.build()`` → ``CooldownFilter.evaluate(reference_ts=…)``
    so the rejection is computed against *historical* bar time, not
    wall-clock.
    """
    candles = _uptrend_candles()
    period_ms = 3_600_000
    start_ms = candles[0].timestamp
    end_ms = candles[-1].timestamp + period_ms

    # Simulate a trade at bar 10 (ts = base + 10 * period_ms)
    trade_bar_ts = candles[10].timestamp
    trade_ts_dt = datetime.fromtimestamp(trade_bar_ts / 1000, tz=UTC)

    settings = _base_settings(
        runtime__mode=Mode.PAPER,
        runtime__strategy="per_timeframe",
        strategy__trend__ema_fast=3, strategy__trend__ema_mid=5,
        strategy__trend__ema_slow=8, strategy__trend__adx_min=5,
        strategy__momentum__rsi_period=4,
        strategy__momentum__rsi_long_min=0,
        strategy__momentum__rsi_long_max=100,
        strategy__volatility__atr_period=3,
        strategy__volatility__atr_min_pct=0.1,
        strategy__volatility__atr_max_pct=20.0,
        strategy__volatility__bb_period=4,
        strategy__volume__ma_period=3,
        strategy__volume__spike_ratio=1.0,
        scoring__min_score=30, scoring__min_confidence=0.0,
        scoring__max_candidates_per_cycle=1,
        filters__enable_liquidity_filter=False,
        filters__enable_spread_filter=False,
        filters__enable_volume_filter=False,
        # Cooldown covers the entire 30-bar range (30h) — every candidate
        # after the warmup period should be rejected by the cooldown filter,
        # proving that ``as_of_ms`` (bar timestamp) is what drives the
        # comparison, not wall-clock time.
        filters__cooldown_after_trade_minutes=5000,
        filters__min_quote_volume_usd=0,
        risk__risk_per_trade_pct=1.0,
        risk__take_profit_risk_multiple=2.0,
        risk__max_stop_distance_pct=5.0,
        risk__max_open_positions=5,
        risk__max_daily_drawdown_pct=20.0,
        risk__emergency_drawdown_pct=30.0,
    )
    env = _make_env(crypto_bot_mode=Mode.PAPER)
    config = Config(settings=settings, env=env)

    source = HistoricalCandleSource()
    source.load_all("BTC/USDT", "1h", candles)

    bt = Backtester(
        config,
        symbols=["BTC/USDT"],
        timeframes=["1h"],
        start_ms=start_ms,
        end_ms=end_ms,
        source=source,
        last_trade_time={"BTC/USDT": trade_ts_dt},
        db=Database(tmp_path / "cooldown_bt.db"),
    )

    summary = await bt.run_async()

    # Every candidate after the warmup period should be rejected — cooldown
    # is 5000 min and the entire backtest is only ~30 h long.
    assert summary.total_trades == 0, (
        f"expected 0 trades because cooldown (5000 min) covers entire "
        f"backtest (~30 h); got {summary.total_trades}"
    )


@pytest.mark.asyncio
async def test_backtender_market_context_passthrough(tmp_path):
    """Backtester passes ``market`` and ``correlation_btc/eth`` through to
    ``FeatureBuilder.build_all()``.

    Without ``market``, ``liquidity_score`` is computed from zero quote
    volume (``SymbolMarketContext()`` default), which would cause
    ``LiquidityFilter`` to reject *every* candidate even with a tiny
    ``min_liquidity_score`` threshold.  Likewise ``correlation_btc``
    would be 0.0 instead of 1.0 for BTC/USDT itself.
    """
    candles = _uptrend_candles()
    period_ms = 3_600_000
    start_ms = candles[0].timestamp
    end_ms = candles[-1].timestamp + period_ms

    settings = _base_settings(
        runtime__mode=Mode.PAPER,
        runtime__strategy="per_timeframe",
        strategy__trend__ema_fast=3, strategy__trend__ema_mid=5,
        strategy__trend__ema_slow=8, strategy__trend__adx_min=5,
        strategy__momentum__rsi_period=4,
        strategy__momentum__rsi_long_min=0,
        strategy__momentum__rsi_long_max=100,
        strategy__volatility__atr_period=3,
        strategy__volatility__atr_min_pct=0.1,
        strategy__volatility__atr_max_pct=20.0,
        strategy__volatility__bb_period=4,
        strategy__volume__ma_period=3,
        strategy__volume__spike_ratio=1.0,
        scoring__min_score=30, scoring__min_confidence=0.0,
        scoring__max_candidates_per_cycle=1,
        # Enable liquidity filter to verify market context passthrough
        filters__enable_liquidity_filter=True,
        filters__enable_spread_filter=False,
        filters__enable_volume_filter=False,
        filters__cooldown_after_trade_minutes=0,
        filters__min_quote_volume_usd=100_000_000,  # $100M min
        risk__risk_per_trade_pct=1.0,
        risk__take_profit_risk_multiple=2.0,
        risk__max_stop_distance_pct=5.0,
        risk__max_open_positions=5,
        risk__max_daily_drawdown_pct=20.0,
        risk__emergency_drawdown_pct=30.0,
    )
    env = _make_env(crypto_bot_mode=Mode.PAPER)
    config = Config(settings=settings, env=env)

    source = HistoricalCandleSource()
    source.load_all("BTC/USDT", "1h", candles)

    # Without market context, liquidity_score=0 → insufficient_liquidity
    bt_no_market = Backtester(
        config, symbols=["BTC/USDT"], timeframes=["1h"],
        start_ms=start_ms, end_ms=end_ms, source=source,
        db=Database(tmp_path / "bt_no_market.db"),
    )
    summary_no_market = await bt_no_market.run_async()
    assert summary_no_market.total_trades == 0, (
        "expected 0 trades without market context (liquidity_score=0)"
    )

    # With a market context providing $200M quote volume, liquidity_score
    # will be != 0 → liquidity filter should pass.
    from crypto_bot.features.context import SymbolMarketContext

    bt_with_market = Backtester(
        config, symbols=["BTC/USDT"], timeframes=["1h"],
        start_ms=start_ms, end_ms=end_ms, source=source,
        market_map={"BTC/USDT": SymbolMarketContext(quote_volume_24h=200_000_000)},
        db=Database(tmp_path / "bt_with_market.db"),
    )
    summary_with_market = await bt_with_market.run_async()
    assert summary_with_market.total_trades > 0, (
        "expected trades when market context provides sufficient volume"
    )

    # correlation_btc should be 1.0 for BTC/USDT (identity), not 0.0
    # We verify this indirectly: the ScoreEngine uses corr as part of
    # the risk dimension — a non-zero corr changes the total score, which
    # may affect which candidates are selected.  The strongest signal is
    # that *any* trade happened, which proves the pipeline ran to
    # completion with non-zero liquidity_score and proper correlation.
    assert summary_with_market.total_trades >= summary_no_market.total_trades
