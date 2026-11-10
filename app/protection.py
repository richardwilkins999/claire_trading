"""Protective legs: the stop-loss and take-profit orders a venue holds for a
desk position (DESIGN.md §11a). Deterministic; no LLM.

A Saxo bracket is three orders — the entry plus a stop and a target that
rest at Saxo on their own (one-cancels-other once the entry fills). The book
used to know only the entry, which broke two things:

- a stop that filled at Saxo sold shares the book never heard of;
- a desk sell (exit review, override, the Sell button) collided with the
  resting legs — Saxo refuses a new sell while sell orders already exist for
  owned shares (SellOrdersAlreadyExistForOwnedContracts, SIM 2026-11-04) —
  and, had it gone through, the legs would have sold the same shares again.

So every leg is an `orders` row (parent_order_id → its entry, leg =
'stop'|'target'), polled by the custodian like any order. Before any desk
order that reduces a protected position is sent, its legs are cancelled AT
THE VENUE; if that cannot be confirmed the desk order is refused. When the
desk order finishes and some of the position is left, the remainder is
re-protected at the original levels with a standalone OCO — and if that
fails, loudly (custodian flag + Telegram), never silently.

Real money: re-protection is never automatic on a live account. Placing
sell orders on Richard's real account is a human decision; the custodian
flags and alerts instead, and its flag disarms live trading (§22.3).
"""
import json
import uuid
from decimal import Decimal

from .accounting.money import from_micro, to_micro

# GTC legs never expire on the desk's clock; this is "no expiry" while
# keeping orders.expires_at NOT NULL (2100-01-01 UTC)
LEG_EXPIRES_AT = 4_102_444_800
WORKING = ("placed", "partially_filled")
TERMINAL = ("filled", "cancelled", "expired", "rejected")


class LegCancelRefused(RuntimeError):
    """A protective leg could not be confirmed cancelled at the venue. The
    desk order that needed it gone must not be placed: with the leg still
    working, the same shares could be sold twice."""


def leg_side(entry_side):
    return "sell" if entry_side == "buy" else "buy"


# ── persisting legs ──────────────────────────────────────────────────────
def persist_legs(repo, *, parent, legs, side, qty, ts, work_item_id=None,
                 account_id=None, instrument_id=None, fx_rate=None):
    """One orders row per leg the venue holds. `parent` is the entry row (or
    the entry id) the legs protect; `side` is the legs' side ('sell' for a
    long). Idempotent on the venue id: a leg already on the book is skipped.
    Returns the new row ids."""
    if isinstance(parent, str):
        parent = repo.order(parent)
    out = []
    for lg in legs or []:
        kind, boid = lg.get("leg"), lg.get("order_id")
        if kind not in ("stop", "target") or not boid:
            continue
        if repo.conn.execute("SELECT 1 FROM orders WHERE broker_order_id=?",
                             (str(boid),)).fetchone():
            continue
        price = lg.get("price")
        price_m = to_micro(str(price)) if price not in (None, "") else None
        if price_m is None:                     # venue said nothing: the
            price_m = parent["stop_loss"] if kind == "stop" \
                else parent["take_profit"]      # level the desk asked for
        oid = f"leg_{uuid.uuid4().hex[:12]}"
        repo.create_leg_order(
            id=oid, parent_order_id=parent["id"], leg=kind,
            account_id=account_id or parent["account_id"],
            instrument_id=instrument_id or parent["instrument_id"],
            side=side, qty=qty, broker_order_id=str(boid), ts=ts,
            expires_at=LEG_EXPIRES_AT,
            work_item_id=work_item_id if work_item_id is not None
            else parent["work_item_id"],
            limit_price=price_m if kind == "target" else None,
            stop_loss=price_m if kind == "stop" else None,
            fx_rate=fx_rate if fx_rate is not None else parent["fx_rate"])
        out.append(oid)
    return out


def working_legs(conn, account_id, instrument_id, side=None):
    """Legs the book believes are resting at the venue for this position."""
    q = ("SELECT * FROM orders WHERE account_id=? AND instrument_id=?"
         " AND leg IS NOT NULL AND status IN ('placed','partially_filled')")
    args = [account_id, instrument_id]
    if side:
        q += " AND side=?"
        args.append(side)
    return conn.execute(q + " ORDER BY created_at, rowid", args).fetchall()


def _filled(conn, order_id) -> int:
    (n,) = conn.execute("SELECT COALESCE(SUM(qty),0) FROM executions"
                        " WHERE order_id=?", (order_id,)).fetchone()
    return n


