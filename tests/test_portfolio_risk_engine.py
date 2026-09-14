"""Tests for the portfolio risk engine: limits, reasons, volatility sizing, correlation filter."""
from __future__ import annotations

import pytest

from crypto_bot.core.enums import Mode, PortfolioRejectReason, Side
from crypto_bot.core.types import FeatureSet
from crypto_bot.portfolio import (
    CrossSectionalFeatureSnapshot,
    PortfolioIntent,
    PortfolioRiskEngine,
    PortfolioRiskLimits,
    PortfolioState,
    PositionIntent,
    UniverseSnapshot,
    VolatilitySizingParams,
)


def _intent(
    *intents: PositionIntent,
    as_of_ms: int = 1_700_000_000_000,
) -> PortfolioIntent:
    return PortfolioIntent(
        as_of_ms=as_of_ms,
        intents=intents,
        universe=(
            UniverseSnapshot(as_of_ms=as_of_ms, symbols=tuple(i.symbol for i in intents))
            if intents
            else None
        ),
        strategy_name="test",
    )


def _state(as_of_ms: int = 1_700_000_000_000) -> PortfolioState:
    return PortfolioState(as_of_ms=as_of_ms, mode=Mode.PAPER, equity=10_000.0, cash=10_000.0)


def _position(symbol: str, side: Side, weight: float, timeframe: str = "1h") -> PositionIntent:
    return PositionIntent(symbol=symbol, side=side, target_weight=weight, timeframe=timeframe)


def _feature_set(symbol: str, atr_pct: float) -> FeatureSet:
    return FeatureSet(
        symbol=symbol,
        timeframe="1h",
        trend_score=0.5,
        momentum_score=0.5,
        volatility_score=0.5,
        volume_score=0.5,
        adx=20.0,
        rsi=50.0,
        atr_pct=atr_pct,
        ema_fast=1.0,
        ema_mid=1.0,
        ema_slow=1.0,
    )


def _features(
    as_of_ms: int = 1_700_000_000_000,
    universe: tuple[str, ...] = ("BTC/USDT", "ETH/USDT"),
    atr: dict[str, float] | None = None,
) -> CrossSectionalFeatureSnapshot:
    atr = atr or {"BTC/USDT": 2.0, "ETH/USDT": 1.0}
    return CrossSectionalFeatureSnapshot(
        as_of_ms=as_of_ms,
        universe=UniverseSnapshot(as_of_ms=as_of_ms, symbols=universe),
        features_by_symbol={symbol: _feature_set(symbol, atr[symbol]) for symbol in universe},
    )


def _limits(
    max_positions: int = 10,
    max_position_weight: float = 0.5,
    max_gross_exposure: float = 1.0,
    max_net_exposure: float = 1.0,
    max_leverage: float = 10.0,
    max_correlation: float = 0.7,
    max_correlated_positions: int = 2,
    enable_correlation_filter: bool = True,
) -> PortfolioRiskLimits:
    return PortfolioRiskLimits(
        max_positions=max_positions,
        max_position_weight=max_position_weight,
        max_gross_exposure=max_gross_exposure,
        max_net_exposure=max_net_exposure,
        max_leverage=max_leverage,
        max_correlation=max_correlation,
        max_correlated_positions=max_correlated_positions,
        enable_correlation_filter=enable_correlation_filter,
    )


