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
    loop_interval_seconds: int = Field(default=60, ge=5)
    timezone: str = "UTC"
    strategy: str = "confluence"  # "confluence" (multi-TF) or "per_timeframe" (independent per TF)


class ExchangeConfig(StrictConfigModel):
    name: str = "binance"
    sandbox: bool = True
    rate_limit_ms: int = Field(default=1200, ge=200)


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
    atr_min_pct: float = Field(default=0.5, ge=0.0)
    atr_max_pct: float = Field(default=8.0, ge=0.0)
    bb_period: int = Field(default=20, ge=1)
    bb_std: float = Field(default=2.0, ge=0.1)

    @model_validator(mode="after")
    def _atr_band(self) -> VolatilityParams:
        if self.atr_min_pct >= self.atr_max_pct:
            raise ValueError("require atr_min_pct < atr_max_pct")
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
    take_profit_risk_multiple: float = Field(default=2.0, ge=1.0)
    max_stop_distance_pct: float = Field(default=3.0, gt=0.0, le=20.0)
    max_open_positions: int = Field(default=5, ge=1)
    max_daily_drawdown_pct: float = Field(default=3.0, gt=0.0, le=100.0)
    emergency_drawdown_pct: float = Field(default=6.0, gt=0.0, le=100.0)

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