# ── cancel before a desk order ───────────────────────────────────────────
def cancel_for(repo, adapter, order_row, *, ts):
    """Cancel, at the venue, every working leg on the same side as the desk
    order about to be sent (a sell against a protected long; a buy-to-cover
    against a protected short), and mark them cancelled_for that order.

    A cancel that errors is checked against the venue before it is called a
    refusal: Saxo cancels an OCO sibling by itself, so the second DELETE of
    a pair may well fail on an order that is already gone. Raises
    LegCancelRefused if any leg is still working (or filled meanwhile — the
    position changed under the order, so it must be looked at again).
    Legs cancelled before the refusal stay cancelled and recorded; the order
    is marked protect_state='pending' so they are re-protected.
    Returns the leg rows it cancelled."""
    legs = working_legs(repo.conn, order_row["account_id"],
                        order_row["instrument_id"], side=order_row["side"])
    if not legs:
        return []
    done, problems = [], []
    for lg in legs:
        try:
            adapter.cancel(lg["broker_order_id"])
            ok = True
        except Exception as e:                  # noqa: BLE001 — verify below
            ok, err = False, e
        if not ok:
            state = None
            try:
                state = adapter.order_status(lg["broker_order_id"])["status"]
            except Exception:                   # noqa: BLE001
                state = None
            if state == "cancelled":
                ok = True
            elif state in ("filled", "partially_filled"):
                problems.append(f"{lg['leg']} {lg['broker_order_id']} has "
                                "(partly) filled at the venue — the position "
                                "changed; review it before selling")
                continue
            else:
                problems.append(f"{lg['leg']} {lg['broker_order_id']}: {err}")
                continue
        repo.conn.execute(
            "UPDATE orders SET status='cancelled', cancelled_for=?,"
            " updated_at=? WHERE id=? AND status IN ('placed',"
            "'partially_filled')", (order_row["id"], ts, lg["id"]))
        done.append(lg)
    if done:
        repo.conn.execute(
            "UPDATE orders SET protect_state='pending', protect_note=?,"
            " updated_at=? WHERE id=?",
            (f"cancelled {len(done)} protective leg(s) to place this order",
             ts, order_row["id"]))
    if problems:
        raise LegCancelRefused(
            "could not cancel the protective stop/target at the venue, so "
            "the order was NOT sent (the shares would be sold twice): "
            + "; ".join(problems)[:400])
    return done


# ── re-protect what is left ──────────────────────────────────────────────
def _levels(conn, order_id):
    """(stop, target, legs side, newest leg row) from the legs this desk
    order cancelled. With more than one protected entry the most recent
    one's levels are used — one OCO protects the whole remainder (Saxo
    refuses a second resting sell for the same shares)."""
    legs = conn.execute(
        "SELECT * FROM orders WHERE cancelled_for=? ORDER BY created_at,"
        " rowid", (order_id,)).fetchall()
    if not legs:
        return None
    by_parent = {}
    for lg in legs:
        by_parent.setdefault(lg["parent_order_id"], []).append(lg)
    # newest group = the latest thesis on this position
    parent_id = max(by_parent, key=lambda p: max(
        (x["created_at"] or 0) for x in by_parent[p]))
    group = by_parent[parent_id]
    stop = next((from_micro(x["stop_loss"]) for x in group
                 if x["leg"] == "stop" and x["stop_loss"]), None)
    target = next((from_micro(x["limit_price"]) for x in group
                   if x["leg"] == "target" and x["limit_price"]), None)
    return stop, target, group[0]["side"], group[0]


