"""Built-in portfolio strategies (cross-section level, intent-producing)."""
from __future__ import annotations

import random
from math import ceil, sqrt

from ..core.enums import Side
from ..core.types import Candle
from ..portfolio.market_snapshot import MarketSnapshot
from ..portfolio.mean_reversion_features import compute_zscore_snapshot
from ..portfolio.models import (
    CrossSectionalFeatureSnapshot,
    PortfolioIntent,
    PortfolioState,
    PositionIntent,
    UniverseSnapshot,
)
from .base import PortfolioStrategy, StrategyContext

DEFAULT_PORTFOLIO_STRATEGY_NAME = "long_only_trend"
MOMENTUM_V0_STRATEGY_NAME = "cross_sectional_momentum_v0"
RANDOM_STRATEGY_NAME = "random_baseline"
REVERSE_MOMENTUM_STRATEGY_NAME = "reverse_momentum_v0"
MEAN_REVERSION_V0_STRATEGY_NAME = "mean_reversion_v0"


class LongOnlyTrendPortfolioStrategy(PortfolioStrategy):
    """Equal-weight LONG targets for every symbol above a trend threshold.

    The strategy is intentionally dumb: it only *selects* symbols and hands
    out equal weights.  Position weights are then constrained and resized by
    the portfolio risk engine; nothing here knows about exposure limits,
    volatility sizing, or execution.
    """

    def __init__(
        self,
        context: StrategyContext,
        primary_timeframes: list[str],
        *,
        min_trend_score: float = 0.5,
    ) -> None:
        self._min_trend_score = min_trend_score

    def evaluate(
        self,
        features: CrossSectionalFeatureSnapshot,
        state: PortfolioState,
    ) -> PortfolioIntent:
        selected = [
            symbol
            for symbol in features.universe.symbols
            if symbol in features.features_by_symbol
            and features.features_by_symbol[symbol].trend_score >= self._min_trend_score
        ]
        weight = 1.0 / len(selected) if selected else 0.0
        intents = tuple(
            PositionIntent(
                symbol=symbol,
                side=Side.LONG,
                target_weight=weight,
                timeframe=features.features_by_symbol[symbol].timeframe,
            )
            for symbol in selected
        )
        return PortfolioIntent(
            as_of_ms=features.as_of_ms,
            universe=features.universe,
            strategy_name=DEFAULT_PORTFOLIO_STRATEGY_NAME,
            intents=intents,
        )


def _simple_return(bars: tuple[Candle, ...], lookback: int) -> float:
    """Cumulative return over ``lookback`` closed bars: close[-1]/close[-1-lookback] - 1."""
    return bars[-1].close / bars[-1 - lookback].close - 1.0


def _ranked_returns(
    snapshot: MarketSnapshot,
    lookbacks_bars: tuple[int, ...],
) -> list[tuple[str, float]]:
    """Composite per-symbol returns over the lookback horizons, descending.

    Symbols with fewer than ``max(lookbacks_bars) + 1`` closed bars cannot
    produce a return and are skipped.
    """
    required_bars = max(lookbacks_bars) + 1
    returns: list[tuple[str, float]] = []
    for symbol, bars in snapshot.candles_by_symbol.items():
        if len(bars) < required_bars:
            continue
        composite = sum(
            _simple_return(bars, lookback) for lookback in lookbacks_bars
        ) / len(lookbacks_bars)
        returns.append((symbol, composite))
    return sorted(returns, key=lambda item: item[1], reverse=True)


def _selection_sizes(
    n_symbols: int,
    top_fraction: float,
    short_fraction: float | None,
) -> tuple[int, int]:
    """Long/short counts to select (per side rounds up, like the CSM v0 contract)."""
    n_long = ceil(top_fraction * n_symbols)
    n_short = ceil(short_fraction * n_symbols) if short_fraction else 0
    return n_long, n_short


