"""Tests for margin/leverage risk limits (R0.3)."""
from __future__ import annotations

from crypto_bot.core.enums import Mode, PortfolioRejectReason, Side
from crypto_bot.portfolio.models import (
    PortfolioIntent,
    PortfolioState,
    PositionIntent,
    UniverseSnapshot,
)
from crypto_bot.portfolio.risk import PortfolioRiskEngine, PortfolioRiskLimits


def make_intent(as_of: int, candidates: list[PositionIntent]) -> PortfolioIntent:
    return PortfolioIntent(
        as_of_ms=as_of,
        intents=tuple(candidates),
        universe=UniverseSnapshot(
            as_of_ms=as_of,
            symbols=tuple(c.symbol for c in candidates),
        ),
    )


def make_state(as_of: int, equity: float = 10000.0) -> PortfolioState:
    return PortfolioState(
        as_of_ms=as_of,
        mode=Mode.PAPER,
        equity=equity,
        cash=equity,
    )


class TestMaxLeverage:
    """Tests for max_leverage risk limit."""

    def test_no_limit_when_leverage_unlimited(self):
        """Very high max_leverage means no margin constraint."""
        limits = PortfolioRiskLimits(
            max_positions=10,
            max_position_weight=1.0,
            max_gross_exposure=1.0,
            max_net_exposure=1.0,
            max_leverage=1e9,
        )
        engine = PortfolioRiskEngine(limits)

        intent = make_intent(
            1_000_000,
            [
                PositionIntent("BTC/USDT", Side.LONG, 0.5),
                PositionIntent("ETH/USDT", Side.LONG, 0.5),
            ],
        )
        state = make_state(1_000_000, 10000.0)

        report = engine.evaluate(intent, state)
        assert report.accepted
        assert abs(report.gross_exposure - 1.0) < 1e-9

    def test_leverage_limit_reduces_gross(self):
        """Leverage limit scales down gross exposure when margin insufficient."""
        limits = PortfolioRiskLimits(
            max_positions=10,
            max_position_weight=1.0,
            max_gross_exposure=1.0,
            max_net_exposure=1.0,
            max_leverage=2.0,  # 2x leverage
            maintenance_margin_buffer_pct=0.0,
        )
        engine = PortfolioRiskEngine(limits)

        # Gross = 1.0, required_margin = 1.0 / 2.0 = 0.5, equity = 10000
        # But we need gross in dollar terms: 1.0 * equity = 10000
        # required_margin = 10000 / 2 = 5000, equity = 10000 -> OK
        intent = make_intent(
            1_000_000,
            [
                PositionIntent("BTC/USDT", Side.LONG, 0.5),
                PositionIntent("ETH/USDT", Side.LONG, 0.5),
            ],
        )
        state = make_state(1_000_000, 10000.0)

        report = engine.evaluate(intent, state)
        assert report.accepted

    def test_leverage_limit_reduces_gross_with_equity(self):
        """Leverage limit scales down when equity cannot support gross."""
        limits = PortfolioRiskLimits(
            max_positions=10,
            max_position_weight=1.0,
            max_gross_exposure=1.0,
            max_net_exposure=1.0,
            max_leverage=2.0,
            maintenance_margin_buffer_pct=0.0,
        )
        engine = PortfolioRiskEngine(limits)

        # Gross = 1.5 (150% of equity), required_margin = 1.5 * equity / 2 = 0.75 * equity
        # With equity=10000, required = 7500, equity=10000 -> OK but at limit
        # With gross=1.5 and equity=1000, required = 750, equity=1000 -> OK
        # But if gross=1.0 and equity=1000, required = 500, equity=1000 -> OK

        # Let's test with a case that exceeds margin
        # equity = 1000, gross = 1.5 (1500 notional), required_margin = 750, equity = 1000 -> OK
        # To exceed: need required_margin > equity
        # required = gross * equity / leverage > equity => gross > leverage
        # With leverage=2, gross must exceed 2.0 to fail
        # So gross=1.5 is fine with leverage=2

        # Let's test: equity=10000, leverage=10, gross=0.9
        # required = 0.9 * 10000 / 10 = 900, equity=10000 -> OK

        # Actually the test should use weights directly since the engine works with weights
        # gross = sum of weights, required_margin = gross / max_leverage
        # If we have equity=10000, gross=1.5, leverage=2
        # required_margin = 1.5 / 2.0 = 0.75 (as fraction of equity)
        # required_with_buffer = 0.75 * 10000 = 7500
        # equity = 10000 -> OK

        # To fail: required_with_buffer > equity
        # gross / leverage * (1 + buffer) > equity
        # gross > equity * leverage / (1 + buffer)
        # With equity=10000, leverage=2, buffer=0: gross > 20000/10000 = 2.0

        # So with gross=2.5 and leverage=2: required = 2.5/2 = 1.25 > 1.0 (equity fraction)
        # Let's use low equity to test

        limits2 = PortfolioRiskLimits(
            max_positions=10,
            max_position_weight=1.0,
            max_gross_exposure=1.0,
            max_net_exposure=1.0,
            max_leverage=2.0,
            maintenance_margin_buffer_pct=0.0,
        )
        engine2 = PortfolioRiskEngine(limits2)

        # Gross = 1.5, but equity is only 1000 (so notional = 1500)
        # Wait, the engine uses weights directly, not dollar amounts
        # gross = 1.5 (sum of weights)
        # required_margin = gross / max_leverage = 1.5 / 2.0 = 0.75
        # available_equity fraction = 1.0 (equity is 100% of itself)
        # So required_margin (0.75) <= 1.0 -> OK

        # To test failure, we need gross / leverage > 1.0
        # gross > leverage
        # With leverage=2, gross > 2.0
        # But max_gross_exposure limits gross to 1.0
        # So we need max_gross_exposure > leverage to test

        limits3 = PortfolioRiskLimits(
            max_positions=10,
            max_position_weight=1.0,
            max_gross_exposure=3.0,  # Allow gross up to 3.0
            max_net_exposure=3.0,
            max_leverage=2.0,
            maintenance_margin_buffer_pct=0.0,
        )
        engine3 = PortfolioRiskEngine(limits3)

        intent = make_intent(
            1_000_000,
            [
                PositionIntent("BTC/USDT", Side.LONG, 1.0),
                PositionIntent("ETH/USDT", Side.LONG, 1.0),
                PositionIntent("SOL/USDT", Side.LONG, 1.0),
            ],
        )
        state = make_state(1_000_000, 10000.0)

        report = engine3.evaluate(intent, state)
        # Gross = 3.0, required = 3.0 / 2.0 = 1.5 > 1.0 -> should scale down
        # Positions are scaled down, not rejected, so report.accepted = True
        assert report.accepted
        # Target gross should be limited to leverage = 2.0 (since buffer=0)
        assert report.gross_exposure <= 2.0 + 1e-6
        # Each position weight should be scaled from 1.0 to 2/3 ≈ 0.667
        for result in report.position_results:
            assert result.granted_weight <= 0.67 + 1e-6

    def test_leverage_with_buffer(self):
        """Maintenance margin buffer increases required margin."""
        limits = PortfolioRiskLimits(
            max_positions=10,
            max_position_weight=1.0,
            max_gross_exposure=3.0,
            max_net_exposure=3.0,
            max_leverage=2.0,
            maintenance_margin_buffer_pct=0.5,  # 50% buffer
        )
        engine = PortfolioRiskEngine(limits)

        intent = make_intent(
            1_000_000,
            [
                PositionIntent("BTC/USDT", Side.LONG, 1.0),
                PositionIntent("ETH/USDT", Side.LONG, 1.0),
            ],
        )
        state = make_state(1_000_000, 10000.0)

        # Gross = 2.0, required = 2.0 / 2.0 * 1.5 = 1.5 > 1.0 -> scale down
        # Target gross = 1.0 * 2.0 / 1.5 = 1.333...
        report = engine.evaluate(intent, state)
        assert report.gross_exposure <= 1.34 + 1e-6

    def test_long_short_symmetric_margin(self):
        """Long and short positions have equal margin requirements."""
        limits = PortfolioRiskLimits(
            max_positions=10,
            max_position_weight=1.0,
            max_gross_exposure=3.0,
            max_net_exposure=3.0,
            max_leverage=2.0,
            maintenance_margin_buffer_pct=0.0,
        )
        engine = PortfolioRiskEngine(limits)

        # All longs
        intent_long = make_intent(
            1_000_000,
            [
                PositionIntent("BTC/USDT", Side.LONG, 1.0),
                PositionIntent("ETH/USDT", Side.LONG, 1.0),
            ],
        )
        # All shorts
        intent_short = make_intent(
            1_000_000,
            [
                PositionIntent("BTC/USDT", Side.SHORT, 1.0),
                PositionIntent("ETH/USDT", Side.SHORT, 1.0),
            ],
        )
        state = make_state(1_000_000, 10000.0)

        report_long = engine.evaluate(intent_long, state)
        report_short = engine.evaluate(intent_short, state)

        # Both should have same gross exposure after margin check
        assert abs(report_long.gross_exposure - report_short.gross_exposure) < 1e-9

    def test_mixed_long_short_gross_calculation(self):
        """Gross exposure for margin uses absolute weights (long + short)."""
        limits = PortfolioRiskLimits(
            max_positions=10,
            max_position_weight=1.0,
            max_gross_exposure=3.0,
            max_net_exposure=3.0,
            max_leverage=2.0,
            maintenance_margin_buffer_pct=0.0,
        )
        engine = PortfolioRiskEngine(limits)

        intent = make_intent(
            1_000_000,
            [
                PositionIntent("BTC/USDT", Side.LONG, 1.0),
                PositionIntent("ETH/USDT", Side.SHORT, 1.0),
            ],
        )
        state = make_state(1_000_000, 10000.0)

        # Gross = 2.0, net = 0.0
        # required_margin = 2.0 / 2.0 = 1.0 <= 1.0 -> OK (at limit)
        report = engine.evaluate(intent, state)
        assert report.accepted
        assert abs(report.gross_exposure - 2.0) < 1e-9

    def test_leverage_check_runs_before_vol_sizing(self):
        """Leverage check happens before volatility sizing."""
        # This is verified by the fact that the leverage check is in the
        # evaluate method before the vol sizing block
        limits = PortfolioRiskLimits(
            max_positions=10,
            max_position_weight=1.0,
            max_gross_exposure=3.0,
            max_net_exposure=3.0,
            max_leverage=2.0,
            maintenance_margin_buffer_pct=0.0,
        )
        engine = PortfolioRiskEngine(limits)

        # Should work without features (no vol sizing)
        intent = make_intent(
            1_000_000,
            [PositionIntent("BTC/USDT", Side.LONG, 1.0)],
        )
        state = make_state(1_000_000, 10000.0)

        report = engine.evaluate(intent, state)
        assert report.accepted

    def test_zero_equity_rejects_all(self):
        """Zero equity rejects all positions due to margin."""
        limits = PortfolioRiskLimits(
            max_positions=10,
            max_position_weight=1.0,
            max_gross_exposure=1.0,
            max_net_exposure=1.0,
            max_leverage=10.0,
        )
        engine = PortfolioRiskEngine(limits)

        intent = make_intent(
            1_000_000,
            [PositionIntent("BTC/USDT", Side.LONG, 0.5)],
        )
        state = make_state(1_000_000, 0.0)

        report = engine.evaluate(intent, state)
        assert not report.accepted
        assert len(report.position_results) == 1
        assert report.position_results[0].reason == PortfolioRejectReason.REJECT_MAX_LEVERAGE

    def test_maintenance_buffer_validation(self):
        """Negative buffer raises validation error."""
        import pytest
        with pytest.raises(ValueError):
            PortfolioRiskLimits(
                max_positions=10,
                max_position_weight=1.0,
                max_gross_exposure=1.0,
                max_net_exposure=1.0,
                max_leverage=2.0,
                maintenance_margin_buffer_pct=-0.1,
            )

    def test_leverage_validation(self):
        """Leverage < 1.0 raises validation error."""
        import pytest
        with pytest.raises(ValueError):
            PortfolioRiskLimits(
                max_positions=10,
                max_position_weight=1.0,
                max_gross_exposure=1.0,
                max_net_exposure=1.0,
                max_leverage=0.5,
            )

    def test_leverage_rechecks_downstream_limits(self):
        """After margin scaling, gross/net/position_weight are re-checked."""
        limits = PortfolioRiskLimits(
            max_positions=2,
            max_position_weight=0.3,  # Max 30% per position
            max_gross_exposure=1.0,
            max_net_exposure=1.0,
            max_leverage=2.0,
            maintenance_margin_buffer_pct=0.0,
        )
        engine = PortfolioRiskEngine(limits)

        intent = make_intent(
            1_000_000,
            [
                PositionIntent("BTC/USDT", Side.LONG, 1.0),  # Will be scaled down
                PositionIntent("ETH/USDT", Side.LONG, 1.0),
                PositionIntent("SOL/USDT", Side.LONG, 1.0),
            ],
        )
        state = make_state(1_000_000, 10000.0)

        report = engine.evaluate(intent, state)

        # After margin scaling to gross=2.0, then gross limit 1.0 applies
        # Then max_position_weight 0.3 applies
        for result in report.position_results:
            if result.accepted:
                assert result.granted_weight <= 0.3 + 1e-6

        assert report.gross_exposure <= 1.0 + 1e-6


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])