def reprotect(repo, adapter, order_id, *, symbol, ts):
    """Re-protect what a finished desk order left of a position whose legs
    it cancelled. Returns (state, note); state is also written on the order
    row: done | not_needed | failed. Never raises."""
    conn = repo.conn
    o = repo.order(order_id)

    def finish(state, note):
        conn.execute("UPDATE orders SET protect_state=?, protect_note=?,"
                     " updated_at=? WHERE id=?",
                     (state, note[:500], ts, order_id))
        return state, note

    try:
        lv = _levels(conn, order_id)
        if lv is None:
            return finish("not_needed", "no legs were cancelled")
        stop, target, side, parent = lv
        held, _, _ = repo.position(o["account_id"], o["instrument_id"])
        # the legs' side says which way the position runs: sell legs
        # protect a long, buy legs a short
        left = held if side == "sell" else -held
        shares = (Decimal(max(left, 0)) / 1_000_000).to_integral_value(
            rounding="ROUND_FLOOR")
        if shares <= 0:
            return finish("not_needed", "position closed — nothing left to "
                                        "protect")
        acct = repo.account(o["account_id"])
        what = (f"{shares} shares of {o['instrument_id']} on "
                f"{o['account_id']} (stop {stop}, target {target})")
        if acct["environment"] == "live":
            return finish("failed", f"UNPROTECTED {what}: the desk does not "
                          "place orders on the LIVE account by itself — "
                          "set the stop at Saxo, or sell the rest")
        if stop is None and target is None:
            return finish("failed", f"UNPROTECTED {what}: the cancelled "
                                    "legs carried no prices")
        # a mislabelled leg would put the 'stop' on the wrong side of the
        # market and fire at once: refuse levels that cannot both be right
        if stop is not None and target is not None and (
                (side == "sell" and not stop < target) or
                (side == "buy" and not stop > target)):
            return finish("failed", f"UNPROTECTED {what}: stop and target "
                                    "are the wrong way round — refusing to "
                                    "place them")
        if not hasattr(adapter, "place_oco"):
            return finish("failed", f"UNPROTECTED {what}: this venue cannot "
                                    "place a standalone stop")
        # the new legs protect the same entry the old ones did — and keep
        # trailing if that entry's stop was a trailing one. The stop row
        # carries the venue's CURRENT (already trailed) price, so the floor
        # it had reached is kept, not reset to the original stop.
        entry = repo.order(parent["parent_order_id"]) \
            if parent["parent_order_id"] else o
        from .graph.nodes_execution import _accepts, trail_distance
        oco = dict(symbol=symbol, side=side, qty=shares,
                   stop_loss=float(stop) if stop is not None else None,
                   take_profit=float(target) if target is not None else None,
                   client_order_id=f"{order_id}-rp")
        trail = trail_distance(entry) if entry is not None else None
        if trail and stop is not None and \
                _accepts(adapter, "trail_distance", "place_oco"):
            oco["trail_distance"] = float(trail)
        try:
            placed = adapter.place_oco(**oco)
        except Exception as e:                  # noqa: BLE001 — unknown
            return finish("failed", f"UNPROTECTED? {what}: re-protection "
                          f"outcome unknown ({e}) — check Saxo's working "
                          "orders")
        if placed.status != "accepted":
            return finish("failed", f"UNPROTECTED {what}: the venue refused "
                                    f"the new stop: {placed.detail}")
        new = persist_legs(repo, parent=entry, legs=placed.legs, side=side,
                           qty=to_micro(shares), ts=ts,
                           work_item_id=parent["work_item_id"],
                           account_id=o["account_id"],
                           instrument_id=o["instrument_id"])
        return finish("done", f"re-protected {what}: legs "
                      + ", ".join(x["order_id"] for x in placed.legs)
                      + (f" (book rows {', '.join(new)})" if new else ""))
    except Exception as e:                      # noqa: BLE001 — never silent
        return finish("failed", f"UNPROTECTED {o['instrument_id']} on "
                                f"{o['account_id']}: re-protection error {e}")


# ── keeping a leg row in step with the venue ─────────────────────────────
def sync_from_venue(repo, o, status, *, ts):
    """After the custodian has booked a leg's fills: reflect what the venue
    says the leg now is. A sibling cancelled by OCO, or a leg cancelled
    with its entry, becomes `cancelled`; a leg the venue reports done
    becomes `filled` even if it covered fewer shares than the row asked for
    (a partly filled entry only arms its legs for what filled). Type and
    price are corrected from the venue (it is presumed right)."""
    from .tools.saxo_broker import leg_kind
    sets, args = [], []
    kind = leg_kind(status.get("order_type"))
    if kind and kind != o["leg"]:
        sets.append("leg=?")
        args.append(kind)
    price = status.get("price")
    if price not in (None, "", 0):
        pm = to_micro(str(price))
        k = kind or o["leg"]
        if k == "stop" and pm != o["stop_loss"]:
            sets += ["stop_loss=?", "limit_price=NULL"]
            args.append(pm)
        elif k == "target" and pm != o["limit_price"]:
            sets += ["limit_price=?", "stop_loss=NULL"]
            args.append(pm)
    amount = status.get("amount")
    if amount not in (None, "", 0):
        am = to_micro(str(amount))
        if am != o["qty"] and am >= _filled(repo.conn, o["id"]) and am > 0:
            sets.append("qty=?")
            args.append(am)
    if sets:
        repo.conn.execute(f"UPDATE orders SET {', '.join(sets)},"
                          " updated_at=? WHERE id=?", (*args, ts, o["id"]))
    cur = repo.order(o["id"])["status"]
    venue = status.get("status")
    if venue == "filled" and cur in WORKING and _filled(repo.conn, o["id"]):
        repo.mark_order(o["id"], "filled", ts=ts)
    elif venue in ("cancelled", "rejected") and cur in WORKING:
        repo.mark_order(o["id"], "cancelled", ts=ts)


