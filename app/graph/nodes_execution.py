"""Deterministic execution nodes (DESIGN.md §11) — no LLM anywhere in this
file. An approved typed thesis contains every number needed; execution is a
function, not a conversation.
"""
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from .. import sessions
from ..accounting.money import to_micro
from ..tools.market import yahoo_symbol


def position_qty(size_base: Decimal, price_base: Decimal, lot_size: int) -> Decimal:
    """Shares purchasable for `size_base`, floored to the venue lot size."""
    if price_base <= 0:
        raise ValueError("price must be positive")
    shares = int(size_base / price_base)
    return Decimal(shares - shares % max(lot_size, 1))


def build_execute(repo, brokers: dict, market, *, account_for,
                  clock=time.time, cal=None):
    """`brokers`: name -> adapter. `account_for(broker_name)` -> account_id."""

    def execute(state):
        ap, th, inst = state.approval, state.thesis, state.instrument
        ts = int(clock())
        now = datetime.fromtimestamp(ts, tz=timezone.utc)
        account_id = account_for(ap.broker)
        symbol = yahoo_symbol(inst.ticker, inst.exchange)
        oid = f"ord_{uuid.uuid4().hex[:12]}"

        if not sessions.is_open(inst.exchange, now, cal):       # §14a
            qty = _intended_qty(ap, th, inst, market, symbol, account_id, repo)
            repo.create_order(
                id=oid, account_id=account_id, instrument_id=inst.id,
                side=th.direction, qty=to_micro(qty),
                limit_price=to_micro(str(th.entry_high)) if th.entry_high else None,
                stop_loss=to_micro(str(th.stop_loss)) if th.stop_loss else None,
                take_profit=to_micro(str(th.take_profit)) if th.take_profit else None,
                status="pending_session", work_item_id=state.work_item_id,
                expires_at=int(sessions.close_of(inst.exchange, now,
                                                 cal).timestamp()), ts=ts)
            return {"order_ids": [oid]}             # custodian places at next open

        qty = _intended_qty(ap, th, inst, market, symbol, account_id, repo)
        if qty <= 0:
            raise ValueError("size too small for one lot at current price")
        placed = brokers[ap.broker].place_bracket(
            symbol=symbol, side=th.direction, qty=qty,
            limit=th.entry_high, stop_loss=th.stop_loss,
            take_profit=th.take_profit)
        if placed.status != "accepted":
            raise RuntimeError(f"broker rejected order: {placed.detail}")
        repo.create_order(
            id=oid, account_id=account_id, instrument_id=inst.id,
            side=th.direction, qty=to_micro(qty),
            limit_price=to_micro(str(th.entry_high)) if th.entry_high else None,
            stop_loss=to_micro(str(th.stop_loss)) if th.stop_loss else None,
            take_profit=to_micro(str(th.take_profit)) if th.take_profit else None,
            status="placed", work_item_id=state.work_item_id,
            broker_order_id=placed.broker_order_id,
            expires_at=int(sessions.close_of(inst.exchange, now,
                                             cal).timestamp()), ts=ts)
        return {"order_ids": [oid]}                 # fills arrive ASYNC (§12)

    def _intended_qty(ap, th, inst, market, symbol, account_id, repo_):
        if th.direction == "sell":
            if ap.qty:                              # exits are sized in shares
                return Decimal(str(ap.qty))
            held, _, _ = repo_.position(account_id, inst.id)
            return Decimal(held) / 1_000_000        # close the whole position
        acct = repo_.account(account_id)
        q = market.quote(symbol)
        price_native = q["price"]
        rate = market.fx(q["currency"], acct["base_currency"])
        price_base = Decimal(price_native) * rate
        return position_qty(Decimal(str(ap.size_base)), price_base,
                            inst.lot_size)

    return execute


def build_record(repo, narratives, *, clock=time.time):
    """Terminal bookkeeping: one factual markdown summary per run. State
    transitions live in Desk._sync; money lives in repo — this node only
    writes prose ABOUT the record, never the record."""

    def record(state):
        lines = [f"# {state.ticker} — {state.kind} {state.work_item_id}", ""]
        for r in state.reports:
            lines.append(f"- **{r.agent}**: {r.signal} "
                         f"(conviction {r.conviction:.2f}) — {r.summary}")
        if state.thesis:
            t = state.thesis
            lines.append(f"\n**Arbiter**: {t.direction} "
                         f"(conviction {t.conviction:.2f}) "
                         f"entry {t.entry_low}–{t.entry_high} "
                         f"stop {t.stop_loss} target {t.take_profit}")
        if state.approval:
            lines.append(f"**Decision**: {state.approval.status} "
                         f"by {state.approval.actor}")
        if state.order_ids:
            lines.append(f"**Orders**: {', '.join(state.order_ids)}")
        if state.errors:
            lines.append("\n## Errors\n" + "\n".join(f"- {e}" for e in state.errors))
        narratives.write(state.work_item_id, "summary", "\n".join(lines))
        return {}

    return record
