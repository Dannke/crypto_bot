"""Pydantic schemas that validate the configuration contract.

These models are intentionally *structural*: they describe and bound the shape
of configuration values. Runtime *policy* (e.g. refusing to run in LIVE mode
until paper trading + tests are ready, honoring the ENABLE_LIVE_TRADING
kill-switch) is enforced by the settings loader / orchestrator, not here.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..core.enums import Mode


class StrictConfigModel(BaseModel):
    """Base for YAML config blocks: unknown keys fail fast everywhere."""

    model_config = ConfigDict(extra="forbid")


class RuntimeConfig(StrictConfigModel):
    mode: Mode = Mode.SIGNAL_ONLY
    loop_interval_seconds: int = Field(60, ge=5)
    timezone: str = "UTC"


class ExchangeConfig(StrictConfigModel):
    name: str = "binance"
    sandbox: bool = True
    rate_limit_ms: int = Field(1200, ge=200)


class TimeframesConfig(StrictConfigModel):
    primary: list[str] = Field(default_factory=lambda: ["15m", "1h", "4h"])
    # Must comfortably exceed the longest indicator period. The default trend
    # EMA slow is 200; validators require period + 10 bars, so 250 leaves a
    # sane margin without being wasteful. (Floor is 50, enforced below.)
    candles_per_tf: int = Field(250, ge=50)

    @model_validator(mode="after")
    def _non_empty(self) -> TimeframesConfig:
        if not self.primary:
            raise ValueError("timeframes.primary must list at least one timeframe")
        return self


# --------------------------------------------------------------------------- #
# Strategy
# --------------------------------------------------------------------------- #
class TrendParams(StrictConfigModel):
    ema_fast: int = Field(21, ge=1)
    ema_mid: int = Field(50, ge=1)
    ema_slow: int = Field(200, ge=1)
    adx_min: float = Field(20.0, ge=0.0, le=100.0)

    @model_validator(mode="after")
    def _ordered_emas(self) -> TrendParams:
        if not (self.ema_fast < self.ema_mid < self.ema_slow):
            raise ValueError("require ema_fast < ema_mid < ema_slow")
        return self


class MomentumParams(StrictConfigModel):
    rsi_period: int = Field(14, ge=2)
    rsi_long_min: float = Field(50.0, ge=0.0, le=100.0)
    rsi_long_max: float = Field(70.0, ge=0.0, le=100.0)
    rsi_short_min: float = Field(30.0, ge=0.0, le=100.0)
    rsi_short_max: float = Field(50.0, ge=0.0, le=100.0)


class VolatilityParams(StrictConfigModel):
    atr_period: int = Field(14, ge=1)
    atr_min_pct: float = Field(0.5, ge=0.0)
    atr_max_pct: float = Field(8.0, ge=0.0)
    bb_period: int = Field(20, ge=1)
    bb_std: float = Field(2.0, ge=0.1)

    @model_validator(mode="after")
    def _atr_band(self) -> VolatilityParams:
        if self.atr_min_pct >= self.atr_max_pct:
            raise ValueError("require atr_min_pct < atr_max_pct")
        return self


class VolumeParams(StrictConfigModel):
    ma_period: int = Field(20, ge=1)
    spike_ratio: float = Field(1.5, ge=1.0)


class StrategyParams(StrictConfigModel):
    trend: TrendParams = TrendParams()
    momentum: MomentumParams = MomentumParams()
    volatility: VolatilityParams = VolatilityParams()
    volume: VolumeParams = VolumeParams()


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #
class ScoringWeights(StrictConfigModel):
    trend: float = Field(0.30, ge=0.0, le=1.0)
    momentum: float = Field(0.20, ge=0.0, le=1.0)
    volume: float = Field(0.15, ge=0.0, le=1.0)
    setup: float = Field(0.15, ge=0.0, le=1.0)
    reward_risk: float = Field(0.15, ge=0.0, le=1.0)
    liquidity: float = Field(0.05, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _positive_total(self) -> ScoringWeights:
        total = (
            self.trend
            + self.momentum
            + self.volume
            + self.setup
            + self.reward_risk
            + self.liquidity
        )
        if total <= 0:
            raise ValueError("scoring weights must sum > 0")
        return self


class ScoringParams(StrictConfigModel):
    weights: ScoringWeights = ScoringWeights()
    min_score: float = Field(65.0, ge=0.0, le=100.0)
    min_confidence: float = Field(0.6, ge=0.0, le=1.0)
    max_candidates_per_cycle: int = Field(3, ge=1)


# --------------------------------------------------------------------------- #
# Filters / Risk
# --------------------------------------------------------------------------- #
class FilterParams(StrictConfigModel):
    min_quote_volume_usd: float = Field(5_000_000.0, ge=0.0)
    max_spread_pct: float = Field(0.15, ge=0.0)
    blacklist: list[str] = Field(default_factory=list)
    cooldown_after_trade_minutes: int = Field(240, ge=0)


class RiskParams(StrictConfigModel):
    equity_currency: str = "USDT"
    risk_per_trade_pct: float = Field(1.0, gt=0.0, le=5.0)
    take_profit_risk_multiple: float = Field(2.0, ge=1.0)
    max_stop_distance_pct: float = Field(3.0, gt=0.0, le=20.0)
    max_open_positions: int = Field(5, ge=1)
    max_daily_drawdown_pct: float = Field(3.0, gt=0.0, le=100.0)
    emergency_drawdown_pct: float = Field(6.0, gt=0.0, le=100.0)

    @model_validator(mode="after")
    def _drawdown_ladder(self) -> RiskParams:
        if self.emergency_drawdown_pct <= self.max_daily_drawdown_pct:
            raise ValueError(
                "emergency_drawdown_pct must be greater than max_daily_drawdown_pct"
            )
        return self


# --------------------------------------------------------------------------- #
# Universe / Storage / Logging
# --------------------------------------------------------------------------- #
class UniverseAutoDiscover(StrictConfigModel):
    enabled: bool = True
    top_n: int = Field(60, ge=1)


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
    runtime: RuntimeConfig = RuntimeConfig()
    exchange: ExchangeConfig = ExchangeConfig()
    timeframes: TimeframesConfig = TimeframesConfig()
    strategy: StrategyParams = StrategyParams()
    scoring: ScoringParams = ScoringParams()
    filters: FilterParams = FilterParams()
    risk: RiskParams = RiskParams()
    storage: StorageConfig = StorageConfig()
    logging: LoggingConfig = LoggingConfig()
    universe: UniverseConfig = UniverseConfig()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Settings:
        """Build from a parsed YAML mapping, failing fast on unknown keys."""
        return cls.model_validate(data)