class TestMaxPositions:
    def test_rejects_positions_beyond_cap(self) -> None:
        intent = _intent(
            _position("BTC/USDT", Side.LONG, 0.3),
            _position("ETH/USDT", Side.LONG, 0.3),
            _position("SOL/USDT", Side.LONG, 0.3),
        )
        report = PortfolioRiskEngine(_limits(max_positions=2)).evaluate(intent, _state())

        assert not report.accepted
        assert [r.symbol for r in report.position_results if r.accepted] == ["BTC/USDT", "ETH/USDT"]
        rejected = [r for r in report.position_results if not r.accepted]
        assert len(rejected) == 1
        assert rejected[0].symbol == "SOL/USDT"
        assert rejected[0].reason == PortfolioRejectReason.REJECT_MAX_POSITIONS
        assert report.rejected_reasons == (PortfolioRejectReason.REJECT_MAX_POSITIONS,)
        assert report.gross_exposure == pytest.approx(0.6)

    def test_order_is_preserved(self) -> None:
        intent = _intent(
            _position("A/USDT", Side.LONG, 0.1),
            _position("B/USDT", Side.LONG, 0.1),
            _position("C/USDT", Side.LONG, 0.1),
        )
        report = PortfolioRiskEngine(_limits(max_positions=2)).evaluate(intent, _state())
        assert [r.symbol for r in report.position_results] == ["A/USDT", "B/USDT", "C/USDT"]

    def test_zero_cap_rejects_everything(self) -> None:
        intent = _intent(_position("BTC/USDT", Side.LONG, 0.3))
        report = PortfolioRiskEngine(_limits(max_positions=0)).evaluate(intent, _state())
        assert not report.accepted
        assert report.position_results[0].reason == PortfolioRejectReason.REJECT_MAX_POSITIONS


class TestMaxPositionWeight:
    def test_rejects_overweight_position(self) -> None:
        intent = _intent(
            _position("BTC/USDT", Side.LONG, 0.6),
            _position("ETH/USDT", Side.LONG, 0.4),
        )
        report = PortfolioRiskEngine(_limits(max_position_weight=0.5)).evaluate(intent, _state())

        assert not report.accepted
        assert report.position_results[0].reason == PortfolioRejectReason.REJECT_MAX_POSITION_WEIGHT
        assert report.position_results[1].accepted
        assert report.rejected_reasons == (PortfolioRejectReason.REJECT_MAX_POSITION_WEIGHT,)
        assert report.adjusted_intent.intents == (_position("ETH/USDT", Side.LONG, 0.4),)

    def test_weight_exactly_at_limit_is_accepted(self) -> None:
        intent = _intent(_position("BTC/USDT", Side.LONG, 0.5))
        report = PortfolioRiskEngine(_limits(max_position_weight=0.5)).evaluate(intent, _state())
        assert report.accepted


class TestMaxGrossExposure:
    def test_drops_smallest_weights_first(self) -> None:
        intent = _intent(
            _position("BTC/USDT", Side.LONG, 0.6),
            _position("ETH/USDT", Side.LONG, 0.5),
            _position("SOL/USDT", Side.LONG, 0.4),
        )
        report = PortfolioRiskEngine(
            _limits(max_position_weight=0.6, max_gross_exposure=1.0)
        ).evaluate(intent, _state())
        assert not report.accepted
        accepted = [r for r in report.position_results if r.accepted]
        rejected = [r for r in report.position_results if not r.accepted]
        assert [r.symbol for r in accepted] == ["BTC/USDT"]
        assert {r.symbol for r in rejected} == {"ETH/USDT", "SOL/USDT"}
        assert all(r.reason == PortfolioRejectReason.REJECT_MAX_GROSS_EXPOSURE for r in rejected)
        assert report.gross_exposure == pytest.approx(0.6)

    def test_short_weights_count_in_gross(self) -> None:
        intent = _intent(
            _position("BTC/USDT", Side.LONG, 0.6),
            _position("ETH/USDT", Side.SHORT, 0.6),
        )
        report = PortfolioRiskEngine(
            _limits(max_position_weight=0.6, max_gross_exposure=1.0)
        ).evaluate(intent, _state())
        rejected = [r for r in report.position_results if not r.accepted]
        assert len(rejected) == 1
        assert rejected[0].reason == PortfolioRejectReason.REJECT_MAX_GROSS_EXPOSURE
        assert report.gross_exposure == pytest.approx(0.6)


