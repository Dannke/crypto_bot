# crypto_bot

Selective multi-coin cryptocurrency signal & trading framework for Python 3.11+.

The goal is **not frequent trading** — it is finding a small number of
high-quality, risk-controlled setups across a large watchlist and emitting
`BUY` / `SELL` / `HOLD` signals, with optional paper and live execution.

> **Risk disclaimer.** This software does **not** guarantee profit. Cryptocurrency
> trading involves substantial risk of loss. Nothing here is financial advice.
> Use `signal_only` / `paper` modes until the system is thoroughly tested.
> Live trading is disabled by default and gated behind explicit kill-switches.

---

## Operating modes

| Mode          | Behavior                                              | Default |
|---------------|-------------------------------------------------------|---------|
| `signal_only` | Compute & log signals; no order simulation           | ✅      |
| `paper`       | Simulate orders against live market data (no money)  |         |
| `live`        | Place **real** orders (gated; added in a later stage)|         |

---

## Status

**Stage 2 completed** — full infrastructure, data feed, indicators, features, strategy scaffold, storage, orchestrator and CLI.

Next: Stage 3 (paper trading executor + improved tests).

## Quick Start

```bash
pip install -e ".[dev]"
cp .env.example .env
cp config/settings.example.yaml config/settings.yaml
cp config/universe.example.yaml config/universe.yaml

crypto-bot
```

Live trading remains **disabled** until paper mode and tests pass.
