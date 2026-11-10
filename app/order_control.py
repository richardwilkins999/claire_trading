"""Cancel or edit an APPROVED order before it fills (Trading page buttons).

Two cases, decided by where the order is:

- Held by the desk (`pending_session`, never sent): the order row is the
  whole truth, so cancel and edit are a conditional UPDATE guarded by
  `submitted_at IS NULL` — the same claim the placer uses (submit_order), so
  a cancel/edit racing the market-open placement either wins outright or is
  refused ("already being sent") and nothing is half-done.
- At the venue (`placed`): cancel is the venue's DELETE (Saxo cancels the
  attached stop/target with the entry), then the book. An edit at the venue
  is cancel + a NEW manual trade with the new levels, which you approve
  again — the desk never re-prices a live order behind the approval.

A partly filled order is refused: some shares are bought and protected by
their legs; cancelling the rest at the venue is a decision with more moving
parts than one button (sell what filled, or keep it).

Runs inside claire-api, which owns the broker adapters.
"""
import json

from .accounting.money import from_micro, to_micro as _to_micro
from .graph.state import Thesis

WORKING = ("pending_session", "placed", "partially_filled")
SAXO_MAX_PCT = 25          # TooFarFromEntryOrder, measured on SIM 2026-11-05


def to_micro(v):
    """Prices arrive as JSON floats; the ledger refuses floats, so go via
    their shortest decimal text (100.1 -> '100.1', not 100.0999...)."""
    return _to_micro(str(v) if isinstance(v, float) else v)


class OrderControlError(Exception):
    def __init__(self, status, msg):
        self.status = status
        super().__init__(msg)


def entry_order(conn, wi):
    """The item's working ENTRY order (protective legs excluded)."""
    rows = conn.execute(
        "SELECT o.*, i.ticker, i.exchange, a.broker FROM orders o"
        " JOIN instruments i ON i.id=o.instrument_id"
        " LEFT JOIN broker_accounts a ON a.id=o.account_id"
        " WHERE o.work_item_id=? AND o.leg IS NULL AND o.status IN"
        " ('pending_session','placed','partially_filled')"
        " ORDER BY o.created_at DESC", (wi,)).fetchall()
    if not rows:
        raise OrderControlError(409, "this trade has no working order")
    return rows[0]


def _close(repo, wi, ts, reason, **payload):
    row = repo.conn.execute("SELECT state FROM work_items WHERE id=?",
                            (wi,)).fetchone()
    if row and row["state"] == "executing":
        repo.set_state(wi, "cancelled", actor="human", ts=ts,
                       payload={"reason": reason, **payload})


def cancel(repo, brokers, wi, *, ts):
    conn = repo.conn
    o = entry_order(conn, wi)
    if o["status"] == "partially_filled":
        raise OrderControlError(409, "part of this order has filled — sell "
                                "the filled shares or cancel the rest at "
                                "Saxo; one button cannot decide that")
    if o["status"] == "pending_session":
        cur = conn.execute(
            "UPDATE orders SET status='cancelled', updated_at=? WHERE id=?"
            " AND status='pending_session' AND submitted_at IS NULL",
            (ts, o["id"]))
        if cur.rowcount != 1:
            raise OrderControlError(409, "the order is being sent to the "
                                    "broker right now — try again in a "
                                    "minute, then cancel it there")
        _close(repo, wi, ts, "you cancelled it before it was sent",
               order=o["id"])
        return {"ok": True, "where": "desk", "order": o["id"]}
    adapter = brokers.get(o["broker"] or "")
    if adapter is None or not o["broker_order_id"]:
        raise OrderControlError(409, f"no {o['broker'] or 'broker'} "
                                "connection to cancel it with")
    try:
        adapter.cancel(o["broker_order_id"])
    except Exception as e:                      # noqa: BLE001 — shown
        raise OrderControlError(502, f"the broker refused the cancel: "
                                f"{str(e)[:250]}") from None
    repo.mark_order(o["id"], "cancelled", ts=ts)
    # the bracket's legs die with the entry at the venue; mirror that
    conn.execute("UPDATE orders SET status='cancelled', updated_at=?"
                 " WHERE parent_order_id=? AND status IN"
                 " ('pending_session','placed')", (ts, o["id"]))
    _close(repo, wi, ts, "you cancelled it at the broker", order=o["id"],
           broker_order_id=o["broker_order_id"])
    return {"ok": True, "where": "broker", "order": o["id"]}


