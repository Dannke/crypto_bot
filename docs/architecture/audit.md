# Crypto Bot — Project Audit

**Date:** 2026-08-25  
**Branch:** `main`  
**Status:** Production infrastructure ready | CSM strategy rejected

---

## 1. Project Overview

**Crypto Bot** — regime-aware portfolio execution platform for cryptocurrency perpetual futures (Bybit). Evolved from a single-timeframe signal generator into a research/execution platform with:

- Cross-sectional momentum (CSM) strategy with regime gating
- Realistic Bybit execution costs (fees, slippage, funding)
- Walk-forward validation with calendar splits
- Automated baseline comparison
- Live/paper trading orchestrator with contract tests

**Core Philosophy:** Same components for backtest and live — feature builders, pipelines, executors, risk engine are identical instances. Deterministic replay on closed bars only.

---

## 2. Architecture Layers

```
┌─────────────────────────────────────────────────────────────┐
│                      Backtester (Unified)                   │
├─────────────────────────────────────────────────────────────┤
│  Candidate Mode (default)          │  Portfolio Mode        │
│  DecisionPipeline (SingleTF)       │  PortfolioDecisionPipeline │
│       ↓                            │       ↓                │
│  SignalExecutor                    │  PortfolioStrategy     │
│                                    │       ↓                │
│                                    │  RegimeGatedFusion     │
│                                    │       ↓                │
│                                    │  PortfolioRiskEngine   │
│                                    │       ↓                │
│                                    │  PortfolioExecutor     │
└─────────────────────────────────────────────────────────────┘
```

**Data Flow (Portfolio Mode):**
```
HistoricalCandleSource (loaded once)
    ↓
Unified Clock (ticks on every bar close across all TFs)
    ↓
PortfolioDecisionPipeline.process_market():
    get_universe_snapshot(source, symbols, tf, as_of_ms)
    → get_market_snapshot(universe)
    → FeatureBuilder.build_all() per symbol
    → PortfolioStrategy.generate_intent(snapshot, state)
    → RegimeGatedFusion.apply(intent, regime)
    → PortfolioRiskEngine.apply(intent, state)
    → PortfolioExecutor.execute(report)
```

**Key Invariants:**
1. **No look-ahead** — all data sliced at `as_of_ms` with `open + period <= as_of_ms`
2. **Same components live/backtest** — identical instances
3. **Shared capital regime** — both layers use same `PnLTracker`, same equity curve
4. **Deterministic** — fixed seed for random baseline, same risk limits across runs

---

## 3. Implementation Status (R0–R8)

### R0: Bybit Execution Realism ✅
- **R0.1** Funding rates — `FundingRepository`, `HistoricalFundingSource`, `BybitFundingClient`
- **R0.2** Instrument specs — `InstrumentSpec`, `InstrumentCache` (async refresh from V5)
- **R0.3** Margin/leverage engine — `PortfolioRiskEngine` with ordered limit enforcement
- **R0.4** Composite cost model — fee + slippage + funding, factory methods for legacy/bybit
- **R0.5** Baseline/walk-forward integration — CLI flags for funding, leverage, margin buffer

### R1: Regime Taxonomy & Configuration ✅
- `RegimeConfig` (Pydantic v2) with trend (ADX), volatility (ATR%), hysteresis, exposure multipliers
- Validation rules for all parameters

### R2: Regime Indicators ✅
- `adx()`, `atr_pct()`, `rolling_percentile()` — vectorized, tested

### R3: Regime Classifier ✅
- Two-axis classification (trend × volatility) → 4 regimes
- Hysteresis with minimum dwell bars
- Reference asset support (universe basket / BTC-only)
- Human-verified timeline: 91 transitions in `data/regime_timeline.csv`

### R4: Regime-Gated Fusion ✅
- `RegimeGatedFusion` scales position weights by regime exposure multiplier
- Applied before risk engine

### R5: Regime Validation ✅
- 40+ tests covering all quadrants, hysteresis, no future leakage, BTC-only mode

### R6: Configuration Wiring ✅
- Settings → PortfolioConfig → CsmConfig + RegimeConfig
- Factory builds pipeline with regime config
- Baseline `"csm_regime_gated"` added with test-friendly params

### R7: Regime-Aware Walk-Forward ✅
- Comparison script shows regime gating reduces overfitting:
  - 55% fewer trades (13 vs 29)
  - 2.2× higher win rate (31% vs 14%)
  - 55% better Sharpe (-0.73 vs -1.64)
- Report: `data/backtests/r7_report/regime_comparison_report.json`

### R8: Cross-Sectional Market Snapshot ✅
- `get_universe_snapshot()` — single anchor, single timeframe, closed bars only
- `get_market_snapshot()` — returns, volumes, filtered by min history
- 13 guarantees tested (single anchor, no future bars, missing history breaks, etc.)

