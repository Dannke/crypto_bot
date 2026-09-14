"""Cross-field validators that are NOT tied to the YAML structure.

``schemas.py`` validates the *shape* of individual config blocks (ranges,
ordering of EMAs, drawdown ladder). The functions here validate *semantics*
that span multiple blocks or involve environment policy:

  * live-trading gate (env kill-switches + release flag + sandbox + creds)
  * mode <-> trading-flag compatibility
  * timeframes are legal, unique and ascending in granularity
  * strategy periods fit within the requested candle history
  * universe is well-formed (uppercase, non-empty, no self-pairs, no dupes,
    no banned assets when the corresponding exclusions are enabled)

Everything raises a typed ``ConfigError`` (or the more specific
``LiveTradingForbiddenError``) on the first violation, so a bad config aborts
startup immediately instead of failing silently mid-loop.
"""
from __future__ import annotations

from ..config.env import Config
from ..core import policy
from ..core.enums import Mode
from ..core.exceptions import ConfigError, LiveTradingForbiddenError


# --------------------------------------------------------------------------- #
# Mode / live gate
# --------------------------------------------------------------------------- #
def validate_mode_compatibility(config: Config) -> None:
    """Trading flags must agree with the requested mode."""
    mode = config.mode
    env = config.env

    if mode == Mode.SIGNAL_ONLY:
        # Signal-only never touches orders; trading flags are irrelevant.
        return

    if not env.enable_trading:
        raise LiveTradingForbiddenError(
            f"mode={mode.value} requires ENABLE_TRADING=true (currently false)."
        )

    if mode == Mode.LIVE:
        validate_live_gate(config)


def validate_live_gate(config: Config) -> None:
    """The full set of conditions for real order placement.

    Any failure here is a deliberate block, surfaced as ``LiveTradingForbiddenError``.
    """
    if config.mode != Mode.LIVE:
        return

    reasons: list[str] = []

    if not policy.LIVE_TRADING_RELEASED:
        reasons.append(
            "paper trading and the test suite have not been released "
            "(policy.LIVE_TRADING_RELEASED is False)"
        )
    if not config.env.enable_live_trading:
        reasons.append("ENABLE_LIVE_TRADING is false")
    sandbox = (
        config.env.exchange_sandbox
        if config.env.exchange_sandbox is not None
        else config.settings.exchange.sandbox
    )
    if config.env.enable_live_trading and sandbox:
        reasons.append("sandbox/testnet endpoints are still on (live needs production)")
    if not config.env.exchange_api_key or not config.env.exchange_api_secret:
        reasons.append("EXCHANGE_API_KEY / EXCHANGE_API_SECRET are not set")

    if reasons:
        raise LiveTradingForbiddenError(
            "Live trading blocked: " + "; ".join(reasons) + "."
        )


# --------------------------------------------------------------------------- #
# Timeframes & strategy fit
# --------------------------------------------------------------------------- #
def validate_timeframes(config: Config) -> None:
    tfs = config.settings.timeframes.primary
    if not tfs:
        raise ConfigError("timeframes.primary must contain at least one timeframe.")

    if len(set(tfs)) != len(tfs):
        raise ConfigError(f"timeframes.primary has duplicates: {tfs}.")

    for tf in tfs:
        if not policy.is_timeframe_allowed(tf):
            raise ConfigError(
                f"unsupported timeframe '{tf}'. Allowed: {policy.ALLOWED_TIMEFRAMES}."
            )

    # Granularity must ascend: trigger (fast) -> confirmation -> trend (slow).
    minutes = [policy.timeframe_to_minutes(tf) for tf in tfs]
    if minutes != sorted(minutes):
        raise ConfigError(
            f"timeframes must be in ascending granularity, got {tfs}."
        )


def _max_indicator_period(config: Config) -> int:
    s = config.settings.strategy
    return max(
        s.trend.ema_slow,
        s.trend.ema_fast,
        s.momentum.rsi_period,
        s.volatility.atr_period,
        s.volatility.bb_period,
        s.volume.ma_period,
    )


def validate_strategy_periods(config: Config) -> None:
    """Requested history must cover the longest indicator period with margin."""
    needed = _max_indicator_period(config) + 10
    have = config.settings.timeframes.candles_per_tf
    if have < needed:
        raise ConfigError(
            f"candles_per_tf={have} is too small; indicators need >= {needed} "
            f"bars on each timeframe (largest period = {_max_indicator_period(config)})."
        )