def _cross_section_intent(
    snapshot: MarketSnapshot,
    long_symbols: list[str],
    short_symbols: list[str],
    strategy_name: str,
) -> PortfolioIntent:
    """Equal-weighted intent over the selected longs/shorts (or all-cash)."""
    selected = long_symbols + short_symbols
    if not selected:
        return PortfolioIntent(
            as_of_ms=snapshot.as_of_ms,
            intents=(),
            universe=UniverseSnapshot(
                as_of_ms=snapshot.as_of_ms,
                symbols=tuple(snapshot.candles_by_symbol),
            ),
            strategy_name=strategy_name,
        )

    weight = 1.0 / len(selected)
    last_close = {
        symbol: bars[-1].close
        for symbol, bars in snapshot.candles_by_symbol.items()
    }
    intents = tuple(
        PositionIntent(
            symbol=symbol,
            side=Side.SHORT if symbol in short_symbols else Side.LONG,
            target_weight=weight,
            timeframe=snapshot.timeframe,
            reference_price=last_close[symbol],
        )
        for symbol in selected
    )
    return PortfolioIntent(
        as_of_ms=snapshot.as_of_ms,
        intents=intents,
        universe=UniverseSnapshot(
            as_of_ms=snapshot.as_of_ms,
            symbols=tuple(snapshot.candles_by_symbol),
        ),
        strategy_name=strategy_name,
    )


class CrossSectionalMomentumStrategy(PortfolioStrategy):
    """Cross-sectional momentum v0: returns -> rank -> percentile -> equal weights.

    v0 deliberately implements only the four primitives:

    * **returns** — geometric cumulative return of the last ``lookback``
      closed bars per symbol (``close[-1] / close[-1-lookback] - 1``).  With
      several lookbacks the per-horizon returns are equally weighted into a
      composite return (the ``weighting: equal`` policy of the CSM config);
    * **rank** — symbols sorted by composite return, descending;
    * **top/bottom percentile** — the top ``top_fraction`` of the universe
      is selected LONG, and with ``short_fraction`` set the bottom
      ``short_fraction`` is selected SHORT (per-side count rounds up, so
      tiny universes still select at least one symbol);
    * **equal weights** — every selected symbol gets ``1 / n_selected`` of
      equity, longs positive and shorts with ``Side.SHORT``.

    Explicitly out of scope for v0: ML, market regime, OI, funding, and the
    order book.  Symbols with fewer than ``max(lookbacks) + 1`` closed bars
    cannot produce a return and are skipped; if nothing qualifies, the
    strategy emits the all-cash intent.
    """

    def __init__(
        self,
        context: StrategyContext,
        primary_timeframes: list[str],
        *,
        lookback_bars: int | None = None,
        lookbacks_bars: tuple[int, ...] | None = None,
        top_fraction: float = 0.2,
        short_fraction: float | None = None,
    ) -> None:
        if (lookback_bars is None) == (lookbacks_bars is None) and lookback_bars is None:
            # Registry/manager construction without lookback arguments:
            # fall back to the v0 default of a single 20-bar horizon.
            self._lookbacks_bars = (20,)
        elif lookback_bars is not None and lookbacks_bars is not None:
            raise ValueError(
                "provide only one of lookback_bars (single horizon) or "
                "lookbacks_bars (multiple horizons)"
            )
        elif lookbacks_bars is not None:
            if not isinstance(lookbacks_bars, tuple) or not lookbacks_bars:
                raise ValueError("lookbacks_bars must be a non-empty tuple of positive ints")
            if any(isinstance(lb, bool) or not isinstance(lb, int) or lb < 1 for lb in lookbacks_bars):
                raise ValueError("lookbacks_bars must contain positive integers")
            self._lookbacks_bars = tuple(sorted(set(lookbacks_bars)))
        else:
            if isinstance(lookback_bars, bool) or not isinstance(lookback_bars, int) or lookback_bars < 1:
                raise ValueError("lookback_bars must be a positive integer")
            self._lookbacks_bars = (lookback_bars,)
        if (
            isinstance(top_fraction, bool)
            or not isinstance(top_fraction, (int, float))
            or not 0.0 < top_fraction <= 1.0
        ):
            raise ValueError("top_fraction must be in (0, 1]")
        if short_fraction is not None and (
            isinstance(short_fraction, bool)
            or not isinstance(short_fraction, (int, float))
            or not 0.0 < short_fraction <= 1.0
        ):
            raise ValueError("short_fraction must be in (0, 1] or None")
        self._top_fraction = float(top_fraction)
        self._short_fraction = float(short_fraction) if short_fraction is not None else None

    @property
    def lookbacks_bars(self) -> tuple[int, ...]:
        """Normalised lookback horizons in bars, ascending."""
        return self._lookbacks_bars

    @property
    def top_fraction(self) -> float:
        """Fraction of the best performers selected LONG."""
        return self._top_fraction

    @property
    def short_fraction(self) -> float | None:
        """Fraction of the worst performers selected SHORT (None = long-only)."""
        return self._short_fraction

    def evaluate(
        self,
        features: CrossSectionalFeatureSnapshot,
        state: PortfolioState,
    ) -> PortfolioIntent:
        raise NotImplementedError(
            "cross_sectional_momentum_v0 evaluates a MarketSnapshot via evaluate_market, "
            "not features"
        )

    def evaluate_market(
        self,
        snapshot: MarketSnapshot,
        state: PortfolioState,
    ) -> PortfolioIntent:
        if not isinstance(snapshot, MarketSnapshot):
            raise ValueError("snapshot must be a MarketSnapshot")
        if not isinstance(state, PortfolioState):
            raise ValueError("state must be a PortfolioState")

        ranked = _ranked_returns(snapshot, self._lookbacks_bars)
        n_symbols = len(ranked)
        n_long, n_short = _selection_sizes(n_symbols, self._top_fraction, self._short_fraction)

        long_symbols = [symbol for symbol, _ in ranked[:n_long]]
        short_symbols = [
            symbol for symbol, _ in ranked[-n_short:] if n_short
        ]
        short_symbols = [
            symbol for symbol in short_symbols if symbol not in long_symbols
        ]
        return _cross_section_intent(
            snapshot, long_symbols, short_symbols, MOMENTUM_V0_STRATEGY_NAME,
        )