# ── one-time backfill of legs placed before they were tracked ────────────
def _close(a, b, tol=Decimal("0.005")):
    if a is None or b is None:
        return False
    a, b = Decimal(str(a)), Decimal(str(b))
    return b != 0 and abs(a / b - 1) <= tol


def backfill(repo, adapter, account_id, *, ts, dry_run=False):
    """Find the protective legs resting at the venue for this account's
    filled bracket entries and put them on the book. Idempotent: an entry
    that already has legs, or a venue order already on the book, is skipped.

    A leg is adopted only if it matches on every count — named by the entry
    in Saxo's audit trail (RelatedOrders), working, same instrument, exit
    side, amount = what the entry filled, and a price within 0.5% of the
    desk's level for that leg type. Anything less is reported, never
    guessed. Returns a report list (one dict per decision)."""
    from .tools.market import yahoo_symbol
    conn = repo.conn
    report = []
    venue = {w["order_id"]: w for w in adapter.working_orders()}
    for o in conn.execute(
            "SELECT o.*, i.ticker, i.exchange FROM orders o JOIN instruments"
            " i ON i.id=o.instrument_id WHERE o.account_id=? AND o.leg IS NULL"
            " AND o.status IN ('filled','partially_filled')"
            " AND o.broker_order_id IS NOT NULL"
            " AND (o.stop_loss IS NOT NULL OR o.take_profit IS NOT NULL)"
            " ORDER BY o.created_at", (account_id,)).fetchall():
        entry = {"order": o["id"], "venue_entry": o["broker_order_id"],
                 "instrument": o["instrument_id"]}
        if conn.execute("SELECT 1 FROM orders WHERE parent_order_id=?",
                        (o["id"],)).fetchone():
            report.append({**entry, "action": "skip",
                           "why": "legs already on the book"})
            continue
        held, _, _ = repo.position(account_id, o["instrument_id"])
        if (held > 0) != (o["side"] == "buy") or held == 0:
            report.append({**entry, "action": "skip",
                           "why": "the desk no longer holds this position"})
            continue
        filled = _filled(conn, o["id"])
        side = leg_side(o["side"])
        sym = yahoo_symbol(o["ticker"], o["exchange"])
        try:
            related = adapter.related_order_ids(o["broker_order_id"])
        except Exception as e:                  # noqa: BLE001
            report.append({**entry, "action": "skip",
                           "why": f"could not read the venue audit: {e}"})
            continue
        legs = []
        for rid in related:
            w = venue.get(str(rid))
            why = None
            from .tools.saxo_broker import leg_kind
            kind = leg_kind(w and w.get("type"))
            if w is None:
                why = "not working at the venue (filled or cancelled?)"
            elif w.get("desk_symbol") != sym:
                why = f"instrument {w.get('symbol')} is not {sym}"
            elif w.get("side") not in (None, side):
                why = f"side {w.get('side')} is not {side}"
            elif kind is None:
                why = f"order type {w.get('type')} is not a stop or target"
            elif to_micro(str(w.get("amount") or 0)) != filled:
                why = (f"amount {w.get('amount')} is not the "
                       f"{from_micro(filled)} shares the entry filled")
            else:
                level = o["stop_loss"] if kind == "stop" else o["take_profit"]
                if level is None or not _close(w.get("price"),
                                               from_micro(level)):
                    why = (f"{kind} price {w.get('price')} is not the desk's "
                           f"{from_micro(level) if level else None}")
            if why:
                report.append({**entry, "venue_order": str(rid),
                               "action": "skip", "why": why})
                continue
            legs.append({"leg": kind, "order_id": str(rid),
                         "price": w.get("price"), "verified": True})
        if not legs:
            if related:
                continue
            report.append({**entry, "action": "skip",
                           "why": "the venue lists no related orders"})
            continue
        if len({x["leg"] for x in legs}) != len(legs):
            report.append({**entry, "action": "skip",
                           "why": "two venue orders claim the same leg — "
                                  "refusing to guess"})
            continue
        ids = [] if dry_run else persist_legs(
            repo, parent=o, legs=legs, side=side, qty=filled, ts=ts)
        for lg, rid in zip(legs, ids or [None] * len(legs)):
            report.append({**entry, "venue_order": lg["order_id"],
                           "leg": lg["leg"], "price": lg["price"],
                           "qty": float(from_micro(filled)),
                           "action": "would insert" if dry_run else "inserted",
                           "row": rid})
    return report


def to_json(report):
    return json.dumps(report, indent=2, default=str)
