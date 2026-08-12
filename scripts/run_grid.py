"""Run grid: RSI width × spike_ratio on portfolio 1h."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT"]
TF = "1h"
BASE = [sys.executable, "scripts/walk_forward.py", "--symbols", *SYMBOLS, "--timeframe", TF]

RSI = {
    "narrow": dict(rsi_long_min=55, rsi_long_max=65, rsi_short_min=35, rsi_short_max=45),
    "wide":   dict(rsi_long_min=40, rsi_long_max=75, rsi_short_min=25, rsi_short_max=55),
}
SPIKE = [1.2, 1.5, 2.0]

def parse_result(output: str) -> dict:
    lines = output.split("\n")
    result = {}
    for line in lines:
        stripped = line.strip()
        # Table lines look like: "  Bars             3244          2633      --"
        if stripped.startswith("Bars"):
            parts = stripped.split()
            result["bars_train"] = parts[1]
            result["bars_test"] = parts[2]
        elif stripped.startswith("Trades"):
            parts = stripped.split()
            result["trades_train"] = parts[1]
            result["trades_test"] = parts[2]
        elif stripped.startswith("Total P&L"):
            parts = stripped.split()
            result["pnl_train"] = parts[2]
            result["pnl_test"] = parts[3]
        elif stripped.startswith("Win rate"):
            parts = stripped.split()
            result["wr_train"] = parts[2]
            result["wr_test"] = parts[3]
        elif stripped.startswith("Max DD"):
            parts = stripped.split()
            result["dd_train"] = parts[2]
            result["dd_test"] = parts[3]
        elif stripped.startswith("Sharpe"):
            parts = stripped.split()
            result["sharpe_train"] = parts[1]
            result["sharpe_test"] = parts[2]
        elif stripped.startswith("Wins"):
            parts = stripped.split()
            result["wins_train"] = parts[1]
            result["wins_test"] = parts[2]
        elif stripped.startswith("Losses"):
            parts = stripped.split()
            result["losses_train"] = parts[1]
            result["losses_test"] = parts[2]
        elif stripped.startswith("Closed by SL"):
            parts = stripped.split()
            result["sl_train"] = parts[3]
            result["sl_test"] = parts[4]
        elif stripped.startswith("Closed by TP"):
            parts = stripped.split()
            result["tp_train"] = parts[3]
            result["tp_test"] = parts[4]
        elif "NOT ENOUGH DATA" in stripped or "POSSIBLE OVERFIT" in stripped:
            result["hint"] = stripped
    return result

print(f"{'RSI':>8} {'Spike':>6} {'TrnTrd':>7} {'TstTrd':>7} {'TrnP&L':>8} {'TstP&L':>8} "
      f"{'TrnWR':>7} {'TstWR':>7} {'TrnDD':>7} {'TstDD':>7} {'TrnSh':>7} {'TstSh':>7} {'SL':>4} {'TP':>4}")
print("-" * 98)

for width_name, rsi_params in RSI.items():
    for sr in SPIKE:
        cmd = BASE.copy()
        cmd += ["--override", f"scoring__min_score=55"]
        cmd += ["--override", f"strategy__volume__spike_ratio={sr}"]
        for k, v in rsi_params.items():
            cmd += ["--override", f"strategy__momentum__{k}={v}"]

        print(f"  Running RSI={width_name} spike={sr} ...", file=sys.stderr)
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            print(f"  FAILED: {r.stderr[:200]}", file=sys.stderr)
            continue
        out = r.stdout + r.stderr
        p = parse_result(out)

        trn_t = p.get("trades_train", "?")
        tst_t = p.get("trades_test", "?")
        trn_p = p.get("pnl_train", "?")
        tst_p = p.get("pnl_test", "?")
        trn_w = p.get("wr_train", "?")
        tst_w = p.get("wr_test", "?")
        trn_d = p.get("dd_train", "?")
        tst_d = p.get("dd_test", "?")
        trn_s = p.get("sharpe_train", "?")
        tst_s = p.get("sharpe_test", "?")
        sl_t = p.get("sl_test", "?")
        tp_t = p.get("tp_test", "?")

        print(f"{width_name:>8} {sr:>6.1f} {trn_t:>7} {tst_t:>7} {trn_p:>8} {tst_p:>8} "
              f"{trn_w:>7} {tst_w:>7} {trn_d:>7} {tst_d:>7} {trn_s:>7} {tst_s:>7} {sl_t:>4} {tp_t:>4}")
