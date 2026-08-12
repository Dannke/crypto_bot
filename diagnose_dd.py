"""Diagnose drawdown: trace equity tick-by-tick for the golden MTF test."""
import sys, os, tempfile, sqlite3
sys.path.insert(0, '.')
from crypto_bot.config.env import Config, EnvConfig
from crypto_bot.config.schemas import Settings
from crypto_bot.core.enums import Mode
from crypto_bot.simulation.backtester import Backtester
from crypto_bot.simulation.historical_source import HistoricalCandleSource
from crypto_bot.core.types import Candle
from crypto_bot.storage.db import Database

# --- Generate synthetic data (5m + 1h, uptrend) ---
base_ts = 1_700_000_000_000

# 5m candles
price = 100.0
candles_5m = []
for i in range(360 + 1):
    ts = base_ts + i * 300_000
    open_p = round(price, 2)
    close_p = round(price * 1.00125, 2)
    high_p = round(max(open_p, close_p) * 1.001, 2)
    low_p = round(min(open_p, close_p) * 0.999, 2)
    candles_5m.append(Candle(timestamp=ts, open=open_p, high=high_p, low=low_p, close=close_p, volume=200.0))
    price = close_p

# 1h candles
price = 100.0
candles_1h = []
for i in range(30):
    ts = base_ts + i * 3_600_000
    open_p = round(price, 2)
    close_p = round(price * 1.015, 2)
    high_p = round(max(open_p, close_p) * 1.005, 2)
    low_p = round(min(open_p, close_p) * 0.995, 2)
    candles_1h.append(Candle(timestamp=ts, open=open_p, high=high_p, low=low_p, close=close_p, volume=1000.0))
    price = close_p

# --- Config ---
s_dict = Settings().model_dump()
overrides = dict(
    runtime__mode=Mode.PAPER,
    runtime__strategy='per_timeframe',
    strategy__trend__ema_fast=3, strategy__trend__ema_mid=5, strategy__trend__ema_slow=8,
    strategy__trend__adx_min=5,
    strategy__momentum__rsi_period=4, strategy__momentum__rsi_long_min=0, strategy__momentum__rsi_long_max=100,
    strategy__volatility__atr_period=3, strategy__volatility__atr_min_pct=0.1, strategy__volatility__atr_max_pct=20.0,
    strategy__volatility__bb_period=4, strategy__volatility__bb_std=2.0,
    strategy__volume__ma_period=3, strategy__volume__spike_ratio=1.0,
    scoring__min_score=30, scoring__min_confidence=0.0, scoring__max_candidates_per_cycle=5,
    filters__enable_liquidity_filter=False, filters__enable_spread_filter=False, filters__enable_volume_filter=False,
    filters__cooldown_after_trade_minutes=0, filters__min_quote_volume_usd=0,
    risk__risk_per_trade_pct=1.0, risk__take_profit_risk_multiple=1000.0,
    risk__max_stop_distance_pct=5.0, risk__max_open_positions=1,
    risk__max_daily_drawdown_pct=20.0, risk__emergency_drawdown_pct=30.0,
)
for key, value in overrides.items():
    parts = key.split('__')
    d = s_dict
    for p in parts[:-1]:
        d = d[p]
    d[parts[-1]] = value
settings = Settings.model_validate(s_dict)
env = EnvConfig.model_validate(dict(crypto_bot_mode=Mode.PAPER))
config = Config(settings=settings, env=env)

start_ms = candles_5m[0].timestamp
end_ms = candles_5m[-1].timestamp + 300_000

source = HistoricalCandleSource()
source.load_all('BTC/USDT', '5m', candles_5m)
source.load_all('BTC/USDT', '1h', candles_1h)

tmp = tempfile.mktemp()
os.makedirs(tmp)
db_path = os.path.join(tmp, 'test.db')

bt = Backtester(config, symbols=['BTC/USDT'], timeframes=['5m', '1h'],
                start_ms=start_ms, end_ms=end_ms, source=source, db=Database(db_path))
bt.run()

# Read results
con = sqlite3.connect(db_path)
con.row_factory = sqlite3.Row

print("=== EQUITY CURVE ===")
eq_rows = con.execute("SELECT ts_ms, equity, drawdown_pct FROM equity ORDER BY ts_ms").fetchall()
for r in eq_rows:
    print(f"  ts={r['ts_ms']} eq={r['equity']:.2f} dd={r['drawdown_pct']:.4f}%")

print("\n=== POSITIONS ===")
pos_rows = con.execute("SELECT * FROM positions ORDER BY id").fetchall()
for r in pos_rows:
    print(f"  id={r['id']} sym={r['symbol']} tf={r['timeframe']} side={r['side']} "
          f"entry={r['entry_price']:.2f} exit={r['exit_price'] or 0:.2f} "
          f"sl={r['stop_loss']:.2f} tp={r['take_profit']:.2f} "
          f"pnl_pct={r['pnl_pct']} closed_by={r['closed_by']}")

print("\n=== POSITION OPEN/CLOSE TIMELINE ===")
# Map equity timestamps to position events
for r in pos_rows:
    opened_at = None
    closed_at = None
    entry_rows = con.execute(
        "SELECT ts_ms, entry_price FROM trades WHERE position_id=? AND side=?",
        (r['id'], r['side'])
    ).fetchall()
    for t in entry_rows:
        opened_at = t['ts_ms']
    if r['closed_by']:
        # Find the bar where it closed by matching equity ts
        closed_at = None
        for eq in reversed(eq_rows):
            if eq['ts_ms'] <= r['exit_time'] if 'exit_time' in r.keys() else True:
                pass
        print(f"  Position #{r['id']}: opened ~{opened_at} on {r['timeframe']}, "
              f"closed_by={r['closed_by']}, pnl_pct={r['pnl_pct']}")

con.close()
print(f"\nmax_dd from equity: {max(abs(r['drawdown_pct']) for r in eq_rows):.2f}%")
