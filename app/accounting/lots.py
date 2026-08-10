"""Pure FIFO lot matching (DESIGN.md §7/§11). No SQL, no side effects —
`repo.py` feeds it rows and writes the results.

A lot with qty_opened > 0 is long; qty_opened < 0 is short (sell-to-open).
qty_remaining is always the absolute number of shares still open. A sell
consumes long lots FIFO and opens a short lot with any excess; a buy covers
short lots FIFO first and opens a long lot with the remainder.
"""
from dataclasses import dataclass

from .money import mul_micro, pro_rata


@dataclass
class OpenLot:
    id: str
    qty_remaining: int          # micro-shares, absolute
    cost_per_share_base: int    # ex-commission; for shorts: the open (sale) price
    commission_allocated: int   # open-side commission still attached to this lot
    is_short: bool
    opened_at: int


@dataclass
class Closure:
    lot_id: str
    qty: int
    proceeds_base: int
    cost_base: int
    commission_base: int        # open-side allocation + this fill's pro-rata share
    realized_pl_base: int
    open_commission_consumed: int  # how much to deduct from the lot's allocation


def match(open_lots: list[OpenLot], qty: int, price_base: int,
          fill_commission: int, closing_short: bool) -> tuple[list[Closure], int]:
    """Close up to `qty` micro-shares against `open_lots` (already filtered to
    the correct side and FIFO-ordered). Returns (closures, qty_unmatched).

    `closing_short=False`: a sell closing long lots — P&L = proceeds − cost.
    `closing_short=True`:  a buy covering short lots — P&L = open sale − cover cost.
    The fill's commission is spread pro-rata over the shares it closes; any
    shares left over (which will open a fresh lot) carry the remainder.
    """
    closures: list[Closure] = []
    remaining = qty
    for lot in open_lots:
        if remaining <= 0:
            break
        take = min(remaining, lot.qty_remaining)
        open_leg = mul_micro(take, lot.cost_per_share_base)
        close_leg = mul_micro(take, price_base)
        open_comm = pro_rata(lot.commission_allocated, take, lot.qty_remaining)
        close_comm = pro_rata(fill_commission, take, qty)
        commission = open_comm + close_comm
        if closing_short:
            proceeds, cost = open_leg, close_leg   # sold high first, bought back now
        else:
            proceeds, cost = close_leg, open_leg
        closures.append(Closure(
            lot_id=lot.id, qty=take,
            proceeds_base=proceeds, cost_base=cost,
            commission_base=commission,
            realized_pl_base=proceeds - cost - commission,
            open_commission_consumed=open_comm,
        ))
        remaining -= take
    return closures, remaining
