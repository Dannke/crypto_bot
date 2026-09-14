import sys
sys.path.insert(0, '.')
import importlib
import sys

# Force reload all modules
for mod in list(sys.modules.keys()):
    if 'signal_frequency' in mod or 'scripts' in mod or 'portfolio' in mod or 'historical' in mod or 'market' in mod:
        del sys.modules[mod]

import scripts.signal_frequency_check as sfc
import importlib
importlib.reload(sfc)

from scripts.signal_frequency_check import run_signal_frequency_check

result = run_signal_frequency_check(
    db_path='data/crypto_bot.db',
    symbols=['BTC/USDT'],
    start_ms=1735689600000,
    end_ms=1735776000000,
    timeframe='1h',
    zscore_window_bars=48,
    signal_lookback_hours=4,
    entry_threshold=3.0,
)
print('Result:', result)