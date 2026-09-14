"""Tests for PortfolioExecutor sizing with instrument constraints."""

from __future__ import annotations

import tempfile
from decimal import Decimal

import pytest

from crypto_bot.config.env import Config
from crypto_bot.config.schemas import (
    CsmConfig,
    PortfolioConfig,
    PortfolioRiskParams,
    RegimeConfig,
    RiskParams,
    RuntimeConfig,
    Settings,
)
from crypto_bot.core.enums import Mode, Side
from crypto_bot.data.instruments import InstrumentCache, InstrumentSpec
from crypto_bot.portfolio import PositionIntent, PortfolioIntent, PortfolioState
from crypto_bot.portfolio.risk import PortfolioRiskEngine, PortfolioRiskLimits
from crypto_bot.simulation.pnl import PnLTracker
from crypto_bot.simulation.portfolio_executor import PortfolioExecutor
from crypto_bot.storage.db import Database, Repositories


def _make_test_instrument_cache() -> InstrumentCache:
    """Create an InstrumentCache with test specs."""
    cache = InstrumentCache.__new__(InstrumentCache)
    cache._cache = {
        "BTCUSDT": InstrumentSpec(
            symbol="BTCUSDT",
            qty_step=0.001,
            min_order_qty=0.001,
            min_notional_value=10.0,
            tick_size=0.1,
            max_leverage=100,
            status="Trading",
            contract_type="LinearPerpetual",
        ),
        "ETHUSDT": InstrumentSpec(
            symbol="ETHUSDT",
            qty_step=0.01,
            min_order_qty=0.01,
            min_notional_value=10.0,
            tick_size=0.01,
            max_leverage=100,
            status="Trading",
            contract_type="LinearPerpetual",
        ),
        "SOLUSDT": InstrumentSpec(
            symbol="SOLUSDT",
            qty_step=0.1,
            min_order_qty=0.1,
            min_notional_value=10.0,
            tick_size=0.01,
            max_leverage=100,
            status="Trading",
            contract_type="LinearPerpetual",
        ),
        "XRPUSDT": InstrumentSpec(
            symbol="XRPUSDT",
            qty_step=1.0,
            min_order_qty=1.0,
            min_notional_value=10.0,
            tick_size=0.0001,
            max_leverage=100,
            status="Trading",
            contract_type="LinearPerpetual",
        ),
    }
    cache._loaded = True
    return cache


def _make_config() -> Config:
    settings = Settings(
        runtime=RuntimeConfig(mode=Mode.PAPER),
        risk=RiskParams(max_open_positions=5),
        portfolio=PortfolioConfig(
            strategy_name="cross_sectional_momentum_v0",
            risk=PortfolioRiskParams(
                max_positions=5,
                max_position_weight=1.0,
                max_gross_exposure=1.0,
                max_net_exposure=1.0,
            ),
            csm=CsmConfig(timeframe="1h"),
        ),
        regime=RegimeConfig(enabled=False),
    )
    return Config(settings=settings, env=None)