class TestMaxNetExposure:
    def test_rejects_side_that_drives_net_over_limit(self) -> None:
        intent = _intent(
            _position("BTC/USDT", Side.LONG, 0.4),
            _position("ETH/USDT", Side.LONG, 0.3),
            _position("SOL/USDT", Side.SHORT, 0.2),
        )
        report = PortfolioRiskEngine(
            _limits(max_gross_exposure=1.0, max_net_exposure=0.4)
        ).evaluate(intent, _state())

        assert not report.accepted
        accepted = [r for r in report.position_results if r.accepted]
        rejected = [r for r in report.position_results if not r.accepted]
        assert [r.symbol for r in accepted] == ["BTC/USDT", "SOL/USDT"]
        assert rejected[0].symbol == "ETH/USDT"
        assert rejected[0].reason == PortfolioRejectReason.REJECT_MAX_NET_EXPOSURE
        assert report.net_exposure == pytest.approx(0.2)
        assert report.rejected_reasons == (PortfolioRejectReason.REJECT_MAX_NET_EXPOSURE,)

    def test_short_weights_reduce_net(self) -> None:
        intent = _intent(
            _position("A/USDT", Side.SHORT, 0.4),
            _position("B/USDT", Side.SHORT, 0.4),
        )
        report = PortfolioRiskEngine(_limits(max_net_exposure=0.9)).evaluate(intent, _state())
        assert report.accepted
        assert report.net_exposure == pytest.approx(0.8)


class TestReasons:
    def test_reason_values_are_exact_strings(self) -> None:
        assert PortfolioRejectReason.REJECT_MAX_POSITION_WEIGHT == "REJECT_MAX_POSITION_WEIGHT"
        assert PortfolioRejectReason.REJECT_MAX_GROSS_EXPOSURE == "REJECT_MAX_GROSS_EXPOSURE"
        assert PortfolioRejectReason.REJECT_MAX_NET_EXPOSURE == "REJECT_MAX_NET_EXPOSURE"

    def test_first_failing_constraint_wins_per_position(self) -> None:
        intent = _intent(_position("BTC/USDT", Side.LONG, 0.9))
        report = PortfolioRiskEngine(
            _limits(max_position_weight=0.3, max_gross_exposure=1.0)
        ).evaluate(intent, _state())
        assert report.position_results[0].reason == PortfolioRejectReason.REJECT_MAX_POSITION_WEIGHT


