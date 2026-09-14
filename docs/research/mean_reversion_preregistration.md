# Mean Reversion Strategy — Pre-Registration v1 (INVALIDATED)

**Date:** 2026-08-28
**Status:** INVALIDATED — cost-gate failure, see v2
**Author:** Systematic review per `mean_reversion_plan.md` §3

---

## Why Invalidated

The frozen parameter grid below fails the cost/turnover sanity gate (Task 8) by >7×:

- **Pre-reg params**: hold=4h, rebalance=1h → 54 round-trips/day → 5913%/yr cost drag
- **Threshold for viability**: <100%/yr cost drag (break-even possible)

The pre-reg grid is economically infeasible. The statement "Rebalance cadence must be validated at 4h or holding period extended before full walk-through" contradicts "No other parameters are tuned." A frozen grid that fails cost-gate cannot be validated post-hoc — that is parameter search on test.

**Do not run walk-forward on this grid.** See `mean_reversion_preregistration_v2.md` for the corrected registration.

---

## Original Frozen Grid (for audit trail only)

| Parameter | Value |
|-----------|-------|
| `timeframe` | `1h` |
| `zscore_window_bars` | 48 |
| `signal_lookback` | `4h` |
| `entry_threshold` | 2.0 |
| `exit_threshold` | 0.5 |
| `max_holding_bars` | 24 |
| `weighting` | `inverse_vol` |
| `rebalance_hours` | 1 |
| `seed` | 42 |

---

## Decision Rule (Immutable — from original registration)

The strategy is **ACCEPTED** iff ALL hold on **test** segment of 3-way walk-forward split:

1. **Test Sharpe > 0** @ production DD=25% (NOT diagnostic 50%)
2. **Validation Sharpe > 0** @ production DD (independent window)
3. **n_trades(test) ≥ 200**
4. **Sign consistency**: Sharpe > 0 in ≥2 of 3 walk-forward sub-windows

If any condition fails → **REJECT** (no parameter re-tuning, no rule relaxation).

---

## Data Coverage Caveat

Funding rates only available through Feb 2024. Test window (Nov 2025+) has no funding data.
→ PnL reported as **pre-funding**; funding costs can only reduce PnL.

---

## Audit Trail

This document created **before** any implementation code (Task 1).
File hash (SHA256): *to be computed on freeze*
Git commit: *to be recorded on freeze*
**INVALIDATED** by cost-gate failure; superseded by v2.