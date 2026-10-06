"""Funding settlements credited to open paper positions — shared by both executors.

Bybit charges funding to positions held at the settlement time: the fee is the
position value at the settlement (quantity × mark price) times the rate; a long
pays a positive rate and a short receives it. ``credit_funding`` applies that to
every open position exactly once per settlement:

- a settlement ``τ`` is credited when ``opened_at < τ <= as_of`` and ``τ`` is
  later than the last settlement already credited, so repeated ticks after one
  event credit it once (F1);
- the notional is ``quantity × price at τ`` in the quote currency: the event's
  mark price when the exchange supplied one, otherwise ``price_at(τ)`` — the
  close of the bar that ends at ``τ`` (F4);
- the amount goes to ``PaperPosition.funding_abs`` and is returned, so the
  caller can journal it.

The backtester credits funding first thing on a tick, before any decision: a
position closed on the tick ``τ`` has then received the settlement ``τ``, and a
position opened on it has not.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from ..data.funding import FundingEvent, HistoricalFundingSource
from .paper_position import PaperPosition

PriceAt = Callable[[str, str, int], float]
"""Price of ``(symbol, timeframe)`` at a settlement time, for the notional."""


@dataclass(frozen=True, slots=True)
class FundingCredit:
    """One settlement credited to one position."""

    position: PaperPosition
    event: FundingEvent
    amount: float  # positive = received, negative = paid


def credit_funding(
    positions: Iterable[PaperPosition],
    funding_source: HistoricalFundingSource,
    as_of_ms: int,
    cost_model: object | None,
    price_at: PriceAt,
) -> list[FundingCredit]:
    """Credit open positions with every settlement up to ``as_of_ms`` not credited yet.

    Nothing is credited when ``cost_model`` carries no funding model — the
    legacy fee + slippage composite, or none at all — so a backtest run with
    funding disabled stays funding-free.
    """
    if cost_model is None or getattr(cost_model, "funding_model", None) is None:
        return []
    credits: list[FundingCredit] = []
    for position in positions:
        if not position.is_open:
            continue
        opened_ms = int(position.entry_time.timestamp() * 1000)
        credited_ms = max(opened_ms, position.funding_through_ms or opened_ms)
        # events_between is inclusive on both ends; +1 ms keeps credited_ms out
        for event in funding_source.events_between(credited_ms + 1, as_of_ms, position.symbol):
            price = (
                event.mark_price
                if event.mark_price is not None
                else price_at(position.symbol, position.timeframe, event.funding_time_ms)
            )
            amount = cost_model.accrue_funding(  # type: ignore[attr-defined]
                position.side, abs(position.size), price, [event],
            ).net_amount
            position.apply_funding(amount, event.funding_time_ms)
            credits.append(FundingCredit(position, event, amount))
    return credits
