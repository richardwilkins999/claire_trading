"""The 2026-11-13 code review: seven findings on the money path.

1  a filled sim entry was pruned after 30 days while its stop-loss /
   take-profit still rested on it — after a restart they could never fire
2  a take-profit was judged against the live price even when the limit
   rests away from it and can only fill at the limit
3  (tests/test_money_path_review_1111.py) a venue slow to show a TTL cancel
4  Hong Kong codes stored before the four-digit rule were never renamed
5  a replayed re-protection at the sim returned no legs, and its client id
   was never stored
9  swallowed(): a changing number in the throttle key defeated the throttle
"""
import logging

import pytest

from app import logsetup
from app.accounting import db
from app.accounting.money import to_micro
from app.accounting.repo import Repo
from app.tools.brokers import PaperSimBroker
from tests.test_execution import World, _levels_state
from tests.test_maintenance import DAY, NOW, world  # noqa: F401 — fixture

T0 = 1_793_000_000


# ── 1: the parent of a resting leg is never pruned ─────────────────────────
def test_a_filled_entry_is_kept_while_its_legs_rest(world):  # noqa: F811
    _, conn, m, _ = world
    sim = PaperSimBroker(protect=True, store=conn, venue="alpaca")
    p = sim.place_bracket(symbol="CSCO", side="buy", qty=9, limit=103,
                          stop_loss=97, take_profit=115)
    assert sim.order_status(p.broker_order_id)["status"] == "filled"
    conn.execute("UPDATE sim_orders SET updated_at=?", (NOW - 40 * DAY,))
    m.prune_sim_orders()
    left = {r[0] for r in conn.execute("SELECT id FROM sim_orders")}
    assert p.broker_order_id in left                # the legs' parent stays
    # … so after a restart the stop still sells all nine shares
    again = PaperSimBroker(protect=True, store=conn, venue="alpaca")
    [f] = again.check_triggers({"CSCO": 96})
    assert f["leg"] == "stop" and f["qty"] == "9"
    # once the legs are over, the whole bracket is old news and goes
    conn.execute("UPDATE sim_orders SET updated_at=?", (NOW - 40 * DAY,))
    m.prune_sim_orders()
    assert conn.execute("SELECT COUNT(*) FROM sim_orders").fetchone()[0] == 0


# ── 2: the take-profit against where the order can fill ────────────────────
def test_a_take_profit_between_a_resting_limit_and_the_price_goes_through():
    # buy limit 98 with the stock at 100: it can only fill at 98 or less,
    # where a 99.50 take-profit is above the fill
    w = World(price="100")
    out = w.execute(_levels_state(w, stop=90.0, target=99.5, entry=98.0))
    assert out["order_ids"]


def test_a_take_profit_below_the_fill_is_still_refused():
    w = World(price="100")
    # the limit is above the price: it fills at ~100, so 99 sells at once
    with pytest.raises(ValueError, match="take-profit .* would sell straight away"):
        w.execute(_levels_state(w, stop=90.0, target=99.0, entry=101.0))
    assert w.broker.snapshot()["orders"] == {}


# ── 4: Hong Kong codes already stored ──────────────────────────────────────
def _hk_world():
    conn = db.connect(":memory:")
    db.init(conn)
    repo = Repo(conn)
    repo.create_account("saxo-paper", "saxo", "paper", "HKD",
                        fee_model={"type": "flat", "per_trade": "0"}, ts=T0)
    repo.add_instrument("HKEX:861", "861", "HKEX", "HKD")
    repo.add_instrument("HKEX:0700", "0700", "HKEX", "HKD")
    conn.execute(
        "INSERT INTO work_items (id, kind, ticker, state, thread_id,"
        " created_at, updated_at) VALUES ('wi_hk', 'pipeline', '861',"
        " 'executing', 'wi_hk', ?, ?)", (T0, T0))
    repo.create_order(id="o_hk", work_item_id="wi_hk",
                      account_id="saxo-paper", instrument_id="HKEX:861",
                      side="buy", qty=to_micro("1000"),
                      limit_price=to_micro("3"), status="placed",
                      expires_at=T0 + 60, ts=T0, fx_rate=to_micro("1"))
    for t in ("861", "0861", "5"):
        conn.execute("INSERT INTO watchlist (ticker, exchange, added_at)"
                     " VALUES (?, 'HKEX', ?)", (t, T0))
    return conn, repo


