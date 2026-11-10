"""Cancel / edit an approved order before it fills (app/order_control.py).

DDOG, 2026-11-05: an approved buy sat "held until the market opens" and
there was no way to cancel or change it short of the Saxo platform."""
import pytest

from app import order_control as oc
from tests.test_protective_legs import T0, World

CLOSED = T0 + 12 * 3600                 # 02:00 UTC — NASDAQ shut


def queued(w, wi="wi_q"):
    """An approved CSCO buy queued while the market is closed."""
    w.t["now"] = CLOSED
    out = w.execute(w.state(wi, "buy", size=1000.0, stop=97.0, target=115.0))
    o = w.repo.order(out["order_ids"][0])
    assert o["status"] == "pending_session" and not w.venue.calls
    return o


def wi_state(w, wi):
    return w.conn.execute("SELECT state FROM work_items WHERE id=?",
                          (wi,)).fetchone()[0]


def test_cancel_a_queued_order_sends_nothing_and_never_places():
    w = World()
    o = queued(w)
    out = oc.cancel(w.repo, {"saxo": w.venue}, "wi_q", ts=CLOSED)
    assert out["where"] == "desk"
    assert w.repo.order(o["id"])["status"] == "cancelled"
    assert wi_state(w, "wi_q") == "cancelled"
    assert w.repo.reserved_cash(w.acct) == 0        # cash released
    w.t["now"] = T0 + 86400                         # next session opens
    w.custodian.run_once()
    assert not [c for c in w.venue.calls if c[0] == "place"]


def test_edit_a_queued_order_in_place_and_it_is_sent_with_the_new_levels():
    w = World()
    o = queued(w)
    before = w.repo.reserved_cash(w.acct)
    oc.edit(w.repo, None, {"saxo": w.venue}, "wi_q", entry=100.0, stop=95.0,
            target=110.0, qty=5, ts=CLOSED)
    n = w.repo.order(o["id"])
    assert (n["limit_price"], n["stop_loss"], n["take_profit"], n["qty"]) == \
        (100_000_000, 95_000_000, 110_000_000, 5_000_000)
    assert 0 < w.repo.reserved_cash(w.acct) < before   # 5 sh, not 9
    w.t["now"] = T0 + 86400
    w.custodian.run_once()
    place = [c for c in w.venue.calls if c[0] == "place"]
    assert place and place[0][2] == 5 and float(place[0][3]) == 95.0 and \
        float(place[0][4]) == 110.0
    ev = [r[0] for r in w.conn.execute(
        "SELECT to_state FROM events WHERE item_id='wi_q'")]
    assert "order_edited" in ev


def test_edit_refuses_incoherent_and_too_far_levels():
    w = World()
    queued(w)
    with pytest.raises(Exception):                  # buy stop above entry
        oc.edit(w.repo, None, {}, "wi_q", entry=100.0, stop=105.0, ts=CLOSED)
    with pytest.raises(oc.OrderControlError, match="25%"):
        oc.edit(w.repo, None, {}, "wi_q", entry=100.0, stop=95.0,
                target=130.0, ts=CLOSED)


def test_a_claimed_order_cannot_be_cancelled_or_edited_from_the_desk():
    """The placer took it (submitted_at set): the desk no longer owns it."""
    w = World()
    o = queued(w)
    w.conn.execute("UPDATE orders SET submitted_at=? WHERE id=?",
                   (CLOSED, o["id"]))
    with pytest.raises(oc.OrderControlError, match="being sent"):
        oc.cancel(w.repo, {}, "wi_q", ts=CLOSED)
    with pytest.raises(oc.OrderControlError, match="being sent"):
        oc.edit(w.repo, None, {}, "wi_q", entry=100.0, stop=95.0, ts=CLOSED)


def test_cancel_at_the_venue_cancels_the_entry_and_its_legs():
    w = World()
    oid = w.buy_with_legs(fill=False)
    bid = w.repo.order(oid)["broker_order_id"]
    oc.cancel(w.repo, {"saxo": w.venue}, "wi_buy", ts=T0)
    assert ("cancel", bid) in w.venue.calls
    assert w.repo.order(oid)["status"] == "cancelled"
    assert all(l["status"] == "cancelled" for l in w.legs())
    assert wi_state(w, "wi_buy") == "cancelled"


