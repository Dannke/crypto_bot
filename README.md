# crypto_bot — Systematic Trading Research Platform

**v1.7** — A production-grade systematic trading research platform for cryptocurrency markets, evolved from a simple signal bot into a full portfolio construction, backtesting, and live-trading preparation framework.

> **Risk disclaimer.** This software does **not** guarantee profit. Cryptocurrency trading involves substantial risk of loss. Nothing here is financial advice. Use `signal_only` / `paper` modes until the system is thoroughly tested. Live trading is disabled by default and gated behind explicit kill-switches.

---

## What Changed Since v1.6 (Backtest Engine)

The project has been completely restructured from a **single-timeframe signal bot** into a **systematic trading research platform** with:

| Layer | v1.6 (Old) | v1.7 (New) |
|-------|------------|------------|
| **Decision** | Per-timeframe BUY/SELL/HOLD signals | Cross-sectional portfolio construction (CSM, MR) |
| **Risk** | Per-trade stop-loss / take-profit | Portfolio-level limits (gross/net exposure, max positions, leverage, correlation) |
| **Regime** | None | 4-regime taxonomy (trend/range × low/high vol) with hysteresis |
| **Backtesting** | Single-pair, single-TF replay | Walk-forward (2-way / 3-way), baselines, regime comparison, multi-symbol multi-TF |
| **Execution** | Paper position simulator | PortfolioExecutor with funding-aware cost models, margin checks |
| **Persistence** | Position journal only | Rebalance/regime scheduler state, universe snapshots, equity curve |
| **Strategies** | Confluence (multi-TF) | Cross-sectional momentum v0, Mean reversion v0, Random/Reverse baselines |
| **Data** | OHLCV fetch + cache | Instrument cache, funding rates, auto universe discovery |

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          SYSTEMATIC TRADING PLATFORM                         │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐    ┌───────────┐  │
│  │   Universe   │───▶│   Features   │───▶│  Portfolio   │───▶│  Fusion   │  │
│  │  Discovery   │    │  (cross-sec) │    │  Strategy    │    │ (Regime)  │  │
│  └──────────────┘    └──────────────┘    └──────────────┘    └───────────┘  │
│        │                    │                    │                   │       │
│        ▼                    ▼                    ▼                   ▼       │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐    ┌───────────┐  │
│  │ Instruments  │    │  Regime      │    │   Risk       │    │ Portfolio │  │
│  │  & Funding   │    │  Classifier  │    │   Engine     │    │  Intent   │  │
│  └──────────────┘    └──────────────┘    └──────────────┘    └───────────┘  │
│                                                                  │           │
│                                              ┌───────────────────┘           │
│                                              ▼                               │
│                                   ┌──────────────────────┐                   │
│                                   │  Portfolio Executor  │                   │
│                                   │  (paper / backtest)  │                   │
│                                   └──────────────────────┘                   │
│                                              │                               │
│                                              ▼                               │
│                                   ┌──────────────────────┐                   │
│                                   │   Walk-Forward       │                   │
│                                   │   Analysis Engine    │                   │
│                                   └──────────────────────┘                   │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Core Modules

| Module | Purpose |
|--------|---------|
| `src/crypto_bot/portfolio/` | Portfolio-domain DTOs (UniverseSnapshot, CrossSectionalFeatureSnapshot, PortfolioIntent, PortfolioState, RegimeSnapshot, OrderRequest, Fill) |
| `src/crypto_bot/portfolio/risk.py` | PortfolioRiskEngine — max positions, position weight, gross/net exposure, leverage/margin, correlation filter, inverse-vol sizing |
| `src/crypto_bot/portfolio/regime.py` | RegimeClassifier — 4-regime taxonomy with ADX trend + ATR% vol percentile, hysteresis |
| `src/crypto_bot/portfolio/mean_reversion_features.py` | Z-score computation for cross-sectional mean reversion |
| `src/crypto_bot/pipeline/portfolio_decision_pipeline.py` | PortfolioDecisionPipeline — features → strategy → fusion (regime gating) |
| `src/crypto_bot/pipeline/portfolio_fusion.py` | RegimeGatedFusion — exposure multipliers per regime |
| `src/crypto_bot/strategy/portfolio_strategies.py` | Built-in strategies: `cross_sectional_momentum_v0`, `mean_reversion_v0`, `random_baseline`, `reverse_momentum_v0` |
| `src/crypto_bot/simulation/backtester.py` | Multi-symbol multi-TF backtester with shared-capital clock |
| `src/crypto_bot/simulation/walk_forward.py` | Walk-forward analysis (2-way / 3-way splits) with overfit diagnostics |
| `src/crypto_bot/simulation/baselines.py` | Buy-and-hold, random, reverse baselines for benchmarking |
| `src/crypto_bot/simulation/portfolio_executor.py` | Portfolio-layer execution with funding-aware costs, margin enforcement |
| `src/crypto_bot/orchestrator_portfolio.py` | Portfolio-mode orchestrator with restart-safe rebalance/regime scheduler |
| `src/crypto_bot/data/instruments.py` | Instrument metadata cache (tick size, lot size, funding intervals) |
| `src/crypto_bot/data/funding.py` | Funding rate fetch + cost integration |
| `src/crypto_bot/execution/costs.py` | CompositeCostModel (Bybit perp: maker/taker + funding + slippage) |
| `src/crypto_bot/indicators/regime.py` | Pure regime indicator functions (ADX trend strength, rolling ATR% percentile) |