class ReverseMomentumStrategy(CrossSectionalMomentumStrategy):
    """Contrarian null model: longs the worst performers, shorts the best.

    The exact mirror of ``CrossSectionalMomentumStrategy`` — same lookbacks,
    fractions, weighting and rebalance cadence — so a positive edge of the
    momentum side shows up as ``momentum_pnl > reverse_pnl`` on the same
    data, fees, slippage and risk harness.
    """

    def evaluate_market(
        self,
        snapshot: MarketSnapshot,
        state: PortfolioState,
    ) -> PortfolioIntent:
        if not isinstance(snapshot, MarketSnapshot):
            raise ValueError("snapshot must be a MarketSnapshot")
        if not isinstance(state, PortfolioState):
            raise ValueError("state must be a PortfolioState")

        ranked = _ranked_returns(snapshot, self._lookbacks_bars)
        n_symbols = len(ranked)
        n_long, n_short = _selection_sizes(n_symbols, self._top_fraction, self._short_fraction)

        short_symbols = [symbol for symbol, _ in ranked[:n_short]] if n_short else []
        long_symbols = [symbol for symbol, _ in ranked[-n_long:]]
        long_symbols = [
            symbol for symbol in long_symbols if symbol not in short_symbols
        ]
        return _cross_section_intent(
            snapshot, long_symbols, short_symbols, REVERSE_MOMENTUM_STRATEGY_NAME,
        )


