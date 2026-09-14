#!/usr/bin/env python3
"""Mean Reversion Cost/Turnover Sanity Gate (Task 8).

Single methodology, single source of truth. No hardcoded defaults.
All parameters must be provided explicitly via CLI.

Methodology (documented, reproducible):
- Concurrent positions capped by risk engine: max_positions
- Each position: at most 1 round-trip per holding period, AND at most 1 round-trip per rebalance period
- Round-trips/day per position = min(24 / avg_holding_hours, 24 / rebalance_hours)
- Round-trips/day total = max_positions × min(24 / avg_holding_hours, 24 / rebalance_hours)
- Cost per round-trip = 2 × (fee_bps + slippage_bps)  [both sides]
- Daily cost drag (bps) = round_trips_per_day × cost_per_round_trip_bps
- Annual cost drag = daily_cost_drag_bps / 10000 × 365  (crypto trades 24/7/365)
- Break-even: gross return must exceed annual_cost_drag_pct

Usage:
    python scripts/estimate_mr_turnover.py --max-positions 3 --holding-hours 8 --rebalance-hours 24 --fee-bps 2 --slippage-bps 1
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass


@dataclass
class TurnoverEstimate:
    """Results of turnover estimation."""

    max_positions: int
    avg_holding_hours: float
    rebalance_hours: int
    timeframe: str

    round_trips_per_day: float
    fee_per_side_bps: float
    slippage_per_side_bps: float
    funding_bps_per_day: float

    daily_cost_drag_bps: float
    annual_cost_drag_pct: float

    # Break-even analysis
    required_gross_annual_return_pct: float  # Annual return needed to break even


def estimate_turnover(
    max_positions: int,
    avg_holding_hours: float,
    rebalance_hours: int,
    timeframe: str,
    fee_bps: float,
    slippage_bps: float,
    funding_bps_per_day: float = 0.0,
) -> TurnoverEstimate:
    """
    Estimate turnover and cost drag for Mean Reversion.

    Args:
        max_positions: Max concurrent positions (risk engine limit)
        avg_holding_hours: Average holding period in hours
        rebalance_hours: Rebalance cadence in hours (cap on frequency)
        timeframe: Timeframe (for reference)
        fee_bps: Fee per side in basis points
        slippage_bps: Slippage per side in basis points
        funding_bps_per_day: Funding cost per day in basis points

    Returns:
        TurnoverEstimate with all computed values
    """
    if max_positions <= 0:
        raise ValueError("max_positions must be positive")
    if avg_holding_hours <= 0:
        raise ValueError("avg_holding_hours must be positive")
    if rebalance_hours <= 0:
        raise ValueError("rebalance_hours must be positive")

    # Round-trips per day per position = min(1/holding_period, 1/rebalance_period) in days
    # = min(24/holding_hours, 24/rebalance_hours)
    rt_per_position_per_day = min(24.0 / avg_holding_hours, 24.0 / rebalance_hours)

    # Total round-trips per day across all positions
    round_trips_per_day = max_positions * rt_per_position_per_day

    # Cost per round-trip (both sides: entry + exit)
    cost_per_round_trip_bps = 2.0 * (fee_bps + slippage_bps)

    # Daily cost drag in basis points
    daily_cost_drag_bps = round_trips_per_day * cost_per_round_trip_bps

    # Add funding (bps/day)
    daily_cost_drag_bps += funding_bps_per_day

    # Annual cost drag (crypto trades 24/7/365)
    annual_cost_drag_pct = daily_cost_drag_bps / 10000.0 * 365.0

    # Required gross annual return to break even
    required_gross_annual_return_pct = annual_cost_drag_pct

    return TurnoverEstimate(
        max_positions=max_positions,
        avg_holding_hours=avg_holding_hours,
        rebalance_hours=rebalance_hours,
        timeframe=timeframe,
        round_trips_per_day=round_trips_per_day,
        fee_per_side_bps=fee_bps,
        slippage_per_side_bps=slippage_bps,
        funding_bps_per_day=funding_bps_per_day,
        daily_cost_drag_bps=daily_cost_drag_bps,
        annual_cost_drag_pct=annual_cost_drag_pct,
        required_gross_annual_return_pct=required_gross_annual_return_pct,
    )


def print_report(est: TurnoverEstimate) -> None:
    """Print a formatted report."""
    print("=" * 70)
    print("  MEAN REVERSION TURNOVER / COST ESTIMATE")
    print("=" * 70)
    print(f"  Max concurrent positions: {est.max_positions}")
    print(f"  Timeframe: {est.timeframe}")
    print(f"  Rebalance: every {est.rebalance_hours}h")
    print(f"  Avg holding: {est.avg_holding_hours:.1f} hours")
    print(f"  Max round-trips/position/day: {min(24.0/est.avg_holding_hours, 24.0/est.rebalance_hours):.4f}")
    print()
    print(f"  Cost assumptions:")
    print(f"    Fee: {est.fee_per_side_bps:.1f} bps/side")
    print(f"    Slippage: {est.slippage_per_side_bps:.1f} bps/side")
    print(f"    Funding: {est.funding_bps_per_day:.1f} bps/day")
    print()
    print(f"  Turnover:")
    print(f"    Round-trips/day: {est.round_trips_per_day:.2f}")
    print()
    print(f"  Cost drag:")
    print(f"    Daily: {est.daily_cost_drag_bps:.1f} bps")
    print(f"    Annual: {est.annual_cost_drag_pct:.2%}")
    print()
    print(f"  Break-even:")
    print(f"    Required gross annual return: {est.required_gross_annual_return_pct:.2%}")
    print()

    # Sanity check
    print("  SANITY CHECK:")
    if est.annual_cost_drag_pct > 1.0:
        print(f"  FAIL: Annual cost drag > 100% ({est.annual_cost_drag_pct:.1%})")
        print(f"       Strategy CANNOT be profitable after costs with these parameters.")
        print(f"       Fix: increase holding period, reduce max_positions, or reduce cadence.")
    elif est.annual_cost_drag_pct > 0.5:
        print(f"  FAIL: Annual cost drag > 50% ({est.annual_cost_drag_pct:.1%})")
        print(f"       Very high hurdle - needs exceptional gross edge.")
    elif est.annual_cost_drag_pct > 0.2:
        print(f"  WARN: Annual cost drag > 20% ({est.annual_cost_drag_pct:.1%})")
        print(f"       High but potentially viable with strong edge.")
    else:
        print(f"  PASS: Cost drag manageable ({est.annual_cost_drag_pct:.1%} annually)")

    print("=" * 70)


def main():
    parser = argparse.ArgumentParser(
        description="Estimate Mean Reversion turnover and cost drag (single methodology, no defaults)"
    )
    parser.add_argument("--max-positions", type=int, required=True, help="Max concurrent positions (risk limit)")
    parser.add_argument("--holding-hours", type=float, required=True, help="Avg holding period in hours")
    parser.add_argument("--rebalance-hours", type=int, required=True, help="Rebalance cadence in hours (cap on frequency)")
    parser.add_argument("--timeframe", type=str, default="1h", help="Timeframe (for reference)")
    parser.add_argument("--fee-bps", type=float, required=True, help="Fee per side (bps)")
    parser.add_argument("--slippage-bps", type=float, required=True, help="Slippage per side (bps)")
    parser.add_argument("--funding-bps", type=float, default=0.0, help="Funding cost per day (bps)")

    args = parser.parse_args()

    est = estimate_turnover(
        max_positions=args.max_positions,
        avg_holding_hours=args.holding_hours,
        rebalance_hours=args.rebalance_hours,
        timeframe=args.timeframe,
        fee_bps=args.fee_bps,
        slippage_bps=args.slippage_bps,
        funding_bps_per_day=args.funding_bps,
    )

    print_report(est)

    # Exit with error code if cost drag is too high
    if est.annual_cost_drag_pct > 1.0:
        return 1
    return 0


if __name__ == "__main__":
    exit(main())