---

## Operating Modes

| Mode | Behavior | Use Case |
|------|----------|----------|
| `signal_only` | Compute & log portfolio intents; no order simulation | Research, signal validation |
| `paper` | Simulate orders against live market data (no money) | Strategy validation, risk engine testing |
| `live` | Place **real** orders (gated; requires `ENABLE_LIVE_TRADING=1`) | Production (not recommended without extensive paper testing) |

---

## Portfolio Strategies (v0)

### Cross-Sectional Momentum (`cross_sectional_momentum_v0`)
- **Returns**: Geometric cumulative return over configurable lookbacks (e.g., 24h, 72h, 168h)
- **Rank**: Symbols sorted by composite return (equal-weighted across lookbacks)
- **Selection**: Top `top_fraction` LONG, bottom `short_fraction` SHORT (per-side rounds up)
- **Weighting**: Equal weight per selected symbol
- **Rebalance**: Hourly (configurable via `csm.rebalance_hours`)

### Mean Reversion (`mean_reversion_v0`)
- **Signal**: horizon-consistent z of the `signal_lookback` log return, scaled by `sqrt(h)` times the RMS of the `zscore_window_bars` one-bar log returns that precede the signal window
- **Entry**: LONG when z ≤ -entry_threshold, SHORT when z ≥ entry_threshold, at most the top/bottom percentile of the cross-sectional rank per tick
- **Exit**: time-stop (`max_holding_bars`); reversion exit on |z| ≤ `exit_threshold` only if it is not `null`
- **Weighting**: equal, or inverse-volatility (24h rolling std of returns)
- **Status**: research verdict REJECTED (cycle 2) — see [`docs/README.md`](docs/README.md)

### Null Baselines
- `random_baseline`: Uniform random long/short selection (seeded for reproducibility)
- `reverse_momentum_v0`: Contrarian — longs worst, shorts best (exact mirror of momentum)

---

## Market Regime Classification (R1)

Two-axis taxonomy producing **4 regimes**:

| Regime | Trend | Volatility | Default Exposure |
|--------|-------|------------|------------------|
| `trend_low_vol` | ADX > 25 | ATR% ≤ 75th pctl | 1.0 (full) |
| `trend_high_vol` | ADX > 25 | ATR% > 75th pctl | 0.5 |
| `range_low_vol` | ADX ≤ 25 | ATR% ≤ 75th pctl | 0.25 |
| `range_high_vol` | ADX ≤ 25 | ATR% > 75th pctl | 0.0 (cash) |

- **Hysteresis**: Minimum dwell bars (`hysteresis_min_dwell_bars`, default 6) prevents regime flickering
- **Per-strategy overrides**: Configure different multipliers per strategy via `regime.strategy_overrides`

---

## Portfolio Risk Engine

Deterministic constraint order:

1. **Max positions** — hard cap on concurrent positions
2. **Max position weight** — per-symbol cap
3. **Max gross exposure** — sum of absolute weights
4. **Max net exposure** — net long minus short
5. **Max leverage / margin** — margin fraction with maintenance buffer
6. **Correlation filter (R8)** — leg-aware (limits within-leg correlation; cross-leg allowed)
7. **Inverse-vol sizing** — optional, renormalized to approved gross

Every rejection produces a `PortfolioRejectReason` for auditability.

---

## Walk-Forward Analysis

```bash
# 2-way split (train/test)
crypto-bot backtest BTC/USDT 1h --start 2025-01-01 --end 2025-02-01

# 3-way split (train/validation/test) via script
python scripts/walk_forward_regime_comparison.py --config config/settings.yaml \
  --symbols BTC/USDT ETH/USDT --timeframe 1h --split 0.6 --val 0.2
```