class TestVolatilitySizing:
    def test_inverse_vol_scaling_preserves_gross(self) -> None:
        intent = _intent(
            _position("BTC/USDT", Side.LONG, 0.5),
            _position("ETH/USDT", Side.LONG, 0.5),
        )
        report = PortfolioRiskEngine(_limits(max_position_weight=1.0)).evaluate(
            intent, _state(), _features(), sizing=VolatilitySizingParams()
        )

        weights = {r.symbol: r.granted_weight for r in report.position_results}
        assert weights["ETH/USDT"] > weights["BTC/USDT"]
        assert report.gross_exposure == pytest.approx(1.0)

    def test_missing_symbol_uses_median_fallback(self) -> None:
        intent = _intent(
            _position("BTC/USDT", Side.LONG, 0.5),
            _position("SOL/USDT", Side.LONG, 0.5),
        )
        features = _features(atr={"BTC/USDT": 1.0, "ETH/USDT": 3.0})
        report = PortfolioRiskEngine(_limits()).evaluate(
            intent, _state(), features, sizing=VolatilitySizingParams()
        )
        weights = {r.symbol: r.granted_weight for r in report.position_results}
        assert weights["BTC/USDT"] == pytest.approx(weights["SOL/USDT"])
        assert report.gross_exposure == pytest.approx(1.0)

    def test_explicit_fallback_reweights_missing_symbol(self) -> None:
        intent = _intent(
            _position("BTC/USDT", Side.LONG, 0.5),
            _position("SOL/USDT", Side.LONG, 0.5),
        )
        features = _features(atr={"BTC/USDT": 1.0, "ETH/USDT": 3.0})
        report = PortfolioRiskEngine(_limits(max_position_weight=1.0)).evaluate(
            intent,
            _state(),
            features,
            sizing=VolatilitySizingParams(fallback_volatility_pct=2.0),
        )
        weights = {r.symbol: r.granted_weight for r in report.position_results}
        assert weights["BTC/USDT"] == pytest.approx(2.0 * weights["SOL/USDT"])
        assert report.gross_exposure == pytest.approx(1.0)

    def test_no_sizing_without_features(self) -> None:
        intent = _intent(
            _position("BTC/USDT", Side.LONG, 0.5),
            _position("ETH/USDT", Side.LONG, 0.5),
        )
        baseline = PortfolioRiskEngine(_limits()).evaluate(intent, _state())
        report = PortfolioRiskEngine(_limits()).evaluate(
            intent, _state(), None, sizing=VolatilitySizingParams()
        )
        assert report.adjusted_intent.intents == intent.intents
        assert report == baseline

    def test_weight_cap_respected_after_sizing(self) -> None:
        intent = _intent(
            _position("BTC/USDT", Side.LONG, 0.5),
            _position("ETH/USDT", Side.LONG, 0.5),
        )
        report = PortfolioRiskEngine(_limits(max_position_weight=0.3)).evaluate(
            intent,
            _state(),
            _features(atr={"BTC/USDT": 4.0, "ETH/USDT": 1.0}),
            sizing=VolatilitySizingParams(),
        )
        assert all(r.granted_weight <= 0.3 for r in report.position_results)
        assert report.gross_exposure <= 0.6

    def test_net_limit_rechecked_after_sizing(self) -> None:
        intent = _intent(
            _position("BTC/USDT", Side.LONG, 0.5),
            _position("ETH/USDT", Side.SHORT, 0.5),
        )
        report = PortfolioRiskEngine(
            _limits(max_gross_exposure=1.0, max_net_exposure=0.1)
        ).evaluate(
            intent,
            _state(),
            _features(atr={"BTC/USDT": 2.0, "ETH/USDT": 1.0}),
            sizing=VolatilitySizingParams(),
        )
        assert report.net_exposure <= 0.1
        assert any(
            r.reason == PortfolioRejectReason.REJECT_MAX_NET_EXPOSURE
            for r in report.position_results if not r.accepted
        )


