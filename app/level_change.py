"""Move the stop-loss / take-profit of a position ALREADY HELD (the Today
bar on Home, the Orders page).

A held position's protection is its resting orders (protection.py: one
`orders` row per leg, kept at the venue or in the built-in sim). Changing
"the stop-loss" therefore means moving every resting stop order of that
position to the new price — a position bought in two parts has two.

Rules, in order:

1. Only a human, through the paired dashboard (the route checks that).
2. Only where the broker's row in capabilities.CAN_CHANGE allows that part,
   and only if its adapter can do it (`change_leg`). A trailing stop held
   by the broker is never moved here.
3. The new level must make sense for the position: for a long, the
   stop-loss below the price now and below the take-profit; the take-profit
   above the price now. (A stop on the wrong side of the market would sell
   at once.) Mirrored for a short.
4. The venue first, then the book: each order is changed at the venue and
   only then written to its row, with the price the venue reports. If one
   of several refuses, the ones already moved stay moved and are reported —
   never rolled back behind the user's back.

Runs inside claire-api, which owns the broker adapters. Nothing here places
or cancels an order, and nothing changes the plan the trade was approved
with (work_items.thesis_json): that stays as the record of what was agreed.
"""
import json
import logging
from decimal import Decimal

from . import capabilities, protection
from .accounting.money import from_micro, to_micro

WORDS = {"stop": "stop-loss", "target": "take-profit"}
PART = {"stop": "stop_loss", "target": "take_profit"}


log = logging.getLogger(__name__)


def _work_item(conn, leg):
    """The trade a leg belongs to: its own, else its entry order's."""
    if leg["work_item_id"]:
        return leg["work_item_id"]
    if leg["parent_order_id"]:
        r = conn.execute("SELECT work_item_id FROM orders WHERE id=?",
                         (leg["parent_order_id"],)).fetchone()
        return r["work_item_id"] if r else None
    return None


class LevelChangeError(Exception):
    def __init__(self, status, msg):
        self.status = status
        super().__init__(msg)


def _price(leg) -> Decimal:
    return from_micro(leg["stop_loss"] if leg["leg"] == "stop"
                      else leg["limit_price"])


def _plain(d) -> str:
    """99.50 -> '99.5', 100.0 -> '100' (never 1E+2)."""
    return format(Decimal(d).normalize(), "f")


def _dec(v, what) -> Decimal:
    try:
        d = Decimal(str(v))
    except Exception:                           # noqa: BLE001
        raise LevelChangeError(422, f"the {what} must be a number") from None
    if not d.is_finite() or d <= 0:
        raise LevelChangeError(422, f"the {what} must be more than zero")
    return d


def check(kind, new, *, long, price=None, other=None):
    """Refuse a level on the wrong side of the market or of its partner.
    `other` is the opposite level as it will be after the change."""
    word = WORDS[kind]
    below = (kind == "stop") == long            # this level sits BELOW price
    if price is not None:
        p = Decimal(str(price))
        if below and new >= p:
            raise LevelChangeError(422, f"the {word} must be below the price "
                                        f"now ({p}) — at {new} it would "
                                        "trade at once")
        if not below and new <= p:
            raise LevelChangeError(422, f"the {word} must be above the price "
                                        f"now ({p}) — at {new} it would "
                                        "trade at once")
    if other is not None:
        if below and new >= other:
            raise LevelChangeError(422, f"the {word} must be below the "
                                        f"{WORDS['target' if kind == 'stop' else 'stop']}"
                                        f" ({other})")
        if not below and new <= other:
            raise LevelChangeError(422, f"the {word} must be above the "
                                        f"{WORDS['target' if kind == 'stop' else 'stop']}"
                                        f" ({other})")