Outputs overfit diagnostics: PnL drop, Sharpe drop, WinRate drop between train/test.

---

## CLI Commands

```bash
# Legacy single-TF scan (candidate layer)
crypto-bot --config config/settings.yaml
crypto-bot run --config config/settings.yaml

# Portfolio mode (cross-sectional)
crypto-bot run_portfolio --config config/settings.yaml

# Backtest (single pair, single TF)
crypto-bot backtest BTC/USDT 1h --start 2025-01-01 --end 2025-02-01

# Seed historical data from exchange
crypto-bot seed_history BTC/USDT 1h --start 2025-01-01 --end 2025-02-01 --network mainnet

# View positions (table / csv / json)
crypto-bot positions --config config/settings.yaml --format table --out positions.csv

# P&L summary
crypto-bot summary --config config/settings.yaml

# Force-close all open positions
crypto-bot close_all --config config/settings.yaml --force

# Clear position journal
crypto-bot clear_positions --config config/settings.yaml --force
```

---

## Configuration

Main config: `config/settings.yaml` (copy from `config/settings.example.yaml`)

Key sections:

```yaml
runtime:
  mode: paper                    # signal_only | paper | live
  strategy_type: portfolio       # candidate | portfolio
  strategy: cross_sectional_momentum_v0

portfolio:
  strategy_name: cross_sectional_momentum_v0  # or mean_reversion_v0, random_baseline
  csm:
    timeframe: 1h
    lookbacks: ["24h", "72h", "168h"]
    long_percentile: 0.90
    short_percentile: 0.10
    rebalance_hours: 24
  # mean_reversion: current values live in config/settings.yaml; the research snapshot is in its pre-registration

regime:
  enabled: true
  reference: universe_basket
  trend_period: 14
  trend_threshold: 25.0
  vol_lookback_bars: 168
  vol_percentile_high: 0.75
  hysteresis_min_dwell_bars: 6
  exposure_trend_low_vol: 1.0
  exposure_trend_high_vol: 0.5
  exposure_range_low_vol: 0.25
  exposure_range_high_vol: 0.0

portfolio.risk:
  max_positions: 5
  max_position_weight: 0.5
  max_gross_exposure: 1.0
  max_net_exposure: 1.0
  max_leverage: 10.0
  maintenance_margin_buffer_pct: 0.1
  enable_correlation_filter: true
  max_correlation: 0.7
  max_correlated_positions: 2
```

---

## Testing

```bash
# Unit tests
pytest tests/ -v

# Portfolio-specific tests
pytest tests/test_portfolio/ -v
pytest tests/test_backtest/ -v
pytest tests/test_regime/ -v

# Strategy tests
pytest tests/test_mean_reversion_v0.py tests/test_momentum_v0.py -v

# Walk-forward / regime comparison
pytest tests/test_backtest/test_walk_forward.py -v
pytest tests/test_backtest/test_regime_comparison.py -v
```

---

## Project Structure