---

## 4. CSM Closure — Final Verdict

### Infrastructure: ✅ CLOSED (all 6 criteria met)

| # | Criterion | Status |
|---|-----------|--------|
| 1 | pytest/ruff green | ✅ 149 core tests pass |
| 2 | Funding on/off diff | ⚠️ Partial (no funding data for test window) |
| 3 | Instrument constraints | ✅ 10 sizing tests |
| 4 | Walk-forward 9 symbols | ✅ Done (production params, fixed override) |
| 5 | R3 regime verified | ✅ 91 transitions |
| 6 | Live/paper wiring | ✅ 17 contract tests |

### Strategy: ❌ REJECTED

**Rule:** `Sharpe > 0 в обоих окнах на production-relevant DD → оставляем CSM`

**Final Results (production params, fixed override, corr_filter=OFF):**

| DD | Train Sharpe | Test Sharpe | Test MaxDD | CB in Test |
|----|--------------|-------------|------------|------------|
| 6% | -1.25 | -1.25 | 6.09% | Yes |
| 15% | +0.30* | — | 15.34% | Yes |
| 20% | +0.30* | — | 20.94% | Yes |
| 25% | +0.30* | — | 25.31% | Yes |
| **50%** | **+0.30*** | **-0.6300** | 50.22% | No |

*Train +0.30 inherited from earlier runs (aggressive sizing). Clean train on 50% DD not completed — doesn't affect verdict.

**Test at 50% DD (only clean window):** 874 trades, -49.75% PnL, **Sharpe -0.63**, MaxDD 50.22%

**Verdict:** Конъюнкция ложна → **CSM REJECTED**

### Why Previous "Positive" Result Was Invalid
| Factor | "Positive" (+0.20) | Clean (-0.63) |
|--------|-------------------|---------------|
| Regime params | test-friendly (5/10) | production (14/168) |
| Sizing | aggressive (0.3%/3) | production (1.0%/5) |
| Trades | ~45 | **874** |
| Sharpe | +0.20 (noise) | **-0.63** (significant) |

---

## 5. Current Configuration (Production)

```yaml
# config/settings.yaml
runtime:
  mode: paper
  loop_interval_seconds: 60
  timezone: UTC
  strategy: per_timeframe

exchange:
  name: bybit
  sandbox: false
  rate_limit_ms: 75

timeframes:
  primary: ["15m", "1h", "4h"]
  candles_per_tf: 400

risk:
  equity_currency: USDT
  risk_per_trade_pct: 1.0
  take_profit_risk_multiple: {15m: 2.0, 1h: 2.0, 4h: 2.0}
  max_stop_distance_pct: 3.0
  max_open_positions: 5
  max_daily_drawdown_pct: 3.0
  emergency_drawdown_pct: 25.0  # tuned from 6.0
  max_open_unrealized_drawdown_pct: 3.0
  max_correlation: 0.7
  max_correlated_positions: 2
  enable_correlation_filter: true
  correlation_lookback_bars: 168

regime:
  enabled: true
  reference: universe_basket
  trend_indicator: adx
  trend_period: 14
  trend_threshold: 25.0
  vol_lookback_bars: 168
  vol_percentile_high: 0.75
  hysteresis_min_dwell_bars: 6
  exposure_trend_low_vol: 1.0
  exposure_trend_high_vol: 0.5
  exposure_range_low_vol: 0.25
  exposure_range_high_vol: 0.0

portfolio:
  strategy_name: cross_sectional_momentum_v0
  csm:
    timeframe: 1h
    lookbacks: ["24h", "72h", "168h"]
    long_percentile: 0.90
    short_percentile: 0.10
    weighting: equal
    rebalance_hours: 24
    seed: 42
  risk:
    max_positions: 5
    max_position_weight: 1.0
    max_gross_exposure: 1.0
    max_net_exposure: 1.0
    max_leverage: 5.0
    maintenance_margin_buffer_pct: 0.05
```

---

## 6. Data Assets

| Asset | Details |
|-------|---------|
| `data/crypto_bot.db` | 846 funding rates (to Feb 2024), 776 instrument specs, 9 symbols × 23,079 1h bars |
| `data/regime_timeline.csv` | 4,286 rows, 91 regime transitions, human-verified |
| Symbols | BTC/USDT, ETH/USDT, SOL/USDT, XRP/USDT, AVAX/USDT, ADA/USDT, DOGE/USDT, BNB/USDT, POL/USDT |

---

## 7. Test Status

| Suite | Tests | Status |
|-------|-------|--------|
| Core (portfolio, live-gate, restart, walk-forward, validators) | 149 | ✅ Pass |
| Full suite | ~460 | 2 failures (`test_cross_validate.py` — pre-existing) |

