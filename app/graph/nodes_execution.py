"""Deterministic execution nodes (DESIGN.md §11) — no LLM anywhere in this
file. An approved typed thesis contains every number needed; execution is a
function, not a conversation.
"""
import json
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from .. import sessions
from ..accounting.money import from_micro, mul_micro, to_micro
from ..accounting.repo import InsufficientCash
from ..tools.market import yahoo_symbol


FRACTION_STEP = Decimal("0.1")   # venues that allow fractions: 1/10th share


def position_qty(size_base: Decimal, price_base: Decimal, lot_size: int,
                 step: Decimal = FRACTION_STEP) -> Decimal:
    """Shares purchasable for `size_base`. Board-lot venues (SGX etc.) floor
    to whole lots; elsewhere fractional trading is allowed down to `step`, so
    a small budget still buys something instead of rounding to zero."""
    if price_base <= 0:
        raise ValueError("price must be positive")
    raw = size_base / price_base
    if lot_size > 1:
        lots = int(raw / lot_size)
        return Decimal(lots * lot_size)
    return (raw / step).to_integral_value(rounding="ROUND_FLOOR") * step


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
            qty, price_base = _intended_qty(ap, th, inst, market, symbol,
                                            account_id, repo, state.kind)
            _pre_checks(repo, account_id, inst, th, qty, price_base, ts)
            repo.create_order(  # queued for the open; checks re-run at fill
                id=oid, account_id=account_id, instrument_id=inst.id,
                side=th.direction, qty=to_micro(qty),
                limit_price=to_micro(str(th.entry_high)) if th.entry_high else None,
                stop_loss=to_micro(str(th.stop_loss)) if th.stop_loss else None,
                take_profit=to_micro(str(th.take_profit)) if th.take_profit else None,
                status="pending_session", work_item_id=state.work_item_id,
                expires_at=int(sessions.close_of(inst.exchange, now,
                                                 cal).timestamp()), ts=ts)
            _set_trailing_floor(repo, inst, ap)
            return {"order_ids": [oid]}             # custodian places at next open

        qty, price_base = _intended_qty(ap, th, inst, market, symbol,
                                        account_id, repo, state.kind)
        _pre_checks(repo, account_id, inst, th, qty, price_base, ts)
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
        _set_trailing_floor(repo, inst, ap)
        return {"order_ids": [oid]}                 # fills arrive ASYNC (§12)

    def _intended_qty(ap, th, inst, market, symbol, account_id, repo_, kind):
        if th.direction == "sell":
            held, _, _ = repo_.position(account_id, inst.id)
            held_shares = Decimal(held) / 1_000_000
            qty = Decimal(str(ap.qty)) if ap.qty else held_shares
            if kind == "sell_review":
                # an exit review closes a position — it never flips short
                qty = min(qty, held_shares)
            return qty, None
        acct = repo_.account(account_id)
        q = market.quote(symbol)
        rate = market.fx(q["currency"], acct["base_currency"])
        price_base = Decimal(q["price"]) * rate
        return position_qty(Decimal(str(ap.size_base)), price_base,
                            inst.lot_size), price_base

    return execute


def _set_trailing_floor(repo, inst, ap):
    """The trailing floor you chose at approval becomes the watcher's rule
    for this instrument, replacing the default 8%."""
    if not ap.trail_pct or ap.status != "approved":
        return
    repo.conn.execute(
        "INSERT INTO price_alerts (instrument_id, rule, threshold, armed)"
        " VALUES (?, 'drop_pct_from_entry', ?, 1)"
        " ON CONFLICT DO NOTHING", (inst.id, float(ap.trail_pct)))
    repo.conn.execute(
        "UPDATE price_alerts SET threshold=?, armed=1 WHERE instrument_id=?"
        " AND rule='drop_pct_from_entry'", (float(ap.trail_pct), inst.id))


def _pre_checks(repo, account_id, inst, th, qty, price_base, ts):
    """Everything that must hold BEFORE the venue sees the order (§11):
    a real quantity, cash to cover cost + estimated fee, and risk caps."""
    if qty is None or qty <= 0:
        raise ValueError(
            "order quantity is zero — size too small for one lot, or a sell "
            "with no position and no explicit qty")
    limit = to_micro(str(th.entry_high)) if th.entry_high else None
    repo.check_risk(account_id, inst.id, th.direction, to_micro(qty),
                    limit, ts)
    if th.direction == "buy" and price_base is not None:
        cost = mul_micro(to_micro(qty), to_micro(str(price_base)))
        acct = repo.account(account_id)
        fee = repo._fee_for(json.loads(acct["fee_model"]), cost)
        balance = repo.cash_balance(account_id)
        if cost + fee > balance:
            raise InsufficientCash(
                f"order needs {from_micro(cost + fee):.2f} "
                f"{acct['base_currency']} (incl. ~{from_micro(fee):.2f} fee) "
                f"but {account_id} holds {from_micro(balance):.2f}")


def build_record(repo, narratives, *, clock=time.time, prose_fn=None):
    """Terminal bookkeeping: one factual markdown summary per run. State
    transitions live in Desk._sync; money lives in repo — this node only
    writes prose ABOUT the record, never the record. `prose_fn` (the recorder
    agent, §4) adds a short human note when a provider is available; its
    failure never fails a run."""

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
        if prose_fn is not None:
            try:
                lines.append("\n## Recorder note\n" +
                             prose_fn("\n".join(lines)))
            except Exception:                   # noqa: BLE001 — prose is a
                pass                            # nicety, never a dependency
        narratives.write(state.work_item_id, "summary", "\n".join(lines))
        return {}

    return record