def validate_portfolio_csm(config: Config) -> None:
    """CSM/MR lookback windows must fit inside the configured candle history."""
    s = config.settings
    if s.portfolio.strategy_name == "cross_sectional_momentum_v0":
        csm = s.portfolio.csm
        tf_seconds = policy.timeframe_to_seconds(csm.timeframe)
        bars_required = max(
            policy.parse_duration_seconds(duration) // tf_seconds
            for duration in csm.lookbacks
        ) + 1
        if s.timeframes.candles_per_tf < bars_required:
            raise ConfigError(
                f"candles_per_tf={s.timeframes.candles_per_tf} is too small for CSM "
                f"lookbacks {csm.lookbacks} on {csm.timeframe}; need >= {bars_required} "
                "bars (longest lookback + 1)."
            )
    elif s.portfolio.strategy_name == "mean_reversion_v0":
        mr = s.portfolio.mean_reversion
        tf_seconds = policy.timeframe_to_seconds(mr.timeframe)
        signal_bars = policy.parse_duration_seconds(mr.signal_lookback) // tf_seconds
        bars_required = mr.zscore_window_bars + signal_bars + 1
        if s.timeframes.candles_per_tf < bars_required:
            raise ConfigError(
                f"candles_per_tf={s.timeframes.candles_per_tf} is too small for Mean Reversion "
                f"window={mr.zscore_window_bars} signal_lookback={mr.signal_lookback} on {mr.timeframe}; "
                f"need >= {bars_required} bars (window + signal_lookback + 1)."
            )


# --------------------------------------------------------------------------- #
# Universe
# --------------------------------------------------------------------------- #
def _norm_symbol(base: str) -> str:
    return base.strip().upper()


def validate_universe(config: Config) -> None:
    u = config.settings.universe

    quote = _norm_symbol(u.quote)
    if not quote:
        raise ConfigError("universe.quote must be a non-empty currency code.")
    if quote not in policy.SUPPORTED_QUOTE_CURRENCIES:
        raise ConfigError(
            f"universe.quote='{quote}' is not supported. "
            f"Supported: {policy.SUPPORTED_QUOTE_CURRENCIES}."
        )

    seen: set[str] = set()
    for raw in u.symbols:
        base = _norm_symbol(raw)
        if not base:
            raise ConfigError("universe.symbols contains an empty entry.")
        if base == quote:
            raise ConfigError(f"symbol '{base}' equals its quote currency '{quote}'.")
        if base in seen:
            raise ConfigError(f"universe.symbols has a duplicate: '{base}'.")
        seen.add(base)

        if u.exclude_stablecoins and policy.is_stablecoin(base):
            raise ConfigError(
                f"symbol '{base}' is a stablecoin but exclude_stablecoins is true."
            )
        if u.exclude_leveraged_tokens and policy.is_leveraged_token(base):
            raise ConfigError(
                f"symbol '{base}' looks like a leveraged token but "
                f"exclude_leveraged_tokens is true."
            )

    if u.auto_discover.top_n > policy.MAX_AUTO_DISCOVER_SYMBOLS:
        raise ConfigError(
            f"auto_discover.top_n={u.auto_discover.top_n} exceeds "
            f"MAX_AUTO_DISCOVER_SYMBOLS={policy.MAX_AUTO_DISCOVER_SYMBOLS}."
        )

    excluded = {_norm_symbol(x) for x in u.exclude if _norm_symbol(x)}
    if len(excluded) != len([x for x in u.exclude if _norm_symbol(x)]):
        raise ConfigError("universe.exclude has duplicate entries.")
    overlap = seen.intersection(excluded)
    if overlap:
        raise ConfigError(
            f"universe.symbols contains excluded assets: {sorted(overlap)}."
        )


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #
def validate_all(config: Config) -> None:
    """Run every validator. Raises on the first violation (fail-fast)."""
    validate_timeframes(config)
    validate_strategy_periods(config)
    validate_portfolio_csm(config)
    validate_universe(config)
    validate_mode_compatibility(config)
    # validate_live_gate is invoked transitively when mode == LIVE.


def validate_runtime_safety(config: Config) -> None:
    """Entry point called from ``cli.py`` before the orchestrator starts.

    Thin wrapper around :func:`validate_all` — kept as a separate name so the
    CLI's intent ("make sure it's safe to run") is explicit at the call site,
    independent of how the validation suite is internally organised.
    """
    validate_all(config)