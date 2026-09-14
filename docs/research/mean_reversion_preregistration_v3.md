# Mean Reversion Strategy — Pre-Registration v3

**Date:** 2026-09-01
**Status:** Active — supersedes v1 (invalidated) and v2 (internal contradictions)
**Author:** Systematic review per `mean_reversion_plan.md` §3 + critique

---

## Why v1 and v2 Invalidated

| Version | Fatal Flaw |
|---------|------------|
| v1 | 5913%/yr cost drag; "no params tuned" vs "rebalance must be validated" contradiction |
| v2 | Internal arithmetic contradictions (43.8% vs 65.7%); script formula ignores rebalance_hours; bars/hours confusion; "wait, let me recalc" three times in frozen doc |

---

## v3 Design: Coherent from Start

### Economic Hypothesis (unchanged)
Cross-sectional mean reversion on 1h crypto perp returns: z-score of 8h deviation predicts reversion within 8h. Economic mechanism: liquidation cascades, funding pressure — hours-horizon, not days.

### Execution Model: Maker-Only (Post-Only Both Sides)

| Aspect | Value | Rationale |
|--------|-------|-----------|
| Entry | Post-only limit | Maker fee 2 bps + 1 bps slippage |
| Exit | Post-only limit (reversion target) | Maker fee 2 bps + 1 bps slippage |
| Fallback | Market after timeout | Taker 10 bps + 5 bps (capped at 4h entry, 4h exit) |

### Registered Parameter Grid (Frozen — No Search on Test)

| Parameter | Value |
|-----------|-------|
| `timeframe` | `1h` |
| `zscore_window_bars` | 48 |
| `signal_lookback` | `8h` |
| `entry_threshold` | 3.0 |
| `exit_threshold` | 0.5 |
| `max_holding_bars` | 48 |
| `weighting` | `inverse_vol` |
| `rebalance_hours` | 24 |
| `max_positions` | 2 |
| `entry_execution` | `post_only` |
| `exit_execution` | `post_only` |
| `min_expected_edge_bps` | 10 |
| `seed` | 42 |

### Cost Model (Bybit Perpetual, Maker-Only)

| Component | Value |
|-----------|-------|
| Maker fee | 2 bps/side |
| Slippage (post-only) | 1 bps/side |
| Round-trip cost (entry+exit) | 6 bps |
| Max positions | 2 |
| Avg holding | 8h |
| Rebalance | 24h |

---

## Cost Analysis (Script-Generated — Single Source of Truth)

**Command:**
```bash
python scripts/estimate_mr_turnover.py --max-positions 2 --holding-hours 8 --rebalance-hours 24 --fee-bps 2 --slippage-bps 1
```

**Output (Single Source of Truth):**
```
======================================================================
  MEAN REVERSION TURNOVER / COST ESTIMATE
======================================================================
  Max concurrent positions: 2
  Timeframe: 1h
  Rebalance: every 24h
  Avg holding: 8.0 hours
  Max round-trips/position/day: 1.0000

  Cost assumptions:
    Fee: 2.0 bps/side
    Slippage: 1.0 bps/side
    Funding: 0.0 bps/day
  Turnover:
    Round-trips/day: 2.00
  Cost drag:
    Daily: 12.0 bps
    Annual: 43.80%
  Break-even:
    Required gross annual return: 43.80%
  SANITY CHECK:
  WARN: Annual cost drag > 20% (43.8%)
       High but potentially viable with strong edge.
======================================================================
```

### Key Numbers (Script-Generated)

| Metric | Value |
|--------|-------|
| Max concurrent positions | 2 |
| Avg holding | 8.0 hours |
| Rebalance | 24h |
| Max round-trips/position/day | 1.0000 |
| Round-trips/day | 2.00 |
| Fee per side | 2.0 bps |
| Slippage per side | 1.0 bps |
| Round-trip cost | 6 bps |
| Daily cost drag | 12.0 bps |
| **Annual cost drag** | **43.80%** |
| Required gross annual return | 43.80% |
| **Sanity Check** | **WARN: >20%** |

### Interpretation

- **43.80%/yr** annual cost drag — WARN tier (not PASS, not FAIL)
- **High but potentially viable with strong edge**
- Break-even requires **43.8% gross annual return** on a market-neutral 9-symbol book
- This is a **very high bar** — CSM's honest result was ~−50% PnL over ~9.5 months
- "Viable" means "a plausible edge *could* clear this," not "green light"
- **Fill-probability/adverse-selection risk remains unmodeled** — see Open Items

---

## Decision Rule (Immutable)

Strategy **ACCEPTED** iff ALL hold on **test** segment of 3-way walk-forward:

1. **Test Sharpe > 0** @ production DD=25% (NOT diagnostic 50%)
2. **Validation Sharpe > 0** @ production DD (independent window)
3. **n_trades(test) ≥ 200**
4. **Sign consistency**: Sharpe > 0 in ≥2 of 3 walk-forward sub-windows

If any condition fails → **REJECT** (no parameter re-tuning, no rule relaxation).

---

## Config Snapshot (Exact YAML for Reproduction)

```yaml
portfolio:
  strategy_name: mean_reversion_v0
  mean_reversion:
    timeframe: 1h
    zscore_window_bars: 48
    signal_lookback: "8h"
    entry_threshold: 3.0
    exit_threshold: 0.5
    max_holding_bars: 48
    weighting: inverse_vol
    rebalance_hours: 24
    max_positions: 2
    entry_execution: post_only
    exit_execution: post_only
    min_expected_edge_bps: 10
    seed: 42
  risk:
    max_positions: 2
    max_position_weight: 1.0
    max_gross_exposure: 1.0
    max_net_exposure: 1.0
    max_leverage: 5.0
    maintenance_margin_buffer_pct: 0.05
  regime:
    strategy_overrides:
      mean_reversion_v0:
        exposure_trend_low_vol: 0.25
        exposure_trend_high_vol: 0.0
        exposure_range_low_vol: 1.0
        exposure_range_high_vol: 0.5
```

---

## Open Items (Must Resolve Before Walk-Forward)

1. **Fill-probability model for post-only orders** — Not yet implemented. Current backtest would assume 100% fill at limit price. Need explicit model:
   - Entry: Fill if next bar's low ≤ limit price (long) / high ≥ limit (short) within 1h timeout
   - Exit: Fill if next bar's high ≥ target (long) / low ≤ target (short) within 4h timeout
   - Fallback: Market order at timeout (taker fee 10+5 bps)
   - **Adverse selection**: Post-only fills biased toward trades where price overshoots through limit

2. **Maker cost model implementation** — Need `CompositeCostModel.bybit_perp_maker_only()` with post-only logic

3. **Funding data gap** — Only through Feb 2024; test window Nov 2025+ has no funding data → PnL pre-funding only

4. **Data snooping** — Test window partially overlaps CSM test window

---

## Audit Trail

This document created **after** v2 internal contradictions identified.
File hash (SHA256): *to be computed on freeze*
Git commit: *to be recorded on freeze*
**Supersedes** `mean_reversion_preregistration.md` (v1, INVALIDATED) and `mean_reversion_preregistration_v2.md` (INVALIDATED — internal contradictions).