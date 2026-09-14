"""Cross-sectional momentum portfolio mode of the shared Backtester (task 12).

The same Backtester class replays the CSM strategy on its rebalance
cadence: a closed-bar MarketSnapshot replaces features, positions that
leave the target book (dropped or side-flipped) are closed on rebalance,
and incumbents are held untouched between rebalances.
"""
from __future__ import annotations

import sqlite3

from crypto_bot.config.env import Config
from crypto_bot.config.schemas import CsmConfig, RegimeConfig
from crypto_bot.core.enums import StrategyType
from crypto_bot.core.types import Candle
from crypto_bot.portfolio import PortfolioRiskLimits
from crypto_bot.simulation.backtester import Backtester
from crypto_bot.simulation.historical_source import HistoricalCandleSource
from crypto_bot.storage.db import Database
from crypto_bot.strategy.portfolio_strategies import MOMENTUM_V0_STRATEGY_NAME

from ._shared import golden_config

PERIOD_MS = 3_600_000
BASE_TS = 1_700_000_000_000


def _candles(rets: list[float], *, start: float = 100.0, amplitude: float = 0.002) -> list[Candle]:
    """Per-bar geometric returns with a small implied bar range."""
    out: list[Candle] = []
    price = start
    for i, r in enumerate(rets):
        close_p = price * (1.0 + r)
        high = max(price, close_p) * (1.0 + amplitude)
        low = min(price, close_p) * (1.0 - amplitude)
        out.append(Candle(
            timestamp=BASE_TS + i * PERIOD_MS,
            open=price, high=high, low=low, close=close_p, volume=1000.0,
        ))
        price = close_p
    return out


def _csm_config(**overrides) -> Config:
    portfolio = golden_config().settings.portfolio.model_copy(
        update={
            "strategy_name": MOMENTUM_V0_STRATEGY_NAME,
            "csm": CsmConfig(
                timeframe="1h",
                lookbacks=["24h"],
                long_percentile=0.67,   # top third
                short_percentile=0.33,  # bottom third
                weighting="equal",
                rebalance_hours=24,
            ),
        }
    )
    settings = golden_config().settings.model_copy(update={"portfolio": portfolio})
    return Config(settings=settings, env=golden_config().env)


def _run(tmp_path, symbol_candles: dict[str, list[Candle]], config=None, regime_config=None):
    symbols = list(symbol_candles)
    start_ms = symbol_candles[symbols[0]][0].timestamp
    end_ms = symbol_candles[symbols[0]][-1].timestamp + PERIOD_MS
    source = HistoricalCandleSource()
    for symbol, candles in symbol_candles.items():
        source.load_all(symbol, "1h", candles)
    limits = PortfolioRiskLimits(
        max_positions=10, max_position_weight=1.0,
        max_gross_exposure=2.0, max_net_exposure=2.0,
    )
    db = Database(tmp_path / "csm_mode.db")
    bt = Backtester(
        config or _csm_config(),
        symbols=symbols,
        timeframes=["1h"],
        start_ms=start_ms,
        end_ms=end_ms,
        source=source,
        db=db,
        strategy_mode=StrategyType.PORTFOLIO,
        portfolio_limits=limits,
        regime_config=regime_config,
    )
    summary = bt.run()
    conn = sqlite3.connect(db.db_path)
    conn.row_factory = sqlite3.Row
    return bt, conn, summary