def test_a_venue_refusal_leaves_the_book_alone():
    w = World()
    oid = w.buy_with_legs(fill=False)
    w.venue.refuse_cancel.add(w.repo.order(oid)["broker_order_id"])
    with pytest.raises(oc.OrderControlError) as e:
        oc.cancel(w.repo, {"saxo": w.venue}, "wi_buy", ts=T0)
    assert e.value.status == 502
    assert w.repo.order(oid)["status"] == "placed"
    assert wi_state(w, "wi_buy") == "executing"


def test_edit_at_the_venue_is_cancel_plus_a_new_approval():
    w = World()
    oid = w.buy_with_legs(fill=False)
    made = []

    class Desk:
        def manual_trade(self, *a, **kw):
            made.append((a, kw))
            return "wi_new"
    out = oc.edit(w.repo, Desk(), {"saxo": w.venue}, "wi_buy", entry=101.0,
                  stop=96.0, target=112.0, ts=T0)
    assert out["new_work_item_id"] == "wi_new"
    assert w.repo.order(oid)["status"] == "cancelled"
    (a, kw), = made
    assert a == ("CSCO", "NASDAQ", "buy", 101.0)
    assert kw["stop"] == 96.0 and kw["suggested"]["broker"] == "saxo"


def test_a_part_filled_order_is_refused():
    w = World()
    oid = w.buy_with_legs(fill=False)
    w.venue.fill(w.repo.order(oid)["broker_order_id"], 3, "103")
    w.custodian.run_once()
    assert w.repo.order(oid)["status"] == "partially_filled"
    with pytest.raises(oc.OrderControlError, match="filled"):
        oc.cancel(w.repo, {"saxo": w.venue}, "wi_buy", ts=T0)


def test_order_control_route_needs_the_dashboard_key(dash):
    import httpx
    base, w, key = dash
    r = httpx.post(base + "/api/order-control",
                   json={"work_item_id": "wi_buy", "action": "cancel"},
                   headers={"Origin": base})
    assert r.status_code == 403
    assert w.legs("placed")                         # nothing touched


from tests.test_protective_legs import dash  # noqa: E402,F401 — fixture


def test_orders_page_lists_queued_orders_legs_and_triggers(dash):
    import httpx
    base, w, key = dash
    d = httpx.get(base + "/api/orders", headers={"X-Dash-Key": key}).json()
    assert [l["leg"] for l in d["legs"]]                # CSCO stop + target
    assert all(l["status"] == "placed" for l in d["legs"])
    # every row says whether the venue really holds a stop/target for it
    assert all(isinstance(l["legs_at_broker"], bool) for l in d["legs"])
    assert d["entries"] == []                           # entry has filled
    assert "triggers" in d
    r = httpx.get(base + "/orders")
    assert r.status_code == 200 and b"Outstanding orders" in r.content


def test_edit_a_queued_buy_changes_its_trailing_alert_in_place():
    """The trailing alert is a watcher rule on the position, not part of the
    order: editing a queued buy changes it at once."""
    from app.watcher import position_floor
    w = World()
    o = queued(w)
    oc.edit(w.repo, None, {"saxo": w.venue}, "wi_q", entry=100.0, stop=95.0,
            target=110.0, trail_pct=6.5, ts=CLOSED)
    pct, _ = position_floor(w.conn, o["instrument_id"], o["account_id"])
    assert pct == 6.5
    # Claire: the queued order itself carries the % — it is what Saxo gets
    # as the trailing stop when the order is sent at the open
    assert w.repo.order(o["id"])["trail_pct"] == 6.5


def test_edit_at_the_venue_carries_the_trailing_alert_to_the_new_card():
    w = World()
    w.buy_with_legs(fill=False)
    made = []

    class Desk:
        def manual_trade(self, *a, **kw):
            made.append(kw)
            return "wi_new"
    oc.edit(w.repo, Desk(), {"saxo": w.venue}, "wi_buy", entry=101.0,
            stop=96.0, target=112.0, trail_pct=9.0, ts=T0)
    assert made[0]["suggested"]["trail_pct"] == 9.0