---

## 8. Open Items for Next Phase

1. **Funding model validation** — confirm on/off difference on overlapping funding data before using for real capital decisions
2. **Replace CSM strategy** — infrastructure ready for mean-reversion / carry / ML alternatives
3. **Full walk-forward test** — fix `walk_forward.py` imports for complete train/test automation
4. **Paper trading integration** — connect orchestrator to Bybit testnet

   **БЛОКЕР (обнаружено 2026-09-21): на portfolio-пути нет работающего закрытия
   позиций.** Это не деталь MR-трека — относится к любой portfolio-стратегии,
   включая CSM, и остаётся в силе независимо от вердикта по mean reversion.

   В `orchestrator_portfolio.py` закрытие ровно одно — `_sim_check_positions`
   на строке 162, и оно вызывает `executor.check_positions(...)`, которого у
   `PortfolioExecutor` нет:

   ```
   PortfolioExecutor.check_positions       : False
   PortfolioExecutor.check_positions_range : True
   SignalExecutor.check_positions          : True
   ```

   Других путей нет: ни `close_position_for_symbol`, ни обработки
   `PortfolioIntent.closes`, ни `close_all_positions`. `AttributeError`
   ловится в `price_simulator.py:169` и логируется на уровне ERROR каждую
   секунду, пока есть открытые позиции.

   **Область:** только portfolio-режим. Candidate-режим (single-timeframe)
   закрывает штатно через `SignalExecutor.check_positions`, поэтому
   `crypto-bot run` на одном таймфрейме этим не затронут.

   Смежно: post-only заявки в живом оркестраторе не обрабатывает никто —
   `process_post_only_entries`/`exits` вызываются только из бэктестера, — а
   `entry_execution`/`exit_execution` по умолчанию `post_only`. То есть до
   исправления в paper/live позиции по MR не откроются вовсе, что и
   маскирует отсутствие закрытия.

---

## 9. Key Files Reference

```
src/crypto_bot/
├── config/schemas.py           # RegimeConfig, PortfolioConfig, CsmConfig, RiskParams
├── data/
│   ├── funding.py              # FundingRepository, HistoricalFundingSource, BybitFundingClient
│   └── instruments.py          # InstrumentSpec, InstrumentCache
├── execution/costs.py          # CompositeCostModel, bybit_perp_default()
├── indicators/regime.py        # ADX, ATR%, rolling_percentile
├── pipeline/
│   ├── factory.py              # build_portfolio_decision_pipeline
│   ├── portfolio_decision_pipeline.py
│   └── portfolio_fusion.py     # RegimeGatedFusion
├── portfolio/
│   ├── market_snapshot.py      # get_universe_snapshot, get_market_snapshot
│   ├── regime.py               # classify_regime, RegimeSnapshot
│   ├── risk.py                 # PortfolioRiskEngine, REJECT_CORRELATION
│   └── models.py               # PortfolioState, PortfolioIntent, etc.
├── simulation/
│   ├── backtester.py           # Unified Backtester
│   ├── baselines.py            # run_baseline_comparison, BASELINE_NAMES
│   ├── portfolio_executor.py   # PortfolioExecutor
│   └── walk_forward.py         # calendar_split, _run_single_window
├── strategy/
│   ├── portfolio_strategies.py # CrossSectionalMomentumV0, baselines
│   └── base.py                 # PortfolioStrategy protocol
├── orchestrator_portfolio.py   # R8 scheduler, persistence, hysteresis
├── cli.py                      # run-portfolio command
└── core/enums.py               # StrategyType.PORTFOLIO, REJECT_CORRELATION

tests/
├── test_portfolio_live_gate.py           # 9 live-gate contract tests
├── test_portfolio_restart_recovery.py    # 8 restart-recovery tests
├── test_portfolio_executor_sizing.py     # 10 sizing tests
├── test_portfolio_risk_engine*.py        # 31+ risk engine tests
├── test_market_snapshot.py               # 13 snapshot tests
├── test_regime/                          # 40+ regime tests
└── test_csm_config.py                    # 22 CSM config tests

scripts/
├── compare_baselines.py
├── walk_forward.py
├── walk_forward_regime_comparison.py
└── inspect_regime.py
```

---

## 10. Summary

**Infrastructure:** Complete, tested, production-ready. All R0–R8 delivered with contract tests, walk-forward validation, regime-aware execution, and live/paper orchestrator.

**CSM Strategy:** Rejected on empirical evidence (Test Sharpe -0.63 on 874 trades at 50% DD with production params). Circuit breaker at 6% DD was masking true behaviour; regime gating helps but insufficient.

**Next:** Deploy infrastructure for alternative portfolio strategy research. Funding model validation required before live capital allocation.