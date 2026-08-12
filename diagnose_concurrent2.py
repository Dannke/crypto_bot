"""Проверка: работал ли max_open_positions до фикса.
Сравниваем MAX(concurrent) при лимите=3 и лимите=999 (имитация pre-fix).
"""
import sys, os, tempfile, sqlite3
sys.path.insert(0, '.')
from crypto_bot.config.env import Config, EnvConfig
from crypto_bot.config.schemas import Settings
from crypto_bot.core.enums import Mode
from crypto_bot.core.types import Candle
from crypto_bot.simulation.backtester import Backtester
from crypto_bot.simulation.historical_source import HistoricalCandleSource
from crypto_bot.storage.db import Database

BASE_TS = 1_700_000_000_000

def candles_uptrend(sym, tf, n, sp):
    period = 300_000 if tf == "15m" else 3_600_000
    price = sp
    res = []
    for i in range(n):
        ts = BASE_TS + i * period
        o = round(price, 2)
        c = round(price * (1.002 if tf == "15m" else 1.008), 2)
        h = round(max(o, c) * 1.002, 2)
        l = round(min(o, c) * 0.998, 2)
        res.append(Candle(timestamp=ts, open=o, high=h, low=l, close=c, volume=1000.0))
        price = c
    return res

SYMBOLS = ["BTC/USDT", "ETH/USDT", "DOGE/USDT"]
TFS = ["15m", "1h"]
N_BARS = 200

source_all = {}
for sym in SYMBOLS:
    sp = {"BTC/USDT": 100.0, "ETH/USDT": 50.0, "DOGE/USDT": 1.0}[sym]
    for tf in TFS:
        source_all[(sym, tf)] = candles_uptrend(sym, tf, N_BARS, sp)

def make_config(max_open):
    sd = Settings().model_dump()
    ov = {
        "runtime__mode": Mode.PAPER, "runtime__strategy": "per_timeframe",
        "strategy__trend__ema_fast": 3, "strategy__trend__ema_mid": 5, "strategy__trend__ema_slow": 8,
        "strategy__trend__adx_min": 5,
        "strategy__momentum__rsi_period": 4, "strategy__momentum__rsi_long_min": 0, "strategy__momentum__rsi_long_max": 100,
        "strategy__volatility__atr_period": 3, "strategy__volatility__atr_min_pct": 0.1, "strategy__volatility__atr_max_pct": 20.0,
        "strategy__volatility__bb_period": 4, "strategy__volatility__bb_std": 2.0,
        "strategy__volume__ma_period": 3, "strategy__volume__spike_ratio": 1.0,
        "scoring__min_score": 30, "scoring__min_confidence": 0.0, "scoring__max_candidates_per_cycle": 10,
        "filters__enable_liquidity_filter": False, "filters__enable_spread_filter": False, "filters__enable_volume_filter": False,
        "filters__cooldown_after_trade_minutes": 0, "filters__min_quote_volume_usd": 0,
        "risk__risk_per_trade_pct": 1.0, "risk__take_profit_risk_multiple": {"15m": 3.0, "1h": 2.0},
        "risk__max_stop_distance_pct": 5.0, "risk__max_open_positions": max_open,
        "risk__max_daily_drawdown_pct": 20.0, "risk__emergency_drawdown_pct": 100.0,
    }
    for k, v in ov.items():
        parts = k.split("__")
        d = sd
        for p in parts[:-1]:
            d = d[p]
        d[parts[-1]] = v
    return Config(settings=Settings.model_validate(sd), env=EnvConfig.model_validate(dict(crypto_bot_mode=Mode.PAPER)))

def run_bt(max_open):
    source = HistoricalCandleSource()
    for sym in SYMBOLS:
        for tf in TFS:
            source.load_all(sym, tf, source_all[(sym, tf)])
    tmp = tempfile.mktemp()
    os.makedirs(tmp)
    db_path = os.path.join(tmp, "bt.db")
    bt = Backtester(make_config(max_open), symbols=SYMBOLS, timeframes=TFS,
                    start_ms=BASE_TS, end_ms=BASE_TS + N_BARS * 3_600_000,
                    source=source, db=Database(db_path))
    bt.run()
    return db_path

for limit, label in [(3, "WITH fix (max=3)"), (999, "WITHOUT fix (max=999 -> no limit)")]:
    db = run_bt(limit)
    con = sqlite3.connect(db)
    print(f"=== {label} ===")
    total = con.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
    print(f"  Total positions: {total}")

    # Count concurrent positions at each open time
    rows = con.execute("""
        SELECT p1.opened_at_ms,
               (SELECT COUNT(*) FROM positions p2
                WHERE p2.opened_at_ms <= p1.opened_at_ms
                  AND (p2.closed_at_ms IS NULL AND p2.status = 'open'
                       OR p2.closed_at_ms > p1.opened_at_ms)) AS cnt
        FROM positions p1 ORDER BY p1.opened_at_ms
    """).fetchall()

    max_c = max((r[1] for r in rows), default=0)
    print(f"  MAX(concurrent): {max_c}")
    print(f"  Sample opens:")
    for r in rows[:8]:
        print(f"    opened={r[0]} concurrent={r[1]}")
    if len(rows) > 8:
        print(f"    ...")
        for r in rows[-3:]:
            print(f"    opened={r[0]} concurrent={r[1]}")
    con.close()