def change(repo, brokers, account_id, instrument_id, *, stop=None,
           target=None, price=None, ts, env=None, actor="human"):
    """Move the position's resting stop-loss and/or take-profit orders.
    `price`: the price now, when the caller has one (it bounds the levels).
    Returns {ok, changed: [{leg, order, from, to, verified}], failed: [...]}."""
    conn = repo.conn
    if stop is None and target is None:
        raise LevelChangeError(422, "give a new stop-loss or take-profit")
    acct = conn.execute("SELECT * FROM broker_accounts WHERE id=?",
                        (account_id,)).fetchone()
    inst = conn.execute("SELECT * FROM instruments WHERE id=?",
                        (instrument_id,)).fetchone()
    if acct is None or inst is None:
        raise LevelChangeError(404, "the desk holds no such position")
    held = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN qty_opened<0 THEN -qty_remaining"
        " ELSE qty_remaining END),0) FROM lots WHERE account_id=? AND"
        " instrument_id=? AND qty_remaining>0",
        (account_id, instrument_id)).fetchone()[0]
    if not held:
        raise LevelChangeError(404, "the desk holds no such position")
    long = held > 0
    legs = [l for l in protection.working_legs(conn, account_id,
                                               instrument_id)
            if l["side"] == ("sell" if long else "buy")]
    by_kind = {k: [l for l in legs if l["leg"] == k]
               for k in ("stop", "target")}
    want = {k: _dec(v, WORDS[k]) for k, v in (("stop", stop),
                                              ("target", target))
            if v is not None}
    for k in want:
        if not by_kind[k]:
            raise LevelChangeError(409, f"this position has no {WORDS[k]} "
                                        "order resting to change")
    adapter = (brokers or {}).get(acct["broker"] or "")
    sim_on = ((env or {}).get("SAXO_SIM_EXECUTION") or "").lower() == "enabled"
    plan = []
    for k, new in want.items():
        for leg in by_kind[k]:
            venue = capabilities.venue_of(
                acct["broker"], acct["environment"], inst["exchange"],
                sim_on=sim_on, order_id=leg["broker_order_id"])
            if not capabilities.can_change(venue).get(PART[k]):
                raise LevelChangeError(
                    409, f"{capabilities.LABEL.get(venue, venue)} does not "
                         f"let the desk change a {WORDS[k]} once the trade "
                         "is done (capabilities.CAN_CHANGE)")
            if adapter is None or not hasattr(adapter, "change_leg") or \
                    not leg["broker_order_id"]:
                raise LevelChangeError(
                    409, f"no {acct['broker'] or 'broker'} connection that "
                         "can change this order")
            plan.append((k, leg, new))
    # the levels as they will be: each checked against the price and its
    # partner (the other one's NEW value when both are being moved)
    current = {k: (min if (k == "stop") == long else max)(
                   _price(l) for l in ls) if ls else None
               for k, ls in by_kind.items()}
    for k, new in want.items():
        partner = "target" if k == "stop" else "stop"
        check(k, new, long=long, price=price,
              other=want.get(partner, current[partner]))

    from .tools.market import yahoo_symbol
    symbol = yahoo_symbol(inst["ticker"], inst["exchange"])
    changed, failed = [], []
    for k, leg, new in plan:
        before = _price(leg)
        if before == new:
            continue
        try:
            out = adapter.change_leg(leg["broker_order_id"], new,
                                     symbol=symbol)
        except Exception as e:                  # noqa: BLE001 — reported
            failed.append({"leg": k, "order": leg["id"],
                           "error": str(e)[:250]})
            continue
        got = _dec(out.get("price") or new, WORDS[k])
        col = "stop_loss" if k == "stop" else "limit_price"
        conn.execute(f"UPDATE orders SET {col}=?, updated_at=? WHERE id=?",
                     (to_micro(str(got)), ts, leg["id"]))
        changed.append({"leg": k, "order": leg["id"],
                        "broker_order_id": leg["broker_order_id"],
                        "from": _plain(before), "to": _plain(got),
                        "verified": bool(out.get("verified"))})
    if changed or failed:
        # the audit trail, on the trade that opened the position: who moved
        # what, from where to where (events hang off a work item)
        wi = next((w for w in (_work_item(conn, l) for _, l, _ in plan)
                   if w), None)
        payload = {"account_id": account_id, "instrument_id": instrument_id,
                   "changed": changed, "failed": failed}
        if wi:
            conn.execute(
                "INSERT INTO events (item_id, ts, actor, from_state,"
                " to_state, payload) VALUES (?,?,?,?,?,?)",
                (wi, ts, actor, "protected", "levels_changed",
                 json.dumps(payload)))
        log.info("levels changed by %s: %s", actor, json.dumps(payload))
    if failed and not changed:
        raise LevelChangeError(502, "the broker refused the change: "
                               + failed[0]["error"])
    return {"ok": not failed, "changed": changed, "failed": failed}
