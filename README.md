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

Live mode additionally requires `ENABLE_LIVE_TRADING=true` in `.env` and passes
readiness checks. It is **not** enabled until paper trading and tests are green.

---

## Architecture (summary)

Strategy (pure, testable, no I/O): **indicators → signal engine → scorer → filters → risk**.
Infrastructure (I/O): **data ingestion, execution, storage, logging**.
An async **orchestrator** wires the layers in a loop:
`scan → features → signal → score → filter → risk → execute → persist`.

See the source tree under [`src/crypto_bot/`](src/crypto_bot/) and the config
contract in [`src/crypto_bot/config/schemas.py`](src/crypto_bot/config/schemas.py).

Key safety properties:
- Hard gates (liquidity, spread, volatility band, data quality) reject noise before scoring.
- Per-symbol **cooldown** + open-position check prevent duplicate entries.
- **Circuit breaker**: stops new entries at `max_daily_drawdown_pct`; hard halts at `emergency_drawdown_pct`.
- A **decision journal** records why each symbol was accepted *or* rejected.

---

## Project layout

```
config/                 # .env + YAML configs (copy the *.example files)
src/crypto_bot/
  core/                 # enums, value types, exceptions, circuit breaker
  config/               # pydantic schemas + settings loader
  data/                 # ccxt exchange wrapper, multi-TF feed, repository
  indicators/           # trend / momentum / volatility / volume / liquidity
  features/             # indicator -> normalized FeatureSet
  strategy/             # signal engine (multi-TF) + scorer
  filters/              # safety gates + cooldown
  risk/                 # position sizing + drawdown circuit breaker
  execution/            # Executor interface: signal_only | paper | live
  orchestrator/         # async loop + state
  backtest/             # historical replay + metrics
  storage/              # sqlite connection/schema + row models
tests/                  # pytest suite
```

---

## Getting started

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

cp .env.example .env               # then edit values
cp config/settings.example.yaml config/settings.yaml
cp config/universe.example.yaml   config/universe.yaml
```

(Config loader, CLI and modules are added in subsequent stages.)

---

## Implementation status

- **Stage 1 (current):** project skeleton, configuration contract (`.env`,
  YAML, pydantic schemas), domain enums & value types.
- **Stage 2 (next):** settings loader, storage (sqlite) schema, data ingestion
  (ccxt OHLCV/ticker/order book), indicators, features, tests.
- **Stage 3:** signal engine, scorer, filters, risk manager, orchestrator,
  signal-only executor.
- **Stage 4:** paper trading executor.
- **Stage 5:** backtesting harness + metrics.
- **Stage 6:** live executor (gated), after paper + tests are validated.