def _validated(o, entry, stop, target):
    side = o["side"]
    th = Thesis.model_validate({
        "ticker": o["ticker"], "direction": side, "conviction": 0.0,
        "entry_low": entry, "entry_high": entry, "stop_loss": stop,
        "take_profit": target, "currency": "USD", "conditions": [],
        "narrative_path": ""})
    if side == "buy" and not stop:
        raise OrderControlError(422, "a buy needs a stop below the entry")
    if (o["broker"] or "").startswith("saxo"):
        for name, v in (("stop", stop), ("target", target)):
            if v and abs(v - entry) / entry * 100 > SAXO_MAX_PCT:
                raise OrderControlError(
                    422, f"Saxo refuses a {name} more than {SAXO_MAX_PCT}% "
                    f"from the entry ({name} {v} vs entry {entry})")
    return th


def edit(repo, desk, brokers, wi, *, entry, stop=None, target=None,
         qty=None, trail_pct=None, ts):
    """New levels (and optionally share count, and a buy's trailing alert)
    for an approved order. The trailing alert is a watcher rule on the
    position, not part of the order: on an order Claire still holds it is
    changed at once; on one at the broker it rides on the new trade's card."""
    conn = repo.conn
    o = entry_order(conn, wi)
    if o["status"] == "partially_filled":
        raise OrderControlError(409, "part of this order has filled — it "
                                "cannot be edited")
    _validated(o, entry, stop, target)
    new_qty = to_micro(qty) if qty else o["qty"]
    if new_qty <= 0:
        raise OrderControlError(422, "shares must be more than zero")
    if o["status"] == "pending_session":
        old_lim = o["limit_price"] or 0
        reserved = o["reserved_base"]
        if reserved is not None and old_lim:
            # the reservation scales with qty x limit; the extra must be free
            new_res = int(reserved * (to_micro(entry) / old_lim)
                          * (new_qty / o["qty"]))
            extra = new_res - reserved
            if extra > 0 and extra > repo.available_cash(o["account_id"]):
                raise OrderControlError(409, "not enough free cash for the "
                                        "larger order")
        else:
            new_res = reserved
        # Claire: the order row's trail_pct is what Saxo receives as the
        # trailing stop when the custodian sends this order at the open
        new_trail = float(trail_pct) if trail_pct and o["side"] == "buy" \
            and stop else (o["trail_pct"] if stop else None)
        cur = conn.execute(
            "UPDATE orders SET limit_price=?, stop_loss=?, take_profit=?,"
            " qty=?, reserved_base=?, trail_pct=?, updated_at=? WHERE id=?"
            " AND status='pending_session' AND submitted_at IS NULL",
            (to_micro(entry), to_micro(stop) if stop else None,
             to_micro(target) if target else None, new_qty, new_res,
             new_trail, ts, o["id"]))
        if cur.rowcount != 1:
            raise OrderControlError(409, "the order is being sent to the "
                                    "broker right now — edit it there")
        th = json.loads(conn.execute("SELECT thesis_json FROM work_items"
                                     " WHERE id=?", (wi,)).fetchone()[0]
                        or "{}")
        th.update(entry_low=entry, entry_high=entry, stop_loss=stop,
                  take_profit=target)
        conn.execute("UPDATE work_items SET thesis_json=?, updated_at=?"
                     " WHERE id=?", (json.dumps(th), ts, wi))
        if trail_pct and o["side"] == "buy":
            from .watcher import set_position_floor
            set_position_floor(conn, o["instrument_id"], o["account_id"],
                               trail_pct, side=1)
        f = lambda m: float(from_micro(m)) if m else None   # noqa: E731
        repo._event(wi, ts, "human", "executing", "order_edited", payload={
            "order": o["id"],
            "from": {"entry": f(o["limit_price"]), "stop": f(o["stop_loss"]),
                     "target": f(o["take_profit"]), "qty": f(o["qty"])},
            "to": {"entry": entry, "stop": stop, "target": target,
                   "qty": f(new_qty), "trail_pct": trail_pct}})
        return {"ok": True, "where": "desk", "order": o["id"]}
    # at the venue: cancel, then a new trade with the new levels — approved
    # again by the human, never re-priced behind the approval
    cancel(repo, brokers, wi, ts=ts)
    new_wi = desk.manual_trade(
        o["ticker"], o["exchange"], o["side"], entry, stop=stop,
        target=target, note=f"edited from {wi}",
        suggested={"qty": float(from_micro(new_qty)), "broker": o["broker"],
                   **({"trail_pct": float(trail_pct)}
                      if trail_pct and o["side"] == "buy" else {})},
        source="edit")
    return {"ok": True, "where": "broker", "order": o["id"],
            "new_work_item_id": new_wi}
