import sys
sys.path.insert(0, '.')

from src.crypto_bot.simulation.historical_source import HistoricalCandleSource
from src.crypto_bot.core.types import Candle
from src.crypto_bot.portfolio import get_universe_snapshot, get_market_snapshot
import sqlite3

db_path = 'data/crypto_bot.db'
source = HistoricalCandleSource()

sym = 'BTC/USDT'
timeframe = '1h'
start_ms = 1704067200000
end_ms = 1704218400000

with sqlite3.connect('data/crypto_bot.db') as conn:
    rows = conn.execute(
        '''SELECT ts_ms, open, high, low, close, volume 
           FROM candles WHERE symbol=? AND timeframe=? AND ts_ms BETWEEN ? AND ?
           ORDER BY ts_ms''',
        (sym, timeframe, start_ms, end_ms)
    ).fetchall()

from src.crypto_bot.core.types import Candle
candles = [
    Candle(
        timestamp=r[0], open=r[1], high=r[2], low=r[3], close=r[4], volume=r[5]
    )
    for r in rows
]

print('Candles created:', len(candles))
print('First candle:', candles[0])
print('Is Candle:', isinstance(candles[0], Candle))

source = HistoricalCandleSource()
source.load_all('BTC/USDT', '1h', candles)

print('Cache:', ('BTC/USDT', '1h') in source._cache)
if ('BTC/USDT', '1h') in source._cache:
    candles_cached, close_times, period_ms = source._cache[('BTC/USDT', '1h')]
    print('Cached candles:', len(candles_cached))
    print('First cached:', candles_cached[0])
    print('Is Candle:', isinstance(candles_cached[0], Candle))

# Test slice
from src.crypto_bot.portfolio import get_universe_snapshot, get_market_snapshot

universe = get_universe_snapshot(source, ['BTC/USDT'], '1h', 1704218400000)
print('Universe:', universe)
snapshot = get_market_snapshot(source, universe, '1h')
print('Snapshot:', snapshot)