```
src/crypto_bot/
├── cli.py                      # Click CLI (run, run_portfolio, backtest, seed_history, positions, summary, close_all, clear_positions)
├── config/
│   ├── env.py                  # Settings loader with env overrides
│   └── schemas.py              # Pydantic config schemas (validated, no unknown keys)
├── core/
│   ├── enums.py                # Mode, StrategyType, Side, OrderType, PortfolioRejectReason, RejectReason
│   ├── policy.py               # Timeframe parsing, allowed timeframes, duration parsing
│   ├── validators.py           # Runtime safety gates (live trading kill-switch)
│   └── logging_setup.py        # Structured logging (JSON/text)
├── data/
│   ├── feed.py                 # Multi-symbol multi-TF OHLCV fetch with retry/cooldown
│   ├── exchange.py             # MarketDataClient (Bybit REST)
│   ├── exchange_sync.py        # Exchange info sync
│   ├── instruments.py          # Instrument metadata cache (tick/lot size, funding)
│   └── funding.py              # Funding rate fetch + cost model integration
├── execution/
│   └── costs.py                # CompositeCostModel (maker/taker + funding + slippage)
├── features/
│   ├── builder.py              # FeatureBuilder (single-symbol indicators)
│   ├── batch.py                # build_features_batch (cross-sectional)
│   └── context.py              # SymbolMarketContext (ticker → feature context)
├── indicators/
│   ├── __init__.py             # EMA, RSI, ATR%, ADX, BB, Volume MA
│   └── regime.py               # ADX trend strength, rolling ATR% percentile, classify_regime
├── orchestrator.py             # Candidate-layer orchestrator (per-TF signals)
├── orchestrator_portfolio.py   # Portfolio-layer orchestrator (cross-sectional + restart-safe scheduler)
├── pipeline/
│   ├── candidate_builder.py    # Per-TF signal generation
│   ├── decision_pipeline.py    # FusionEngine (candidate layer)
│   ├── factory.py              # Builder/factory functions
│   ├── portfolio_decision_pipeline.py  # PortfolioDecisionPipeline (features → strategy → fusion)
│   └── portfolio_fusion.py     # RegimeGatedFusion (regime exposure multipliers)
├── portfolio/
│   ├── models.py               # Immutable DTOs (UniverseSnapshot, PortfolioIntent, PortfolioState, RegimeSnapshot, ...)
│   ├── risk.py                 # PortfolioRiskEngine (limits, correlation, vol sizing, margin)
│   ├── regime.py               # RegimeClassifier (4-regime taxonomy + hysteresis)
│   ├── mean_reversion_features.py  # Z-score snapshot computation
│   └── market_snapshot.py      # MarketSnapshot (closed-bar snapshot for market-based strategies)
├── simulation/
│   ├── backtester.py           # Multi-symbol multi-TF backtester (shared-capital clock)
│   ├── walk_forward.py         # Walk-forward analysis (2/3-way splits, overfit diagnostics)
│   ├── baselines.py            # Buy-hold, random, reverse baselines
│   ├── portfolio_executor.py   # PortfolioExecutor (funding costs, margin, SL/TP)
│   ├── executor.py             # SignalExecutor (candidate layer paper trading)
│   ├── price_simulator.py      # OU process price simulation for paper SL/TP
│   ├── paper_position.py       # PaperPosition (candidate layer)
│   ├── historical_source.py    # HistoricalCandleSource (DB-backed)
│   └── seed_history.py         # Historical data seeding from exchange
├── strategy/
│   ├── base.py                 # PortfolioStrategy protocol, StrategyContext
│   ├── registry.py             # Strategy registry
│   ├── manager.py              # Strategy manager
│   └── portfolio_strategies.py # CSM v0, MR v0, Random, Reverse
└── storage/
    ├── db.py                   # Database + Repositories (positions, equity, candles, state)
    └── migrations.sql          # Schema with rebalance/regime state keys

tests/
├── test_backtest/              # Backtest golden, walk-forward, regime comparison, baselines
├── test_portfolio/             # Regime classifier, portfolio constraints, risk engine, live gate
├── test_regime/                # Hysteresis, no future leakage, regime classification, exposure scaling
├── test_execution/             # Cost models, funding cost model
└── test_data/                  # Funding source, instruments
```

---

## Documentation

Start at [`docs/README.md`](docs/README.md): current state, reading order, structure and
conventions. Architecture — [`docs/architecture/overview.md`](docs/architecture/overview.md),
open items — [`docs/architecture/backlog.md`](docs/architecture/backlog.md), research verdicts
— [`docs/research/`](docs/research/).

---

## Quick Start

```bash
# 1. Install
pip install -e ".[dev]"

# 2. Configure
cp .env.example .env
cp config/settings.example.yaml config/settings.yaml
cp config/universe.example.yaml config/universe.yaml

# 3. Seed historical data (required for backtesting)
crypto-bot seed_history BTC/USDT 1h --start 2025-01-01 --end 2025-06-01 --network mainnet

# 4. Run backtest
crypto-bot backtest BTC/USDT 1h --start 2025-01-01 --end 2025-02-01

# 5. Run portfolio mode (paper)
crypto-bot run_portfolio --config config/settings.yaml

# 6. View results
crypto-bot positions --format table
crypto-bot summary
```

---

## Version History

| Version | Date | Highlights |
|---------|------|------------|
| **v1.7** | 2026-09 | Systematic trading platform: portfolio layer, regime, walk-forward, risk engine, baselines |
| **v1.6** | 2026-08 | Backtest Engine final (single-pair, single-TF) |
| **v1.5** | 2026-07 | Stage 2 Backtest Engine + bug fixes |
| **v1.4** | 2026-07 | PriceSimulator (OU process), SL/TP per-second, P&L in positions |
| **v1.3** | 2026-07 | Virtual local trading with TP/SL |
| **v1.2** | 2026-07 | Multi-timeframe analysis, added 1m |

---

## License

MIT License — see `LICENSE` for details.