"""Debug: почему max_open_positions не ограничивает."""
import sys, os, tempfile
sys.path.insert(0, '.')
from datetime import UTC, datetime
from crypto_bot.config.env import Config, EnvConfig
from crypto_bot.config.schemas import Settings
from crypto_bot.core.enums import Mode, Side, Signal
from crypto_bot.decision.decision_report import DecisionReport
from crypto_bot.simulation.executor import SignalExecutor
from crypto_bot.simulation.pnl import PnLTracker
from crypto_bot.storage.db import Database, Repositories

sd = Settings().model_dump()
for k, v in {
    "runtime__mode": Mode.PAPER, "risk__max_open_positions": 2, "risk__max_stop_distance_pct": 5.0,
    "risk__risk_per_trade_pct": 1.0, "risk__take_profit_risk_multiple": 2.0,
    "scoring__min_score": 30,
}.items():
    parts = k.split("__")
    d = sd
    for p in parts[:-1]:
        d = d[p]
    d[parts[-1]] = v
settings = Settings.model_validate(sd)
env = EnvConfig.model_validate(dict(crypto_bot_mode=Mode.PAPER))
config = Config(settings=settings, env=env)

tmp = tempfile.mktemp()
os.makedirs(tmp)
db_path = os.path.join(tmp, "test.db")
repos = Repositories(Database(db_path))
executor = SignalExecutor(config, repos, PnLTracker())

print(f"max_open_positions = {config.settings.risk.max_open_positions}")
print(f"initial open count: {len([p for p in executor.tracker.positions if p.is_open])}")

def fake_report(symbol, tf, side=Side.LONG):
    return DecisionReport(
        timestamp=datetime.now(tz=UTC),
        symbol=symbol, signal=Signal.BUY, side=side,
        confidence=1.0, total_score=70.0,
        features={"close": 100.0, "last_close": 100.0, "atr_pct": 1.0,
                  "spread_pct": 0.0, "timeframe": tf,
                  "candle_timestamp_ms": int(datetime.now().timestamp()*1000)},
    )

for i in range(5):
    r = fake_report(f"SYM{i}", "1h")
    res = executor.handle_selected(r)
    cnt = len([p for p in executor.tracker.positions if p.is_open])
    print(f"  report {i} ({r.symbol}): handled={res.handled}, open_count={cnt}, msg={res.message}")
