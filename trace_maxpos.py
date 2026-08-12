"""Trace max_open_positions behaviour in multi-symbol backtester."""
import sys, os, tempfile, sqlite3, asyncio, logging
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

logging.basicConfig(level=logging.WARNING)

from crypto_bot.config.env import Config, EnvConfig
from crypto_bot.core.enums import Mode
from crypto_bot.core.types import Candle
from crypto_bot.simulation.backtester import Backtester
from crypto_bot.simulation.historical_source import HistoricalCandleSource
from crypto_bot.storage.db import Database
from crypto_bot.config.schemas import Settings

async def main():
    base_ts = 1_700_000_000_000
    period_5m = 300_000

    # Generate 144 candles (12 hours) for 3 symbols
    candle_sets = {}
    for sym in ['BTC/USDT', 'ETH/USDT', 'SOL/USDT']:
        candles = []
        price = {'BTC/USDT': 100.0, 'ETH/USDT': 50.0, 'SOL/USDT': 20.0}[sym]
        for i in range(144):
            ts = base_ts + i * period_5m
            cp = round(price * 1.0025, 2)
            hp = round(max(price, cp) * 1.001, 2)
            lp = round(min(price, cp) * 0.999, 2)
            candles.append(Candle(timestamp=ts, open=round(price,2), high=hp, low=lp, close=cp, volume=1000.0))
            price = cp
        candle_sets[sym] = candles

    # 1h candles
    candle_sets_1h = {}
    for sym in candle_sets:
        group = candle_sets[sym]
        candles = []
        for h in range(12):
            start = h * 12
            chunk = group[start:start+12]
            candles.append(Candle(
                timestamp=chunk[0].timestamp,
                open=chunk[0].open, high=max(c.high for c in chunk),
                low=min(c.low for c in chunk), close=chunk[-1].close,
                volume=sum(c.volume for c in chunk),
            ))
        candle_sets_1h[sym] = candles

    source = HistoricalCandleSource()
    for sym in ['BTC/USDT', 'ETH/USDT', 'SOL/USDT']:
        source.load_all(sym, '15m', candle_sets[sym])
        source.load_all(sym, '1h', candle_sets_1h[sym])

    tmp = tempfile.mktemp()
    os.makedirs(tmp)
    db_path = os.path.join(tmp, 'test.db')
    db = Database(db_path)

    sd = Settings().model_dump()
    sd['risk']['max_open_positions'] = 3
    sd['risk']['emergency_drawdown_pct'] = 100.0
    sd['risk']['risk_per_trade_pct'] = 1.0
    sd['risk']['take_profit_risk_multiple'] = 2.0
    sd['risk']['max_stop_distance_pct'] = 5.0
    sd['scoring']['min_score'] = 30
    sd['scoring']['max_candidates_per_cycle'] = 10
    settings = Settings.model_validate(sd)
    env = EnvConfig(crypto_bot_mode=Mode.PAPER)
    config = Config(settings=settings, env=env)

    start_ms = candle_sets['BTC/USDT'][0].timestamp
    end_ms = candle_sets['BTC/USDT'][-1].timestamp + period_5m

    bt = Backtester(config, symbols=['BTC/USDT','ETH/USDT','SOL/USDT'],
                    timeframes=['15m','1h'],
                    start_ms=start_ms, end_ms=end_ms, source=source, db=db)

    print(f"max_open_positions={bt._config.settings.risk.max_open_positions}")
    summary = await bt.run_async()
    print(f"total_trades={summary.total_trades}")
    print(f"open_positions={len([p for p in bt._executor.tracker.positions if p.is_open])}")

    con = sqlite3.connect(db_path)
    rows = con.execute("""
        SELECT id, symbol, timeframe, opened_at_ms, closed_at_ms, status,
               (SELECT COUNT(*) FROM positions p2
                WHERE p2.opened_at_ms <= p1.opened_at_ms
                  AND (p2.closed_at_ms IS NULL OR p2.closed_at_ms > p1.opened_at_ms)) as concurrent
        FROM positions p1
        ORDER BY opened_at_ms
    """).fetchall()
    con.close()
    maxc = max(r[6] for r in rows) if rows else 0
    print(f"max_concurrent={maxc}")
    for r in rows:
        print(f"  id={r[0]:2d} {r[1]:12s} {r[2]:2s} open={r[3]} close={str(r[4]):>15s} status={r[5]:6s} concurrent={r[6]}")

if __name__ == "__main__":
    asyncio.run(main())