class TestEngineContract:
    def test_empty_intent_is_valid_all_cash(self) -> None:
        intent = _intent()
        report = PortfolioRiskEngine(_limits()).evaluate(intent, _state())
        assert report.accepted
        assert report.adjusted_intent.intents == ()
        assert report.gross_exposure == 0.0
        assert report.net_exposure == 0.0

    def test_metadata_preserved_in_adjusted_intent(self) -> None:
        universe = UniverseSnapshot(as_of_ms=1_700_000_000_000, symbols=("BTC/USDT",))
        intent = PortfolioIntent(
            as_of_ms=1_700_000_000_000,
            universe=universe,
            strategy_name="alpha_1",
            intents=(_position("BTC/USDT", Side.LONG, 0.3),),
        )
        report = PortfolioRiskEngine(_limits()).evaluate(intent, _state())
        assert report.adjusted_intent.universe is universe
        assert report.adjusted_intent.strategy_name == "alpha_1"
        assert report.adjusted_intent.as_of_ms == intent.as_of_ms

    def test_position_details_preserved(self) -> None:
        intent = _intent(_position("BTC/USDT", Side.SHORT, 0.2, timeframe="4h"))
        report = PortfolioRiskEngine(_limits()).evaluate(intent, _state())
        position = report.position_results[0]
        assert position.timeframe == "4h"
        assert position.side == Side.SHORT
        assert report.adjusted_intent.intents[0].timeframe == "4h"
        assert report.adjusted_intent.intents[0].side == Side.SHORT

    @pytest.mark.parametrize(
        ("bad_intent", "bad_state"),
        [(None, None), ("x", None), (None, "x")],
    )
    def test_rejects_wrong_types(self, bad_intent, bad_state) -> None:
        engine = PortfolioRiskEngine(_limits())
        with pytest.raises(ValueError):
            engine.evaluate(bad_intent, bad_state)  # type: ignore[arg-type]

    def test_as_of_mismatch_rejected(self) -> None:
        intent = _intent(as_of_ms=100)
        state = _state(as_of_ms=200)
        with pytest.raises(ValueError, match="as_of_ms"):
            PortfolioRiskEngine(_limits()).evaluate(intent, state)

    def test_features_as_of_mismatch_rejected(self) -> None:
        intent = _intent(as_of_ms=100)
        features = _features(as_of_ms=200)
        with pytest.raises(ValueError, match="as_of_ms"):
            PortfolioRiskEngine(_limits()).evaluate(intent, _state(100), features)

    @pytest.mark.parametrize(
        "raw",
        [
            (-1, 0.5, 1.0, 1.0),
            (5, -0.1, 1.0, 1.0),
            (5, 0.5, -1.0, 1.0),
            (5, 0.5, 1.0, -1.0),
            (5, float("nan"), 1.0, 1.0),
        ],
    )
    def test_rejects_invalid_limits(self, raw: tuple[float, ...]) -> None:
        with pytest.raises(ValueError):
            PortfolioRiskLimits(*raw)  # type: ignore[arg-type]
            PortfolioRiskEngine(PortfolioRiskLimits(*raw))  # type: ignore[arg-type]

    def test_rejects_wrong_engine_args(self) -> None:
        with pytest.raises(ValueError, match="PortfolioRiskLimits"):
            PortfolioRiskEngine(object())  # type: ignore[arg-type]


