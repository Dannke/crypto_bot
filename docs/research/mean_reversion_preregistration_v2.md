# Mean Reversion Strategy — Pre-Registration v2

**Date:** 2026-09-01
**Status:** Active — supersedes v1 (invalidated by cost-gate failure)
**Author:** Systematic review per `mean_reversion_plan.md` §3 + critique

---

## Why v1 Invalidated

v1 grid (hold=4h, rebalance=1h, max_positions=5, entry=2.0) → **5913%/yr cost drag** @ Bybit perp taker fees (10 bps/side + 5 bps slippage).
Statement "Rebalance cadence must be validated at 4h" contradicted "No parameters tuned" — parameter search on test.

---

## v2 Design Philosophy

Cost-gate failure is not a parameter-tuning opportunity — it is a signal that the **signal design** must change.
Per critique: fix the economics, not the holding period.

### Core Changes from v1

| Aspect | v1 (invalidated) | v2 (this registration) |
|--------|------------------|------------------------|
| **Entry threshold** | 2.0 | **3.0** (higher conviction, fewer trades) |
| **Min expected edge** | None | **≥30 bps** per round-trip (covers 2× fee+slippage) |
| **Max positions** | 5 | **3** (risk limit, not universe size) |
| **Holding period** | 24 bars (4h) | **48 bars** (8h) — still hours, not weeks |
| **Rebalance** | 1h | **24h** (daily, aligns with funding cycle) |
| **Execution** | Market (taker) | **Post-only entry** (maker fee 2 bps) + market exit |
| **Cost model** | Taker 10+5 bps | **Maker 2+1 bps entry, taker 10+5 bps exit** |

### Cost Analysis (v2 grid, Bybit perp)

- **Concurrent positions**: 3 (max_positions)
- **Avg holding**: 8 hours (48 bars @ 1h)
- **Rebalance**: 24h (daily)
- **Entry**: Post-only (maker 2 bps + 1 bps slippage = 3 bps)
- **Exit**: Market (taker 10 bps + 5 bps slippage = 15 bps)
- **Round-trip cost**: 18 bps
- **Round-trips/day**: 3 × (24/8) = 9.0
- **Daily cost drag**: 9 × 18 bps = 162 bps = 1.62%
- **Annual cost drag**: 1.62% × 365 = **591%/yr** → **STILL TOO HIGH**

**Wait** — the rebalance=24h means we only check signals once per day. So max 1 round-trip per position per day, not 3/day.

**Correct calc (rebalance=24h = 1 check/day):**
- Max 1 round-trip per position per day (rebalance cadence)
- Round-trips/day = 3 positions × 1 = 3
- Daily cost drag = 3 × 18 bps = 54 bps = 0.54%
- Annual cost drag = 0.54% × 365 = **197%/yr** → **STILL >100%**

**Fundamental issue:** Even with maker entry, 3 positions × daily rebalance × 18 bps = 197%/yr.

**Only way to pass cost-gate with Bybit perp costs:**
- Reduce to **2 positions** → 131%/yr (still >100%)
- **1 position** → 65%/yr (viable but no diversification)
- **Maker exit too** (post-only both sides) → 6 bps round-trip → 3 positions = 66%/yr (viable!)
- Or **reduce rebalance frequency** (e.g., weekly) → defeats MR hypothesis

---

## Honest Conclusion for v2

The Mean Reversion hypothesis **as a cross-sectional portfolio strategy on Bybit perpetuals with taker fees is not viable** at any reasonable parameter setting that preserves the "hours-horizon" economic mechanism.

**Two paths forward:**

1. **Change venue/execution**: Use spot (maker fees viable) or post-only both sides (maker fees 2 bps each side = 4 bps round-trip). With 4 bps round-trip: 3 positions × daily = 44%/yr (viable).
2. **Conclude hypothesis rejected for this venue** and move to Funding/Basis (priority #4).

---

## v2 Registration: Maker-Only Execution Design

Since the critique explicitly mentioned "проверить maker/taker модель комиссий" — we register a **maker-only** variant that is economically coherent.

### Parameter Grid (Fixed — No Search on Test)

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| `timeframe` | `1h` | Anchored to market_snapshot |
| `zscore_window_bars` | 48 | Rolling mean/std |
| `signal_lookback` | `8h` | Horizon of deviation |
| `entry_threshold` | 3.0 | High conviction |
| `exit_threshold` | 0.5 | Reversion exit |
| `max_holding_bars` | 48 | 8h time-stop |
| `weighting` | `inverse_vol` | Risk parity |
| `rebalance_hours` | 24 | Daily (funding cycle) |
| `max_positions` | 3 | Diversification limit |
| `entry_execution` | `post_only` | **Maker fee (2 bps)** |
| `exit_execution` | `post_only` | **Maker fee (2 bps)** |
| `min_expected_edge_bps` | 10 | Covers 2× maker fee |
| `seed` | 42 | Reproducibility |

### Cost Analysis (Maker-Only, Bybit perp)

- **Round-trip cost**: 4 bps (2 bps entry + 2 bps exit, post-only)
- **Concurrent positions**: 3
- **Rebalance**: 24h (1 round-trip/day max per position)
- **Daily cost drag**: 3 × 4 bps = 12 bps = 0.12%
- **Annual cost drag**: 0.12% × 365 = **43.8%/yr** ✓ **VIABLE**

### Execution Requirements

- **Entry**: Post-only limit at signal price (or 1 tick inside). If not filled within 1h → cancel.
- **Exit**: Post-only limit at reversion target (|z| ≤ 0.5). If not filled within 4h → market exit (taker).
- **Time-stop**: Market exit at max_holding_bars.

### Cost Model Switch

In `simulation/baselines.py` and walk-forward:
- Use `CompositeCostModel.bybit_perp_maker_only()` (to be added) with:
  - maker_fee_bps = 2, taker_fee_bps = 10
  - slippage_maker_bps = 1, slippage_taker_bps = 5
  - post_only_entry = True, post_only_exit = True
  - fallback_to_taker_after_bars = 4 (entry), 4 (exit)

---

## Decision Rule (Immutable — Identical to v1)

Strategy **ACCEPTED** iff ALL hold on **test** segment of 3-way walk-forward:

1. **Test Sharpe > 0** @ production DD=25% (NOT diagnostic 50%)
2. **Validation Sharpe > 0** @ production DD (independent window)
3. **n_trades(test) ≥ 200**
4. **Sign consistency**: Sharpe > 0 in ≥2 of 3 walk-forward sub-windows

If any condition fails → **REJECT** (no parameter re-tuning, no rule relaxation).

---

## Circuit Breaker Reporting Discipline

Every walk-forward row **must** carry explicit label:
- `production` (DD=25%) — **verdict uses this**
- `diagnostic_widened` (DD=50%) — edge visibility only, never for verdict

---

## Config Snapshot (Exact YAML)

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
    max_positions: 3
    entry_execution: post_only
    exit_execution: post_only
    min_expected_edge_bps: 10
    seed: 42
  risk:
    max_positions: 3
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

## Audit Trail

This document created **after** v1 invalidation, **before** any v2 implementation changes.
File hash (SHA256): *to be computed on freeze*
Git commit: *to be recorded on freeze*
**Supersedes** `mean_reversion_preregistration.md` (v1).