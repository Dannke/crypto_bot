"""Pydantic schemas that validate the configuration contract.

These models are intentionally *structural*: they describe and bound the shape
of configuration values. Runtime *policy* (e.g. refusing to run in LIVE mode
until paper trading + tests are ready, honoring the ENABLE_LIVE_TRADING
kill-switch) is enforced by the settings loader / orchestrator, not here.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..core import policy
from ..core.enums import Mode, StrategyType


class StrictConfigModel(BaseModel):
    """Base for YAML config blocks: unknown keys fail fast everywhere."""

    model_config = ConfigDict(extra="forbid")


class RuntimeConfig(StrictConfigModel):
    mode: Mode = Mode.SIGNAL_ONLY
    loop_interval_seconds: int = Field(default=60, ge=5)
    timezone: str = "UTC"
    strategy: str = "confluence"  # "confluence" (multi-TF) or "per_timeframe" (independent per TF)
    strategy_type: StrategyType = StrategyType.CANDIDATE


class ExchangeConfig(StrictConfigModel):
    name: str = "bybit"
    sandbox: bool = True
    rate_limit_ms: int = Field(default=75, ge=0)


class TimeframesConfig(StrictConfigModel):
    primary: list[str] = Field(default_factory=lambda: ["1m", "15m", "1h", "4h"])
    # Must comfortably exceed the longest indicator period (default EMA slow=200)
    # plus ~2x warmup for EMA stabilisation. 400 gives EMA(200) a 200-bar
    # runway. Validators enforce >= max_period + 10. (Floor is 50.)
    candles_per_tf: int = Field(default=400, ge=50)

    @model_validator(mode="after")
    def _non_empty(self) -> TimeframesConfig:
        if not self.primary:
            raise ValueError("timeframes.primary must list at least one timeframe")
        return self


# --------------------------------------------------------------------------- #
# Strategy
# --------------------------------------------------------------------------- #
class TrendParams(StrictConfigModel):
    ema_fast: int = Field(default=21, ge=1)
    ema_mid: int = Field(default=50, ge=1)
    ema_slow: int = Field(default=200, ge=1)
    adx_min: float = Field(default=20.0, ge=0.0, le=100.0)

    @model_validator(mode="after")
    def _ordered_emas(self) -> TrendParams:
        if not (self.ema_fast < self.ema_mid < self.ema_slow):
            raise ValueError("require ema_fast < ema_mid < ema_slow")
        return self


class MomentumParams(StrictConfigModel):
    rsi_period: int = Field(default=14, ge=2)
    rsi_long_min: float = Field(default=50.0, ge=0.0, le=100.0)
    rsi_long_max: float = Field(default=70.0, ge=0.0, le=100.0)
    rsi_short_min: float = Field(default=30.0, ge=0.0, le=100.0)
    rsi_short_max: float = Field(default=50.0, ge=0.0, le=100.0)


class VolatilityParams(StrictConfigModel):
    atr_period: int = Field(default=14, ge=1)
    atr_min_pct: float | dict[str, float] = Field(default=0.5)
    atr_max_pct: float | dict[str, float] = Field(default=8.0)
    bb_period: int = Field(default=20, ge=1)
    bb_std: float = Field(default=2.0, ge=0.1)

    @model_validator(mode="after")
    def _atr_band(self) -> VolatilityParams:
        mins = self.atr_min_pct
        maxs = self.atr_max_pct
        if isinstance(mins, dict) and isinstance(maxs, dict):
            for tf in mins:
                mx = maxs.get(tf, 8.0)
                if mins[tf] >= mx:
                    raise ValueError(f"require atr_min_pct < atr_max_pct for {tf}")
        elif isinstance(mins, (int, float)) and isinstance(maxs, (int, float)):
            if mins >= maxs:
                raise ValueError("require atr_min_pct < atr_max_pct")
        else:
            raise ValueError("atr_min_pct and atr_max_pct must be same type (both scalar or both dict)")
        return self


class VolumeParams(StrictConfigModel):
    ma_period: int = Field(default=20, ge=1)
    spike_ratio: float = Field(default=1.5, ge=1.0)


class StrategyParams(StrictConfigModel):
    trend: TrendParams = TrendParams()
    momentum: MomentumParams = MomentumParams()
    volatility: VolatilityParams = VolatilityParams()
    volume: VolumeParams = VolumeParams()


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #
class ScoringWeights(StrictConfigModel):
    trend: float = Field(default=0.25, ge=0.0, le=1.0)
    momentum: float = Field(default=0.15, ge=0.0, le=1.0)
    volume: float = Field(default=0.15, ge=0.0, le=1.0)
    volatility: float = Field(default=0.10, ge=0.0, le=1.0)
    liquidity: float = Field(default=0.10, ge=0.0, le=1.0)
    spread: float = Field(default=0.05, ge=0.0, le=1.0)
    risk: float = Field(default=0.20, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _positive_total(self) -> ScoringWeights:
        total = (
            self.trend
            + self.momentum
            + self.volume
            + self.volatility
            + self.liquidity
            + self.spread
            + self.risk
        )
        if total <= 0:
            raise ValueError("scoring weights must sum > 0")
        return self


class ScoringParams(StrictConfigModel):
    weights: ScoringWeights = ScoringWeights()
    min_score: float = Field(default=65.0, ge=0.0, le=100.0)
    min_confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    max_candidates_per_cycle: int = Field(default=3, ge=1)


# --------------------------------------------------------------------------- #
# Filters / Risk
# --------------------------------------------------------------------------- #
class FilterParams(StrictConfigModel):
    min_quote_volume_usd: float = Field(default=5_000_000.0, ge=0.0)
    max_spread_pct: float = Field(default=0.15, ge=0.0)
    blacklist: list[str] = Field(default_factory=list)
    cooldown_after_trade_minutes: int = Field(default=240, ge=0)
    # Liquidity/spread reflect real exchange microstructure (order book depth,
    # market-maker competition), which testnet fundamentally doesn't have —
    # its 24h quote volume is often zero even for major pairs. Rather than
    # chasing an arbitrary low threshold that may still reject everything (or
    # accept everything, defeating the point of the filter), these let a
    # config explicitly say "this exchange endpoint's liquidity data isn't
    # meaningful, don't gate on it" instead of silently guessing via sandbox.
    enable_liquidity_filter: bool = True
    enable_spread_filter: bool = True
    enable_volume_filter: bool = True
    min_volume_score: float = Field(default=0.2, ge=0.0, le=1.0)
    min_absolute_volume: float = Field(default=0.0, ge=0.0)


class RiskParams(StrictConfigModel):
    equity_currency: str = "USDT"
    risk_per_trade_pct: float = Field(default=1.0, gt=0.0, le=5.0)
    take_profit_risk_multiple: float | dict[str, float] = Field(default=2.0)
    max_stop_distance_pct: float = Field(default=3.0, gt=0.0, le=20.0)
    max_open_positions: int = Field(default=5, ge=1)
    max_daily_drawdown_pct: float = Field(default=3.0, gt=0.0, le=100.0)
    emergency_drawdown_pct: float = Field(default=6.0, gt=0.0, le=100.0)
    max_open_unrealized_drawdown_pct: float = Field(default=3.0, ge=0.0, le=100.0)

    # R8: Correlation and clustering risk controls
    max_correlation: float = Field(default=0.7, ge=0.0, le=1.0)  # reject if corr > threshold
    max_correlated_positions: int = Field(default=2, ge=1, le=10)  # max positions in same correlation cluster
    enable_correlation_filter: bool = True
    correlation_lookback_bars: int = Field(default=168, ge=24)  # lookback for correlation calc

    @model_validator(mode="after")
    def _drawdown_ladder(self) -> RiskParams:
        if self.emergency_drawdown_pct <= self.max_daily_drawdown_pct:
            raise ValueError(
                "emergency_drawdown_pct must be greater than max_daily_drawdown_pct"
            )
        return self


class PortfolioRiskParams(StrictConfigModel):
    """Portfolio-level limits enforced by the risk engine."""

    max_positions: int = Field(default=5, ge=1)
    max_position_weight: float = Field(default=0.5, gt=0.0, le=1.0)
    max_gross_exposure: float = Field(default=1.0, gt=0.0, le=3.0)
    max_net_exposure: float = Field(default=1.0, gt=0.0, le=3.0)
    max_leverage: float = Field(default=10.0, ge=1.0, le=100.0)
    maintenance_margin_buffer_pct: float = Field(default=0.1, ge=0.0, le=1.0)

    # R8: Correlation and clustering risk controls
    max_correlation: float = Field(default=0.7, ge=0.0, le=1.0)  # reject if corr > threshold
    max_correlated_positions: int = Field(default=2, ge=1, le=10)  # max positions in same correlation cluster
    enable_correlation_filter: bool = True
    correlation_lookback_bars: int = Field(default=168, ge=24)  # lookback for correlation calc


class CsmConfig(StrictConfigModel):
    """Cross-sectional momentum (CSM) strategy parameters.

    Mirrors the CSM contract: one timeframe, one or more duration lookbacks
    (e.g. ``["24h", "72h", "168h"]``), long/short percentile cutoffs, an
    equal-weighting policy, and a rebalance cadence.  v0 supports only
    ``weighting: equal``; ML, regime, OI, funding and order-book inputs are
    out of scope and rejected structurally.
    """

    timeframe: str = "1h"
    lookbacks: list[str] = Field(default_factory=lambda: ["24h"])
    # Percentile cutoffs on the cross-sectional rank: longs are symbols in
    # the top (1 - long_percentile), shorts in the bottom short_percentile.
    long_percentile: float = Field(default=0.90, gt=0.0, lt=1.0)
    # Omit / null for long-only.
    short_percentile: float | None = Field(default=None, gt=0.0, lt=1.0)
    weighting: Literal["equal"] = "equal"
    rebalance_hours: int = Field(default=24, ge=1)
    # Seed for randomised baselines (``random_baseline`` strategy) so the
    # null model is reproducible; unused by the momentum strategies.
    seed: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _csm_sanity(self) -> CsmConfig:
        if not policy.is_timeframe_allowed(self.timeframe):
            raise ValueError(
                f"csm.timeframe must be one of {policy.ALLOWED_TIMEFRAMES}, "
                f"got {self.timeframe!r}"
            )
        if not self.lookbacks:
            raise ValueError("csm.lookbacks must list at least one duration")
        tf_seconds = policy.timeframe_to_seconds(self.timeframe)
        for duration in self.lookbacks:
            try:
                seconds = policy.parse_duration_seconds(duration)
            except ValueError as exc:
                raise ValueError(str(exc)) from exc
            if seconds % tf_seconds != 0:
                raise ValueError(
                    f"csm lookback {duration!r} is not an integer multiple of "
                    f"csm.timeframe {self.timeframe!r}"
                )
        if self.short_percentile is not None and self.long_percentile <= self.short_percentile:
            raise ValueError(
                "csm.long_percentile must exceed csm.short_percentile"
            )
        return self


class MeanReversionConfig(StrictConfigModel):
    """Mean reversion (MR) strategy parameters.

    Cross-sectional z-score of short-horizon returns on a rolling window.
    Entry when |z| >= entry_threshold, exit on reversion (|z| <= exit_threshold)
    or time-stop (max_holding_bars). Hourly rebalance cadence.
    """
    timeframe: str = "1h"
    zscore_window_bars: int = Field(default=48, ge=10)
    signal_lookback: str = "8h"
    entry_threshold: float = Field(default=2.0, gt=0.0)
    exit_threshold: float = Field(default=0.5, ge=0.0)
    max_holding_bars: int = Field(default=48, ge=1)
    weighting: Literal["equal", "inverse_vol"] = "inverse_vol"
    rebalance_hours: int = Field(default=12, ge=1)
    max_positions: int = Field(default=4, ge=1)
    seed: int | None = Field(default=42, ge=0)
    # Post-only execution (maker-only)
    entry_execution: Literal["market", "post_only"] = "post_only"
    exit_execution: Literal["market", "post_only"] = "post_only"
    min_expected_edge_bps: int = Field(default=10, ge=0)

    @model_validator(mode="after")
    def _mr_sanity(self) -> MeanReversionConfig:
        if not policy.is_timeframe_allowed(self.timeframe):
            raise ValueError(
                f"mean_reversion.timeframe must be one of {policy.ALLOWED_TIMEFRAMES}, "
                f"got {self.timeframe!r}"
            )
        if self.entry_threshold <= self.exit_threshold:
            raise ValueError(
                "mean_reversion.entry_threshold must exceed exit_threshold"
            )
        tf_seconds = policy.timeframe_to_seconds(self.timeframe)
        try:
            signal_seconds = policy.parse_duration_seconds(self.signal_lookback)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        if signal_seconds % tf_seconds != 0:
            raise ValueError(
                f"mean_reversion.signal_lookback {self.signal_lookback!r} is not an "
                f"integer multiple of timeframe {self.timeframe!r}"
            )
        return self


class RegimeConfig(StrictConfigModel):
    """Market regime classification parameters (R1).

    Two-axis regime classification:
    - Trend vs Range: ADX-based trend strength
    - Volatility Regime: Rolling percentile of ATR%
    """

    enabled: bool = True
    reference: str = "universe_basket"  # or "btc_only"
    # Trend axis
    trend_indicator: str = "adx"
    trend_period: int = 14
    trend_threshold: float = 25.0
    # Volatility axis
    vol_lookback_bars: int = 168
    vol_percentile_high: float = 0.75
    # Hysteresis
    hysteresis_min_dwell_bars: int = 6
    # Exposure multipliers per regime
    exposure_trend_low_vol: float = 1.0
    exposure_trend_high_vol: float = 0.5
    exposure_range_low_vol: float = 0.25
    exposure_range_high_vol: float = 0.0
    # Per-strategy overrides (Task 7)
    strategy_overrides: dict[str, dict[str, float]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _regime_sanity(self) -> RegimeConfig:
        # Trend axis
        if self.trend_period < 1:
            raise ValueError("regime.trend_period must be >= 1")
        if not 0.0 < self.trend_threshold <= 100.0:
            raise ValueError("regime.trend_threshold must be in (0, 100]")
        if self.trend_indicator not in ("adx",):
            raise ValueError(f"regime.trend_indicator must be 'adx', got {self.trend_indicator!r}")

        # Volatility axis
        if self.vol_lookback_bars < 2 * self.trend_period:
            raise ValueError(
                f"regime.vol_lookback_bars ({self.vol_lookback_bars}) must be >= 2 * trend_period ({2 * self.trend_period})"
            )
        if not 0.0 < self.vol_percentile_high < 1.0:
            raise ValueError("regime.vol_percentile_high must be in (0, 1)")

        # Hysteresis
        if self.hysteresis_min_dwell_bars < 0:
            raise ValueError("regime.hysteresis_min_dwell_bars must be >= 0")

        # Exposure multipliers
        for field_name in (
            "exposure_trend_low_vol",
            "exposure_trend_high_vol",
            "exposure_range_low_vol",
            "exposure_range_high_vol",
        ):
            value = getattr(self, field_name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"regime.{field_name} must be in [0, 1], got {value}")

        # Strategy overrides validation
        for strat_name, overrides in self.strategy_overrides.items():
            for key, value in overrides.items():
                if key not in (
                    "exposure_trend_low_vol",
                    "exposure_trend_high_vol",
                    "exposure_range_low_vol",
                    "exposure_range_high_vol",
                ):
                    raise ValueError(f"regime.strategy_overrides.{strat_name}.{key}: unknown exposure key")
                if not 0.0 <= value <= 1.0:
                    raise ValueError(f"regime.strategy_overrides.{strat_name}.{key} must be in [0, 1], got {value}")

        return self


class PortfolioConfig(StrictConfigModel):
    """Portfolio decision layer: strategy name, risk limits, volatility sizing."""

    strategy_name: str = "long_only_trend"
    volatility_sizing: bool = False
    risk: PortfolioRiskParams = PortfolioRiskParams()
    csm: CsmConfig = CsmConfig()
    mean_reversion: MeanReversionConfig = MeanReversionConfig()

    # R8: Rebalance scheduler persistence (persist next_rebalance_ts to SQLite)
    rebalance_persist: bool = True
    # R8: Separate regime recalculation cadence from rebalance cadence
    regime_cadence_hours: int = Field(default=1, ge=1, le=24)


# --------------------------------------------------------------------------- #
# Universe / Storage / Logging
# --------------------------------------------------------------------------- #
class UniverseAutoDiscover(StrictConfigModel):
    enabled: bool = True
    top_n: int = Field(default=60, ge=1)


class UniverseConfig(StrictConfigModel):
    quote: str = "USDT"
    symbols: list[str] = Field(default_factory=list)
    auto_discover: UniverseAutoDiscover = UniverseAutoDiscover()
    exclude: list[str] = Field(default_factory=list)
    exclude_stablecoins: bool = True
    exclude_leveraged_tokens: bool = True


class StorageConfig(StrictConfigModel):
    db_path: str = "data/crypto_bot.db"


class LoggingConfig(StrictConfigModel):
    # Accept both the canonical attribute name and the legacy YAML key "json".
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    level: str = "INFO"
    file: str = "logs/crypto_bot.log"
    # ``json`` shadows a pydantic BaseModel attribute; use it as the YAML/env
    # alias while the code reads the safe ``json_logs`` attribute.
    json_logs: bool = Field(default=False, alias="json")


# --------------------------------------------------------------------------- #
# Root
# --------------------------------------------------------------------------- #
class Settings(StrictConfigModel):
    """Application settings loaded from YAML + env overrides.

    All nested configs are validated recursively. Unknown keys in the YAML
    raise a validation error (fail-fast on config drift).
    """
    runtime: RuntimeConfig = RuntimeConfig()
    exchange: ExchangeConfig = ExchangeConfig()
    timeframes: TimeframesConfig = TimeframesConfig()
    strategy: StrategyParams = StrategyParams()
    scoring: ScoringParams = ScoringParams()
    filters: FilterParams = FilterParams()
    risk: RiskParams = RiskParams()
    portfolio: PortfolioConfig = PortfolioConfig()
    regime: RegimeConfig = RegimeConfig()
    storage: StorageConfig = StorageConfig()
    logging: LoggingConfig = LoggingConfig()
    universe: UniverseConfig = UniverseConfig()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Settings:
        """Build from a parsed YAML mapping, failing fast on unknown keys."""

        return cls.model_validate(data)