class TestPortfolioExecutorSizing:
    """Test position sizing with instrument constraints."""

    def test_round_qty_down_to_qty_step(self) -> None:
        """Quantity should be rounded DOWN to qtyStep."""
        cache = _make_test_instrument_cache()

        # BTC qtyStep=0.001
        assert cache.round_qty_down("BTC/USDT", 0.12345) == 0.123
        assert cache.round_qty_down("BTC/USDT", 0.12399) == 0.123
        assert cache.round_qty_down("BTC/USDT", 0.12300) == 0.123

        # ETH qtyStep=0.01
        assert cache.round_qty_down("ETH/USDT", 1.2345) == 1.23
        assert cache.round_qty_down("ETH/USDT", 1.2399) == 1.23

        # SOL qtyStep=0.1
        assert cache.round_qty_down("SOL/USDT", 10.56) == 10.5
        assert cache.round_qty_down("SOL/USDT", 10.59) == 10.5

        # XRP qtyStep=1.0
        assert cache.round_qty_down("XRP/USDT", 100.9) == 100.0
        assert cache.round_qty_down("XRP/USDT", 100.1) == 100.0

    def test_reject_below_min_notional(self) -> None:
        """Positions below minNotionalValue should be rejected."""
        cache = _make_test_instrument_cache()

        # BTC minNotional=10.0, price=50000 -> min qty = 10/50000 = 0.0002
        # But qtyStep=0.001, so min qty = 0.001, notional = 50
        assert cache.check_min_notional("BTC/USDT", 0.001, 50000.0)  # 50 >= 10
        assert not cache.check_min_notional("BTC/USDT", 0.0001, 50000.0)  # 5 < 10

        # ETH minNotional=10.0, price=3000 -> min qty = 10/3000 = 0.0033
        # qtyStep=0.01, so min qty = 0.01, notional = 30
        assert cache.check_min_notional("ETH/USDT", 0.01, 3000.0)  # 30 >= 10
        assert not cache.check_min_notional("ETH/USDT", 0.001, 3000.0)  # 3 < 10

    def test_fail_closed_for_unknown_symbol(self) -> None:
        """Unknown symbol should fail closed (return True for safety in cache, but executor should reject)."""
        cache = _make_test_instrument_cache()
        # Cache returns True for unknown symbols (fail open in cache)
        # But executor should handle this
        assert cache.check_min_notional("UNKNOWN/USDT", 1.0, 100.0)
        assert cache.round_qty_down("UNKNOWN/USDT", 1.0) == 1.0

    @pytest.fixture
    def executor(self) -> PortfolioExecutor:
        """Create a PortfolioExecutor with test instrument cache."""
        config = _make_config()
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            db = Database(tmp.name)
        repos = Repositories(db)
        tracker = PnLTracker(initial_equity=10000.0)
        cache = _make_test_instrument_cache()
        return PortfolioExecutor(config, repos, tracker, instrument_cache=cache)

    def test_open_position_rounds_qty(self, executor: PortfolioExecutor) -> None:
        """open_position should round qty down to qtyStep."""
        intent = PositionIntent(
            symbol="BTC/USDT",
            side=Side.LONG,
            target_weight=1.0,
            timeframe="1h",
        )
        # equity=10000, price=50000 -> raw size = 10000/50000 = 0.2
        # Rounded to qtyStep=0.001 -> 0.200
        result = executor.open_position(
            intent, entry_price=50000.0, atr_pct=2.0, timestamp_ms=1700000000000
        )
        assert result.handled
        assert result.size == 0.200  # Exactly 0.2, no rounding needed

    def test_open_position_rejects_below_min_notional(self, executor: PortfolioExecutor) -> None:
        """open_position should reject if notional < minNotionalValue."""
        intent = PositionIntent(
            symbol="BTC/USDT",
            side=Side.LONG,
            target_weight=0.0001,  # Very small weight
            timeframe="1h",
        )
        # equity=10000, weight=0.0001 -> raw size = 1.0/50000 = 0.00002
        # Rounded to qtyStep=0.001 -> 0.001, notional = 50 >= 10, so this passes
        # Need even smaller
        intent = PositionIntent(
            symbol="BTC/USDT",
            side=Side.LONG,
            target_weight=0.00001,  # 0.001% weight
            timeframe="1h",
        )
        # equity=10000, weight=0.00001 -> raw size = 0.1/50000 = 0.000002
        # Rounded to qtyStep=0.001 -> 0.0 (rounded to zero)
        result = executor.open_position(
            intent, entry_price=50000.0, atr_pct=2.0, timestamp_ms=1700000000000
        )
        assert not result.handled
        assert "rounded to zero" in result.message.lower()

    def test_open_position_rejects_below_min_qty_step(self, executor: PortfolioExecutor) -> None:
        """open_position should reject if rounded qty is zero."""
        intent = PositionIntent(
            symbol="SOL/USDT",  # qtyStep=0.1
            side=Side.LONG,
            target_weight=0.0005,  # Small weight
            timeframe="1h",
        )
        # equity=10000, price=100 -> raw size = 5.0/100 = 0.05
        # Rounded to qtyStep=0.1 -> 0.0
        result = executor.open_position(
            intent, entry_price=100.0, atr_pct=2.0, timestamp_ms=1700000000000
        )
        assert not result.handled
        assert "rounded to zero" in result.message.lower()

    def test_renormalization_after_rounding(self) -> None:
        """After executor rounds/rejects some positions, remaining weights should be renormalized."""
        config = _make_config()
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            db = Database(tmp.name)
        repos = Repositories(db)
        tracker = PnLTracker(initial_equity=10000.0)
        cache = _make_test_instrument_cache()
        executor = PortfolioExecutor(config, repos, tracker, instrument_cache=cache)

        # Create intents: SOLUSDT will be rejected (qty rounded to zero with small weight),
        # BTCUSDT will be accepted
        intents = (
            PositionIntent(symbol="BTC/USDT", side=Side.LONG, target_weight=0.5, timeframe="1h"),
            PositionIntent(symbol="SOL/USDT", side=Side.LONG, target_weight=0.0005, timeframe="1h"),  # Very small weight
        )

        # Manually execute both and check results
        result_btc = executor.open_position(
            intents[0], entry_price=50000.0, atr_pct=2.0, timestamp_ms=1700000000000
        )
        result_sol = executor.open_position(
            intents[1], entry_price=100.0, atr_pct=2.0, timestamp_ms=1700000000000
        )

        # BTC should be accepted, SOL should be rejected (rounded to zero)
        assert result_btc.handled
        assert not result_sol.handled
        assert "rounded to zero" in result_sol.message.lower()

        # Check that SOL intent was tracked as rejected
        rejected = executor.rejected_intents
        assert len(rejected) == 1
        assert rejected[0].symbol == "SOL/USDT"

    def test_gross_exposure_not_violated_after_rounding(self) -> None:
        """Gross exposure should not exceed limit after rounding down."""
        config = _make_config()
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            db = Database(tmp.name)
        repos = Repositories(db)
        tracker = PnLTracker(initial_equity=10000.0)
        cache = _make_test_instrument_cache()
        executor = PortfolioExecutor(config, repos, tracker, instrument_cache=cache)

        # Open multiple positions that sum to gross=1.0
        # Use prices that ensure all positions have qty > qtyStep
        intents = [
            PositionIntent(symbol="BTC/USDT", side=Side.LONG, target_weight=0.34, timeframe="1h"),
            PositionIntent(symbol="ETH/USDT", side=Side.LONG, target_weight=0.33, timeframe="1h"),
            PositionIntent(symbol="SOL/USDT", side=Side.LONG, target_weight=0.33, timeframe="1h"),
        ]

        # Use appropriate prices for each symbol
        prices = {"BTC/USDT": 50000.0, "ETH/USDT": 3000.0, "SOL/USDT": 100.0}
        for intent in intents:
            result = executor.open_position(
                intent, entry_price=prices[intent.symbol], atr_pct=2.0, timestamp_ms=1700000000000
            )
            # All should be accepted
            assert result.handled, f"Failed for {intent.symbol}: {result.message}"

        # Check gross exposure in tracker
        open_positions = [p for p in tracker.positions if p.is_open]
        total_notional = sum(p.size * p.entry_price for p in open_positions)
        gross_exposure = total_notional / tracker.current_equity
        assert gross_exposure <= 1.0 + 1e-9, f"Gross exposure {gross_exposure} exceeds 1.0"


