"""Portfolio strategy mode of the shared Backtester.

Verifies that ONE backtester class replays both decision layers: the
candidate mode (existing, unchanged) and the portfolio mode (features ->
portfolio strategy -> risk engine -> weight-sized positions).  No second
backtester exists; only ``strategy_mode`` differs.
"""
from __future__ import annotations

import sqlite3

import pytest

from crypto_bot.core.enums import StrategyType
from crypto_bot.core.types import Candle
from crypto_bot.portfolio import PortfolioRiskLimits
from crypto_bot.simulation.backtester import Backtester
from crypto_bot.simulation.historical_source import HistoricalCandleSource
from crypto_bot.storage.db import Database

from ._shared import golden_config, uptrend_candles
from crypto_bot.config.schemas import RegimeConfig

PERIOD_MS = 3_600_000
BASE_TS = 1_700_000_000_000


def _run(
    tmp_path,
    config,
    symbol_candles: dict[str, list[Candle]],
    *,
    timeframes: list[str] | None = None,
    strategy_mode: StrategyType | str = StrategyType.PORTFOLIO,
    portfolio_limits: PortfolioRiskLimits | None = None,
    regime_config=None,
):
    timeframes = timeframes or ["1h"]
    symbols = list(symbol_candles)
    start_ms = symbol_candles[symbols[0]][0].timestamp
    end_ms = symbol_candles[symbols[0]][-1].timestamp + PERIOD_MS

    source = HistoricalCandleSource()
    for symbol, candles in symbol_candles.items():
        source.load_all(symbol, "1h", candles)

    db = Database(tmp_path / "portfolio_mode.db")
    bt = Backtester(
        config,
        symbols=symbols,
        timeframes=timeframes,
        start_ms=start_ms,
        end_ms=end_ms,
        source=source,
        db=db,
        strategy_mode=strategy_mode,
        portfolio_limits=portfolio_limits,
        regime_config=regime_config,
    )
    summary = bt.run()
    conn = sqlite3.connect(db.db_path)
    conn.row_factory = sqlite3.Row
    return bt, conn, summary


def _volatile_trend_candles(amplitude: float, trend: float) -> list[Candle]:
    """Uptrend candles with controllable bar range (drives ATR)."""
    price = 100.0
    candles: list[Candle] = []
    for i in range(40):
        ts = BASE_TS + i * PERIOD_MS
        open_p = round(price, 2)
        close_p = round(price * trend, 2)
        high_p = round(max(open_p, close_p) * (1 + amplitude / 2), 2)
        low_p = round(min(open_p, close_p) * (1 - amplitude / 2), 2)
        candles.append(Candle(
            timestamp=ts, open=open_p, high=high_p,
            low=low_p, close=close_p, volume=1000.0,
        ))
        price = close_p
    return candles