class RandomBaselineStrategy(CrossSectionalMomentumStrategy):
    """Random null model: uniform-random long/short cross-section.

    Picks ``top_fraction`` of the universe LONG and ``short_fraction`` SHORT
    uniformly at random (no correlation to returns), equal-weighted, on the
    same rebalance cadence as CSM.  The draw is seeded (``seed``, mixed with
    the evaluation timestamp) so repeated runs are byte-identical.
    """

    def __init__(
        self,
        context: StrategyContext,
        primary_timeframes: list[str],
        *,
        seed: int | None = None,
        lookback_bars: int | None = None,
        lookbacks_bars: tuple[int, ...] | None = None,
        top_fraction: float = 0.2,
        short_fraction: float | None = None,
    ) -> None:
        super().__init__(
            context,
            primary_timeframes,
            lookback_bars=lookback_bars,
            lookbacks_bars=lookbacks_bars,
            top_fraction=top_fraction,
            short_fraction=short_fraction,
        )
        if seed is not None and (
            isinstance(seed, bool) or not isinstance(seed, int) or seed < 0
        ):
            raise ValueError("seed must be a non-negative integer or None")
        self._seed = seed

    def evaluate_market(
        self,
        snapshot: MarketSnapshot,
        state: PortfolioState,
    ) -> PortfolioIntent:
        if not isinstance(snapshot, MarketSnapshot):
            raise ValueError("snapshot must be a MarketSnapshot")
        if not isinstance(state, PortfolioState):
            raise ValueError("state must be a PortfolioState")

        ranked = _ranked_returns(snapshot, self._lookbacks_bars)
        n_symbols = len(ranked)
        n_long, n_short = _selection_sizes(n_symbols, self._top_fraction, self._short_fraction)

        shuffled = [symbol for symbol, _ in ranked]
        rng = random.Random(f"{self._seed}:{snapshot.as_of_ms}")
        rng.shuffle(shuffled)
        long_symbols = shuffled[:n_long]
        short_symbols = shuffled[n_long:n_long + n_short]
        return _cross_section_intent(
            snapshot, long_symbols, short_symbols, RANDOM_STRATEGY_NAME,
        )


def _inverse_vol_weights(
    snapshot: MarketSnapshot,
    symbols: list[str],
    lookback_bars: int = 24,
) -> dict[str, float]:
    """Compute inverse-volatility weights for a list of symbols.

    Volatility estimated as std of 1-bar returns over lookback_bars.
    Weights are normalized to sum to 1.0.
    """
    vols: dict[str, float] = {}
    for symbol in symbols:
        bars = snapshot.candles_by_symbol.get(symbol)
        if not bars or len(bars) < lookback_bars + 1:
            vols[symbol] = 1.0  # fallback
            continue
        closes = tuple(bar.close for bar in bars)
        returns = tuple(
            (closes[i] / closes[i - 1]) - 1.0
            for i in range(1, len(closes))
        )
        if len(returns) < lookback_bars:
            vols[symbol] = 1.0
            continue
        window_returns = returns[-lookback_bars:]
        mean = sum(window_returns) / len(window_returns)
        if len(window_returns) == 1:
            std = 0.0
        else:
            variance = sum((r - mean) ** 2 for r in window_returns) / (len(window_returns) - 1)
            std = sqrt(variance)
        vols[symbol] = max(std, 1e-8)

    inv_vols = {s: 1.0 / v for s, v in vols.items()}
    total = sum(inv_vols.values())
    if total == 0:
        return {s: 1.0 / len(symbols) for s in symbols}
    return {s: w / total for s, w in inv_vols.items()}