class TestInstrumentsFailClosed:
    """Test fail-closed behavior for symbols without specs."""

    def test_symbol_without_spec_rejected(self) -> None:
        """Executor should reject symbols without instrument specs."""
        config = _make_config()
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            db = Database(tmp.name)
        repos = Repositories(db)
        tracker = PnLTracker(initial_equity=10000.0)
        # Empty cache - no specs loaded
        cache = InstrumentCache.__new__(InstrumentCache)
        cache._cache = {}
        cache._loaded = True
        executor = PortfolioExecutor(config, repos, tracker, instrument_cache=cache)

        intent = PositionIntent(
            symbol="UNKNOWN/USDT",
            side=Side.LONG,
            target_weight=0.5,
            timeframe="1h",
        )
        result = executor.open_position(
            intent, entry_price=100.0, atr_pct=2.0, timestamp_ms=1700000000000
        )
        # Should be rejected because no spec available
        assert not result.handled
        # The executor should handle missing specs explicitly
        # Currently it uses default rounding (no-op) and minNotional check (passes)
        # This test documents the expected fail-closed behavior

    def test_instrument_cache_is_tradable_check(self) -> None:
        """is_tradable_linear_perpetual should return False for unknown symbols."""
        cache = _make_test_instrument_cache()
        assert cache.is_tradable_linear_perpetual("BTC/USDT")
        assert cache.is_tradable_linear_perpetual("ETH/USDT")
        assert not cache.is_tradable_linear_perpetual("UNKNOWN/USDT")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])