class TestPortfolioMode:
    def test_portfolio_mode_is_default_candidate_mode(self, tmp_path) -> None:
        candles = uptrend_candles()
        start_ms = candles[0].timestamp
        end_ms = candles[-1].timestamp + PERIOD_MS
        source = HistoricalCandleSource()
        source.load_all("BTC/USDT", "1h", candles)
        bt = Backtester(
            golden_config(),
            symbols=["BTC/USDT"],
            timeframes=["1h"],
            start_ms=start_ms,
            end_ms=end_ms,
            source=source,
            db=Database(tmp_path / "default_mode.db"),
        )
        assert bt.strategy_mode == StrategyType.CANDIDATE
        assert bt.manifest.strategy_mode == StrategyType.CANDIDATE.value

    @pytest.mark.parametrize("bad_mode", ["bogus", "portfolioe", ""])
    def test_rejects_unknown_strategy_mode(self, tmp_path, bad_mode) -> None:
        candles = uptrend_candles()
        start_ms = candles[0].timestamp
        end_ms = candles[-1].timestamp + PERIOD_MS
        source = HistoricalCandleSource()
        source.load_all("BTC/USDT", "1h", candles)
        with pytest.raises(ValueError, match="strategy_mode"):
            Backtester(
                golden_config(),
                symbols=["BTC/USDT"],
                timeframes=["1h"],
                start_ms=start_ms,
                end_ms=end_ms,
                source=source,
                db=Database(tmp_path / "bad_mode.db"),
                strategy_mode=bad_mode,
            )

    def test_portfolio_mode_opens_weight_sized_positions(self, tmp_path) -> None:
        config = golden_config()
        limits = PortfolioRiskLimits(
            max_positions=10,
            max_position_weight=1.0,
            max_gross_exposure=2.0,
            max_net_exposure=2.0,
)
        bt, conn, summary = _run(
            tmp_path,
            config,
            {"BTC/USDT": uptrend_candles(), "ETH/USDT": uptrend_candles()},
            strategy_mode=StrategyType.PORTFOLIO,
            portfolio_limits=limits,
            regime_config=RegimeConfig(
                enabled=True,
                reference="universe_basket",
                trend_period=5,
                trend_threshold=0.1,
                vol_lookback_bars=10,
                vol_percentile_high=0.75,
            ),
        )

        assert bt.strategy_mode == StrategyType.PORTFOLIO
        assert bt.manifest.strategy_mode == StrategyType.PORTFOLIO.value
        assert summary.total_trades >= 1, "expected at least one closed trade"

        rows = conn.execute("SELECT symbol, size, entry_price FROM positions").fetchall()
        assert len(rows) >= 2, f"expected positions for both symbols, got {len(rows)}"
        symbols = {r["symbol"] for r in rows}
        assert symbols == {"BTC/USDT", "ETH/USDT"}

        # Weight-based sizing: notional ~ weight * equity (equity starts at 10k)
        first: dict[str, object] = {}
        for r in rows:
            first.setdefault(r["symbol"], r)
        for symbol in symbols:
            row = first[symbol]
            notional = float(row["size"]) * float(row["entry_price"])
            assert notional == pytest.approx(0.5 * 10_000.0, rel=0.01), (
                f"expected ~50% weight sizing for {symbol}, notional={notional}"
            )

    def test_portfolio_mode_risk_rejects_overweight(self, tmp_path) -> None:
        limits = PortfolioRiskLimits(
            max_positions=10,
            max_position_weight=0.2,  # equal-weight 0.5 intents must be rejected
            max_gross_exposure=2.0,
            max_net_exposure=2.0,
        )
        bt, conn, summary = _run(
            tmp_path,
            golden_config(),
            {"BTC/USDT": uptrend_candles(), "ETH/USDT": uptrend_candles()},
            strategy_mode="portfolio",
            portfolio_limits=limits,
            regime_config=RegimeConfig(
                enabled=True,
                reference="universe_basket",
                trend_period=5,
                trend_threshold=0.1,
                vol_lookback_bars=10,
                vol_percentile_high=0.75,
            ),
        )

        assert summary.total_trades == 0, "no position may open when every intent is rejected"
        position_count = int(
            conn.execute("SELECT COUNT(*) AS c FROM positions").fetchone()["c"]
        )
        assert position_count == 0

        rejected = conn.execute(
            "SELECT symbol, accepted, detail FROM decisions WHERE accepted=0"
        ).fetchall()
        assert rejected, "risk rejections must be journaled"
        assert any(
            "REJECT_MAX_POSITION_WEIGHT" in r["detail"] for r in rejected
        ), f"expected portfolio reason in journal, got {[r['detail'] for r in rejected]}"

    def test_portfolio_mode_max_positions_cap(self, tmp_path) -> None:
        limits = PortfolioRiskLimits(
            max_positions=2,
            max_position_weight=1.0,
            max_gross_exposure=3.0,
            max_net_exposure=3.0,
        )
        bt, conn, summary = _run(
            tmp_path,
            golden_config(),
            {
                "BTC/USDT": uptrend_candles(),
                "ETH/USDT": uptrend_candles(),
                "SOL/USDT": uptrend_candles(),
            },
            strategy_mode="portfolio",
            portfolio_limits=limits,
            regime_config=RegimeConfig(
                enabled=True,
                reference="universe_basket",
                trend_period=5,
                trend_threshold=0.1,
                vol_lookback_bars=10,
                vol_percentile_high=0.75,
            ),
        )

        symbols = {
            r["symbol"] for r in conn.execute("SELECT DISTINCT symbol FROM positions").fetchall()
        }
        assert len(symbols) == 2, f"max_positions=2 must cap symbols to 2, got {symbols}"
        rejected = conn.execute(
            "SELECT detail FROM decisions WHERE accepted=0"
        ).fetchall()
        assert any("REJECT_MAX_POSITIONS" in r["detail"] for r in rejected)

    def test_portfolio_mode_volatility_sizing_weights(self, tmp_path) -> None:
        config = golden_config()
        # ETH calmer than BTC: inverse-vol sizing must give ETH a larger weight.
        btc = _volatile_trend_candles(amplitude=0.03, trend=1.01)
        eth = _volatile_trend_candles(amplitude=0.002, trend=1.01)
        limits = PortfolioRiskLimits(
            max_positions=10,
            max_position_weight=1.0,
            max_gross_exposure=2.0,
            max_net_exposure=2.0,
        )
        settings = config.settings.model_copy(
            update={"portfolio": config.settings.portfolio.model_copy(
                update={"volatility_sizing": True}
            )}
        )
        from crypto_bot.config.env import Config

        config = Config(settings=settings, env=config.env)

        bt, conn, summary = _run(
            tmp_path,
            config,
            {"BTC/USDT": btc, "ETH/USDT": eth},
            strategy_mode="portfolio",
            portfolio_limits=limits,
            regime_config=RegimeConfig(
                enabled=True,
                reference="universe_basket",
                trend_period=5,
                trend_threshold=0.1,
                vol_lookback_bars=10,
                vol_percentile_high=0.75,
            ),
        )
        assert summary.total_trades >= 1

        rows = conn.execute(
            "SELECT symbol, size, entry_price FROM positions ORDER BY id"
        ).fetchall()
        weights: dict[str, float] = {}
        for r in rows:
            weights.setdefault(r["symbol"], float(r["size"]) * float(r["entry_price"]) / 10_000.0)
        assert "BTC/USDT" in weights and "ETH/USDT" in weights
        assert weights["ETH/USDT"] > weights["BTC/USDT"], (
            f"expected ETH (calm) to be overweighted vs BTC (volatile), got {weights}"
        )