def test_stored_hong_kong_codes_become_four_digits():
    conn, repo = _hk_world()
    db.init(conn)                                   # as on the next start
    ids = {r[0]: r[1] for r in conn.execute(
        "SELECT id, ticker FROM instruments WHERE exchange='HKEX'")}
    assert ids == {"HKEX:0861": "0861", "HKEX:0700": "0700"}
    assert repo.order("o_hk")["instrument_id"] == "HKEX:0861"
    assert [r[0] for r in conn.execute(
        "SELECT ticker FROM watchlist WHERE exchange='HKEX' ORDER BY ticker"
        )] == ["0005", "0861"]                      # 861 merged into 0861
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    db.init(conn)                                   # and it is settled
    assert len(conn.execute("SELECT 1 FROM instruments").fetchall()) == 2


def test_other_exchanges_and_lettered_codes_are_left_alone():
    conn = db.connect(":memory:")
    db.init(conn)
    repo = Repo(conn)
    repo.add_instrument("TSE:7203", "7203", "TSE", "JPY")
    repo.add_instrument("SGX:C07", "C07", "SGX", "SGD")
    repo.add_instrument("HKEX:82331", "82331", "HKEX", "HKD")
    db.init(conn)
    assert {r[0] for r in conn.execute("SELECT id FROM instruments")} == {
        "TSE:7203", "SGX:C07", "HKEX:82331"}


# ── 5: a replayed re-protection is the same pair, legs included ────────────
def test_a_replayed_re_protection_returns_its_legs_even_after_a_restart():
    conn = db.connect(":memory:")
    sim = PaperSimBroker(protect=True, store=conn, venue="alpaca")
    p = sim.place_oco(symbol="CSCO", side="sell", qty=4, stop_loss=97,
                      take_profit=115, client_order_id="x-rp")
    assert len(p.legs) == 2
    for s in (sim, PaperSimBroker(protect=True, store=conn, venue="alpaca")):
        again = s.place_oco(symbol="CSCO", side="sell", qty=4, stop_loss=97,
                            take_profit=115, client_order_id="x-rp")
        assert again.broker_order_id == p.broker_order_id
        assert sorted(lg["order_id"] for lg in again.legs) == \
            sorted(lg["order_id"] for lg in p.legs)
        assert {lg["leg"]: lg["price"] for lg in again.legs} == \
            {"stop": "97", "target": "115"}
        assert len(s.orders) == 2                   # no second pair


# ── 9: the throttle key ────────────────────────────────────────────────────
def test_a_note_is_logged_but_is_not_part_of_the_throttle_key(caplog):
    logsetup.reset()
    log = logging.getLogger("t.note")
    clock = lambda: 1000.0                          # noqa: E731
    with caplog.at_level(logging.WARNING, logger="t.note"):
        assert logsetup.swallowed(log, "reusing the last good rate",
                                  note="12 min old", clock=clock)
        assert not logsetup.swallowed(log, "reusing the last good rate",
                                      note="13 min old", clock=clock)
    assert [r.getMessage() for r in caplog.records] == [
        "reusing the last good rate (12 min old)"]


def test_the_throttle_table_does_not_grow_without_bound():
    logsetup.reset()
    log = logging.getLogger("t.bound")
    log.disabled = True
    try:
        for i in range(logsetup.MAX_SEEN + 500):
            logsetup.swallowed(log, f"flag with a number {i}",
                               clock=lambda i=i: float(i))
    finally:
        log.disabled = False
    assert len(logsetup._seen) <= logsetup.MAX_SEEN
    # the newest keys are the ones kept
    assert ("t.bound", f"flag with a number {logsetup.MAX_SEEN + 499}") \
        in logsetup._seen
    logsetup.reset()