class TestCorrelationFilter:
    """Tests for the leg-aware correlation filter (Task 6).

    The filter distinguishes between within-leg (same side) and cross-leg
    (opposite side) positions. It allows up to max_correlated_positions
    per side, so a long and a short can both be accepted even if they
    would be correlated, because they are naturally diversifying.
    """

    def test_allows_max_correlated_positions_per_side(self) -> None:
        """Up to max_correlated_positions on each side are accepted."""
        intent = _intent(
            _position("BTC/USDT", Side.LONG, 0.3),
            _position("ETH/USDT", Side.LONG, 0.3),
            _position("SOL/USDT", Side.SHORT, 0.3),
            _position("ADA/USDT", Side.SHORT, 0.3),
        )
        report = PortfolioRiskEngine(
            _limits(max_correlated_positions=2, enable_correlation_filter=True, max_gross_exposure=2.0)
        ).evaluate(intent, _state(), _features())

        # 2 longs + 2 shorts = all 4 accepted (per-side limit is 2)
        accepted = [r for r in report.position_results if r.accepted]
        rejected = [r for r in report.position_results if not r.accepted]
        assert len(accepted) == 4
        assert len(rejected) == 0

    def test_rejects_excess_on_same_side(self) -> None:
        """Exceeding max_correlated_positions on the SAME side rejects the extra."""
        intent = _intent(
            _position("BTC/USDT", Side.LONG, 0.3),
            _position("ETH/USDT", Side.LONG, 0.3),
            _position("SOL/USDT", Side.LONG, 0.3),
            _position("ADA/USDT", Side.SHORT, 0.3),
        )
        report = PortfolioRiskEngine(
            _limits(max_correlated_positions=2, enable_correlation_filter=True, max_gross_exposure=2.0)
        ).evaluate(intent, _state(), _features())

        accepted = [r for r in report.position_results if r.accepted]
        rejected = [r for r in report.position_results if not r.accepted]
        assert len(accepted) == 3  # 2 longs + 1 short
        assert len(rejected) == 1
        assert rejected[0].symbol in ("BTC/USDT", "ETH/USDT", "SOL/USDT")
        assert rejected[0].reason == PortfolioRejectReason.REJECT_CORRELATION

    def test_cross_leg_not_counted_together(self) -> None:
        """Cross-leg positions (long + short) do not count against each other."""
        # 2 longs + 2 shorts = 4 accepted with max_correlated_positions=2
        # because the limit is per-side (2 per side)
        intent = _intent(
            _position("BTC/USDT", Side.LONG, 0.2),
            _position("ETH/USDT", Side.LONG, 0.2),
            _position("ADA/USDT", Side.SHORT, 0.2),
            _position("DOGE/USDT", Side.SHORT, 0.2),
        )
        report = PortfolioRiskEngine(
            _limits(max_correlated_positions=2, enable_correlation_filter=True, max_gross_exposure=2.0)
        ).evaluate(intent, _state(), _features())

        accepted = [r for r in report.position_results if r.accepted]
        rejected = [r for r in report.position_results if not r.accepted]
        assert len(accepted) == 4
        assert len(rejected) == 0

    def test_preserves_order_of_intents(self) -> None:
        """First intents on each side are kept; later ones rejected if over limit."""
        intent = _intent(
            _position("A/USDT", Side.LONG, 0.2),   # 1st long - keep
            _position("B/USDT", Side.LONG, 0.2),   # 2nd long - keep
            _position("C/USDT", Side.LONG, 0.2),   # 3rd long - reject
            _position("D/USDT", Side.SHORT, 0.2),  # 1st short - keep
            _position("E/USDT", Side.SHORT, 0.2),  # 2nd short - keep
            _position("F/USDT", Side.SHORT, 0.2),  # 3rd short - reject
        )
        report = PortfolioRiskEngine(
            _limits(max_correlated_positions=2, enable_correlation_filter=True, max_gross_exposure=2.0)
        ).evaluate(intent, _state(), _features())

        accepted_symbols = [r.symbol for r in report.position_results if r.accepted]
        rejected_symbols = [r.symbol for r in report.position_results if not r.accepted]
        assert accepted_symbols == ["A/USDT", "B/USDT", "D/USDT", "E/USDT"]
        assert rejected_symbols == ["C/USDT", "F/USDT"]

    def test_disabled_filter_accepts_all(self) -> None:
        """When enable_correlation_filter=False, all positions accepted."""
        intent = _intent(
            _position("BTC/USDT", Side.LONG, 0.3),
            _position("ETH/USDT", Side.LONG, 0.3),
            _position("SOL/USDT", Side.LONG, 0.3),
            _position("ADA/USDT", Side.SHORT, 0.3),
            _position("DOGE/USDT", Side.SHORT, 0.3),
            _position("XRP/USDT", Side.SHORT, 0.3),
        )
        report = PortfolioRiskEngine(
            _limits(max_correlated_positions=1, enable_correlation_filter=False, max_gross_exposure=2.0)
        ).evaluate(intent, _state(), _features())

        assert report.accepted
        assert len(report.position_results) == 6
        assert all(r.accepted for r in report.position_results)

    def test_no_features_bypasses_filter(self) -> None:
        """Without features, filter is bypassed (conservative)."""
        intent = _intent(
            _position("BTC/USDT", Side.LONG, 0.3),
            _position("ETH/USDT", Side.LONG, 0.3),
            _position("SOL/USDT", Side.LONG, 0.3),
        )
        report = PortfolioRiskEngine(
            _limits(max_correlated_positions=1, enable_correlation_filter=True, max_gross_exposure=2.0)
        ).evaluate(intent, _state(), None)  # No features

        # Should bypass and accept all
        accepted = [r for r in report.position_results if r.accepted]
        assert len(accepted) == 3