class MeanReversionStrategy(PortfolioStrategy):
    """Cross-sectional mean reversion v0: z-score -> rank -> threshold -> weights.

    Signal pipeline:
    1. **z-score** — per-symbol z-score of short-horizon return (signal_lookback)
       relative to rolling mean/std over zscore_window_bars (computed via
       ``compute_zscore_snapshot`` with same-anchor guarantees).
    2. **rank** — symbols sorted by z-score ascending (most negative = oversold = LONG
       candidate, most positive = overbought = SHORT candidate).
    3. **threshold filter** — enter LONG when z <= -entry_threshold, SHORT when
       z >= entry_threshold.  Exit (reversion) when |z| <= exit_threshold.
    4. **time-stop** — positions held longer than max_holding_bars are closed
       regardless of z-score (uses PortfolioState.positions[].opened_at).
    5. **weighting** — equal or inverse_vol (24h vol estimate).

    The strategy is stateful across evaluations: it reads ``PortfolioState.positions``
    to enforce exit logic (reversion + time-stop) and only emits new intents for
    entries.  Existing positions that meet exit criteria are omitted from the
    returned intent (which signals closure to the executor).

    v0 scope: single timeframe, cross-sectional z-score, fixed thresholds,
    hourly rebalance cadence.  Regime, funding, OI, order book — out of scope.
    """

    def __init__(
        self,
        context: StrategyContext,
        primary_timeframes: list[str],
        *,
        zscore_window_bars: int = 48,
        signal_lookback_bars: int = 4,
        entry_threshold: float = 2.0,
        exit_threshold: float = 0.5,
        max_holding_bars: int = 24,
        weighting: str = "inverse_vol",
        top_fraction: float = 0.2,
        short_fraction: float | None = 0.2,
    ) -> None:
        if not isinstance(zscore_window_bars, int) or zscore_window_bars < 10:
            raise ValueError("zscore_window_bars must be an integer >= 10")
        if not isinstance(signal_lookback_bars, int) or signal_lookback_bars < 1:
            raise ValueError("signal_lookback_bars must be a positive integer")
        if not isinstance(entry_threshold, (int, float)) or entry_threshold <= 0:
            raise ValueError("entry_threshold must be positive")
        if not isinstance(exit_threshold, (int, float)) or exit_threshold < 0:
            raise ValueError("exit_threshold must be non-negative")
        if entry_threshold <= exit_threshold:
            raise ValueError("entry_threshold must exceed exit_threshold")
        if not isinstance(max_holding_bars, int) or max_holding_bars < 1:
            raise ValueError("max_holding_bars must be a positive integer")
        if weighting not in ("equal", "inverse_vol"):
            raise ValueError("weighting must be 'equal' or 'inverse_vol'")
        if not isinstance(top_fraction, (int, float)) or isinstance(top_fraction, bool) or not 0.0 < top_fraction <= 1.0:
            raise ValueError("top_fraction must be in (0, 1]")
        if short_fraction is not None and (isinstance(short_fraction, bool) or not isinstance(short_fraction, (int, float)) or not 0.0 < short_fraction <= 1.0):
            raise ValueError("short_fraction must be in (0, 1] or None")

        self._zscore_window_bars = zscore_window_bars
        self._signal_lookback_bars = signal_lookback_bars
        self._entry_threshold = float(entry_threshold)
        self._exit_threshold = float(exit_threshold)
        self._max_holding_bars = max_holding_bars
        self._weighting = weighting
        self._top_fraction = float(top_fraction)
        self._short_fraction = float(short_fraction) if short_fraction is not None else None

    @property
    def zscore_window_bars(self) -> int:
        return self._zscore_window_bars

    @property
    def signal_lookback_bars(self) -> int:
        return self._signal_lookback_bars

    @property
    def entry_threshold(self) -> float:
        return self._entry_threshold

    @property
    def exit_threshold(self) -> float:
        return self._exit_threshold

    @property
    def max_holding_bars(self) -> int:
        return self._max_holding_bars

    @property
    def weighting(self) -> str:
        return self._weighting

    @property
    def top_fraction(self) -> float:
        return self._top_fraction

    @property
    def short_fraction(self) -> float | None:
        return self._short_fraction

    def evaluate(
        self,
        features: CrossSectionalFeatureSnapshot,
        state: PortfolioState,
    ) -> PortfolioIntent:
        raise NotImplementedError(
            "mean_reversion_v0 evaluates a MarketSnapshot via evaluate_market, "
            "not features"
        )

    def _get_position_age_bars(
        self,
        state: PortfolioState,
        symbol: str,
        timeframe: str,
    ) -> int | None:
        """Return number of bars since position entry, or None if not found."""
        for position in state.positions:
            if (
                position.symbol == symbol
                and position.timeframe == timeframe
                and position.opened_at is not None
            ):
                # We need to convert opened_at to bar count relative to state.as_of_ms
                # This is approximate; the caller should use a more precise method
                # For now, we approximate using the timeframe duration
                from ..core import policy
                tf_seconds = policy.timeframe_to_seconds(timeframe)
                age_seconds = (state.as_of_ms - int(position.opened_at.timestamp() * 1000)) / 1000
                return int(age_seconds / tf_seconds)
        return None

    def evaluate_market(
        self,
        snapshot: MarketSnapshot,
        state: PortfolioState,
    ) -> PortfolioIntent:
        if not isinstance(snapshot, MarketSnapshot):
            raise ValueError("snapshot must be a MarketSnapshot")
        if not isinstance(state, PortfolioState):
            raise ValueError("state must be a PortfolioState")

        # Compute z-scores
        zscore_snapshot = compute_zscore_snapshot(
            snapshot,
            window_bars=self._zscore_window_bars,
            signal_lookback_bars=self._signal_lookback_bars,
        )

        # Rank by z-score ascending (most negative first = oversold = LONG)
        ranked = sorted(
            zscore_snapshot.zscores.items(),
            key=lambda item: item[1],
        )

        n_symbols = len(ranked)
        n_long = ceil(self._top_fraction * n_symbols)
        n_short = ceil(self._short_fraction * n_symbols) if self._short_fraction else 0

        # Determine entry candidates
        long_candidates = [
            symbol for symbol, z in ranked[:n_long]
            if z <= -self._entry_threshold
        ]
        short_candidates = [
            symbol for symbol, z in ranked[-n_short:]
            if z >= self._entry_threshold
        ]

        # Determine which existing positions to keep (not exited)
        # Exit conditions: |z| <= exit_threshold (reversion) OR age >= max_holding_bars (time-stop)
        keep_long: list[str] = []
        keep_short: list[str] = []
        # Track symbols that were explicitly exited (to prevent re-entry on same bar)
        exited_symbols: set[str] = set()

        for position in state.positions:
            symbol = position.symbol
            if symbol not in zscore_snapshot.zscores:
                # No signal data — keep (conservative)
                if position.side == Side.LONG:
                    keep_long.append(symbol)
                else:
                    keep_short.append(symbol)
                continue

            z = zscore_snapshot.zscores[symbol]
            age_bars = self._get_position_age_bars(state, symbol, snapshot.timeframe)

            # Check exit conditions
            exited = False
            if abs(z) <= self._exit_threshold:
                exited = True  # reversion exit
            elif age_bars is not None and age_bars >= self._max_holding_bars:
                exited = True  # time-stop exit

            if not exited:
                if position.side == Side.LONG:
                    keep_long.append(symbol)
                else:
                    keep_short.append(symbol)
            else:
                exited_symbols.add(symbol)

        # Exclude exited symbols from new entry candidates (prevent immediate re-entry)
        long_candidates = [s for s in long_candidates if s not in exited_symbols]
        short_candidates = [s for s in short_candidates if s not in exited_symbols]

        # Combine: keep existing non-exited + new entries (avoid duplicates)
        final_long = list(dict.fromkeys(keep_long + long_candidates))
        final_short = list(dict.fromkeys(keep_short + short_candidates))

        if not final_long and not final_short:
            return PortfolioIntent(
                as_of_ms=snapshot.as_of_ms,
                intents=(),
                universe=UniverseSnapshot(
                    as_of_ms=snapshot.as_of_ms,
                    symbols=tuple(snapshot.candles_by_symbol),
                ),
                strategy_name=MEAN_REVERSION_V0_STRATEGY_NAME,
            )

        # Compute weights
        selected = final_long + final_short
        if self._weighting == "inverse_vol":
            weights = _inverse_vol_weights(snapshot, selected)
        else:
            weight = 1.0 / len(selected)
            weights = {s: weight for s in selected}

        last_close = {
            symbol: bars[-1].close
            for symbol, bars in snapshot.candles_by_symbol.items()
        }

        intents = tuple(
            PositionIntent(
                symbol=symbol,
                side=Side.SHORT if symbol in final_short else Side.LONG,
                target_weight=weights[symbol],
                timeframe=snapshot.timeframe,
                reference_price=last_close[symbol],
            )
            for symbol in selected
        )

        return PortfolioIntent(
            as_of_ms=snapshot.as_of_ms,
            intents=intents,
            universe=UniverseSnapshot(
                as_of_ms=snapshot.as_of_ms,
                symbols=tuple(snapshot.candles_by_symbol),
            ),
            strategy_name=MEAN_REVERSION_V0_STRATEGY_NAME,
        )