class TestCsmPortfolioMode:
    def test_opens_long_and_short_from_the_cross_section(self, tmp_path) -> None:
        # A +1.5%/bar for 24 bars then flat; B -1%/bar then flat:
        # at t=25h A is the top, B the bottom; both held to the end.
        bt, conn, summary = _run(
            tmp_path,
            {
                "A/USDT": _candles([0.015] * 24 + [0.0] * 31),
                "B/USDT": _candles([-0.01] * 24 + [0.0] * 31),
            },
            regime_config=RegimeConfig(
                enabled=True,
                reference="universe_basket",
                trend_period=5,
                trend_threshold=0.1,
                vol_lookback_bars=10,
                vol_percentile_high=0.75,
            ),
        )
        rows = conn.execute(
            "SELECT symbol, side, closed_by FROM positions ORDER BY id"
        ).fetchall()
        assert {(r["symbol"], r["side"]) for r in rows} >= {
            ("A/USDT", "LONG"), ("B/USDT", "SHORT"),
        }
        # Both must still be held at the end (nothing to drop or flip).
        assert not any(r["closed_by"] == "rebalance" for r in rows)
        assert summary.total_trades == 0

    def test_rebalance_holds_incumbents_until_out_of_book(self, tmp_path) -> None:
        # A up then flat; B down then flat; C flat then up +1%/bar.
        # Rebalance 1 (t=25h): A long, B short.  Rebalance 2 (t=49h):
        # C (+27% over 24h) takes the top, A drops to the bottom.
        a = _candles([0.015] * 25 + [0.0] * 30)
        b = _candles([-0.01] * 25 + [0.0] * 30)
        c = _candles([0.0] * 25 + [0.01] * 30)
        bt, conn, summary = _run(
            tmp_path, {"A/USDT": a, "B/USDT": b, "C/USDT": c},
            regime_config=RegimeConfig(
                enabled=True,
                reference="universe_basket",
                trend_period=5,
                trend_threshold=0.1,
                vol_lookback_bars=10,
                vol_percentile_high=0.75,
            ),
        )

        rows = conn.execute(
            "SELECT symbol, side, closed_by, opened_at_ms, exit_price FROM positions ORDER BY id"
        ).fetchall()
        first_of: dict[str, dict] = {}
        for r in rows:
            first_of.setdefault(r["symbol"], dict(r))
        assert first_of["A/USDT"]["side"] == "LONG"
        assert first_of["B/USDT"]["side"] == "SHORT"

        # Second rebalance: C (+27% over 24h) takes the top, A drops to
        # the bottom third (0% ties B, which stays) and flips LONG -> SHORT.
        # A is closed by rebalance; B stays SHORT and is held untouched.
        rebalanced = [r["symbol"] for r in rows if r["closed_by"] == "rebalance"]
        assert rebalanced == ["A/USDT"]
        b_rows = [dict(r) for r in rows if r["symbol"] == "B/USDT"]
        assert len(b_rows) == 1
        assert b_rows[0]["side"] == "SHORT"
        assert b_rows[0]["closed_by"] is None

        # C enters LONG and A is re-opened SHORT at the second rebalance.
        c_row = [dict(r) for r in rows if r["symbol"] == "C/USDT"][0]
        assert c_row["side"] == "LONG"
        assert c_row["opened_at_ms"] == BASE_TS + 49 * PERIOD_MS
        assert summary.total_trades >= 2

    def test_only_evaluates_on_the_rebalance_cadence(self, tmp_path) -> None:
        # 55 bars, rebalance every 24h: evaluations at t=25h and t=49h
        # (the first ticks with 25 closed bars), not one per hour.
        bt, conn, summary = _run(
            tmp_path,
            {
                "A/USDT": _candles([0.015] * 24 + [0.0] * 31),
                "B/USDT": _candles([-0.01] * 24 + [0.0] * 31),
            },
            regime_config=RegimeConfig(
                enabled=True,
                reference="universe_basket",
                trend_period=5,
                trend_threshold=0.1,
                vol_lookback_bars=10,
                vol_percentile_high=0.75,
            ),
        )
        ticks = conn.execute(
            "SELECT DISTINCT ts_ms FROM decisions ORDER BY ts_ms"
        ).fetchall()
        assert [t["ts_ms"] for t in ticks] == [
            BASE_TS + h * PERIOD_MS for h in (25, 49)
        ]
        assert summary.total_trades == 0  # A long and B short held throughout