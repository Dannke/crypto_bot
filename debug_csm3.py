import tempfile
from pathlib import Path
import sqlite3

from crypto_bot.config.env import Config, EnvConfig
from crypto_bot.config.settings import load_settings
from crypto_bot.config.settings import Settings
from crypto_bot.config.schemas import RegimeConfig
from crypto_bot.core.enums import Mode, StrategyType
from crypto_bot.simulation.historical_source import HistoricalCandleSource
from crypto_bot.simulation.backtester import Backtester
from crypto_bot.storage.db import Database
from crypto_bot.core.enums import StrategyType
from crypto_bot.core.types import Candle
from crypto_bot.config.env import EnvConfig
from crypto_bot.portfolio import PortfolioRiskLimits
from crypto_bot.config.schemas import RegimeConfig

PERIOD_MS = 3_600_000

def _candles(returns):
    out = []
    for i, ret in enumerate(returns):
        t = 1_700_000_000_000 + i * PERIOD_MS
        close = 100.0
        for r in returns[:i+1]:
            close *= (1 + r)
        c = Candle(
            timestamp=t,
            open=close,
            high=close * 1.001,
            low=close * 0.999,
            close=close,
            volume=1000.0
        )
        out.append(c)
    return out

def _csm_config():
    settings = Settings.model_validate({
        'strategy': {
            'trend': {'adx_min': 10.0},
            'momentum': {'rsi_period': 14},
            'volatility': {'atr_min_pct': 0.1, 'atr_max_pct': 10.0},
            'volume': {'spike_ratio': 1.5}
        },
        'portfolio': {
            'strategy_name': 'cross_sectional_momentum_v0',
            'csm': {
                'timeframe': '1h',
                'lookbacks': ['4h', '8h', '12h'],
                'long_percentile': 0.8,
                'short_percentile': 0.2,
                'rebalance_hours': 24
            },
            'risk': {'max_positions': 10, 'max_position_weight': 1.0}
        }
    })
    return Settings.model_validate(settings)


def main():
    a_returns = [0.015] * 24 + [0.0] * 31
    b_returns = [-0.01] * 24 + [0.0] * 31

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        symbol_candles = {
            'A/USDT': _candles(a_returns),
            'B/USDT': _candles(b_returns),
        }
        symbols = list(symbol_candles)
        start_ms = symbol_candles[symbols[0]][0].timestamp
        end_ms = symbol_candles[symbols[0]][-1].timestamp + PERIOD_MS

        source = HistoricalCandleSource()
        for symbol, candles in symbol_candles.items():
            source.load_all(symbol, '1h', candles)

        limits = PortfolioRiskLimits(
            max_positions=10, max_position_weight=1.0,
            max_gross_exposure=2.0, max_net_exposure=2.0,
        )

        db = Database(tmp_path / "csm_mode.db")
        config = _csm_config()
        config.runtime.mode = Mode.PAPER

        regime_config = RegimeConfig(
            enabled=True,
            reference="universe_basket",
            trend_period=5,
            trend_threshold=0.1,
            vol_lookback_bars=10,
            vol_percentile_high=0.75,
        )

        bt = Backtester(
            config,
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

        # Add debug to see what regime is detected
        import crypto_bot.pipeline.portfolio_fusion as fusion_mod
        original_fuse = fusion_mod.RegimeGatedFusion.fuse

        def debug_fuse(self, intents, regime):
            print(f"DEBUG: Regime={regime.regime}, strategy={intents[0].strategy_name if intents else None}")
            return original_fuse(self, intents, regime)

        fusion_mod.RegimeGatedFusion.fuse = debug_fuse

        summary = bt.run()
        print(f'Total trades: {summary.total_trades}')
        print(f'Total PnL: {summary.total_pnl_pct:.2f}%')

        conn = sqlite3.connect(db.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute('SELECT symbol, side, closed_by FROM positions ORDER BY id').fetchall()
        for r in rows:
            print(f'  {r["symbol"]} {r["side"]} {r["closed_by"]}')

if __name__ == '__main__':
    main()