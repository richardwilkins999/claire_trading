"""The built-in sims enforce a bracket's stop-loss / take-profit
(tools/brokers.PaperSimBroker, protect=True; custodian._sim_triggers).

Before 2026-11-11 the in-process sim only remembered the levels: a stop on
an alpaca / moomoo / SGX paper trade never sold anything — the watcher's
trailing alert was the only backstop and the Orders page said "not at the
broker". Now the sim holds them as resting orders the desk books (§11a), and
the custodian fills them against the live price while the market is open."""
from decimal import Decimal

from app.accounting import db
from app.custodian import Custodian
from app.graph.nodes_execution import build_execute
from app.tools.brokers import PaperSimBroker
from tests.test_protective_legs import T0, World

CLOSED = T0 + 12 * 3600                 # NASDAQ shut


# ── the sim on its own ────────────────────────────────────────────────────
def test_a_bracket_rests_its_stop_and_target_as_orders_of_their_own():
    sim = PaperSimBroker(protect=True)
    p = sim.place_bracket(symbol="CSCO", side="buy", qty=9, limit=103,
                          stop_loss=97, take_profit=115)
    assert p.status == "accepted"
    assert [(lg["leg"], lg["price"]) for lg in p.legs] == [("stop", "97"),
                                                           ("target", "115")]
    assert sim.check_triggers({"CSCO": 100}) == []          # in between
    stop, tgt = (lg["order_id"] for lg in p.legs)
    st = sim.order_status(stop)
    assert st["status"] == "placed" and st["order_type"] == "StopIfTraded"


def test_a_stop_sells_at_the_price_seen_and_cancels_the_take_profit():
    sim = PaperSimBroker(protect=True)
    p = sim.place_bracket(symbol="CSCO", side="buy", qty=9, limit=103,
                          stop_loss=97, take_profit=115)
    stop, tgt = (lg["order_id"] for lg in p.legs)
    [f] = sim.check_triggers({"CSCO": Decimal("95.5")})     # gapped through
    assert (f["leg"], f["side"], f["qty"], f["price"]) == \
        ("stop", "sell", "9", "95.5")
    assert sim.order_status(stop)["status"] == "filled"
    assert sim.order_status(tgt)["status"] == "cancelled"   # one-cancels-other
    assert sim.check_triggers({"CSCO": 120}) == []          # nothing left


def test_a_take_profit_fills_at_its_own_price():
    sim = PaperSimBroker(protect=True)
    p = sim.place_bracket(symbol="CSCO", side="buy", qty=9, limit=103,
                          stop_loss=97, take_profit=115)
    [f] = sim.check_triggers({"CSCO": 118})
    assert (f["leg"], f["price"]) == ("target", "115")
    assert sim.order_status(p.legs[0]["order_id"])["status"] == "cancelled"


def test_a_short_is_protected_the_other_way_round():
    sim = PaperSimBroker(protect=True)
    p = sim.place_bracket(symbol="CSCO", side="sell", qty=5, limit=103,
                          stop_loss=110, take_profit=95)
    assert sim.check_triggers({"CSCO": 108}) == []
    [f] = sim.check_triggers({"CSCO": 111})
    assert (f["leg"], f["side"], f["price"]) == ("stop", "buy", "111")


def test_legs_are_armed_only_for_what_the_entry_filled():
    sim = PaperSimBroker(fill_mode="manual", protect=True)
    p = sim.place_bracket(symbol="CSCO", side="buy", qty=10, limit=103,
                          stop_loss=97)
    assert sim.check_triggers({"CSCO": 90}) == []           # nothing bought
    sim.simulate_fill(p.broker_order_id, 4, 103)
    [f] = sim.check_triggers({"CSCO": 90})
    assert f["qty"] == "4"


def test_cancelling_an_unfilled_entry_cancels_its_legs():
    sim = PaperSimBroker(fill_mode="manual", protect=True)
    p = sim.place_bracket(symbol="CSCO", side="buy", qty=10, limit=103,
                          stop_loss=97, take_profit=115)
    sim.cancel(p.broker_order_id)
    assert {sim.order_status(lg["order_id"])["status"] for lg in p.legs} \
        == {"cancelled"}


def test_without_protect_the_sim_behaves_as_before():
    sim = PaperSimBroker()
    p = sim.place_bracket(symbol="CSCO", side="buy", qty=9, limit=103,
                          stop_loss=97, take_profit=115)
    assert p.legs == [] and sim.check_triggers({"CSCO": 1}) == []
    assert sim.place_oco(symbol="CSCO", side="sell", qty=9,
                         stop_loss=97).status == "rejected"


def test_resting_stops_survive_a_restart():
    """claire-api restarts often; the sim used to forget every order."""
    conn = db.connect(":memory:")
    a = PaperSimBroker(protect=True, store=conn, venue="alpaca")
    p = a.place_bracket(symbol="CSCO", side="buy", qty=9, limit=103,
                        stop_loss=97, take_profit=115,
                        client_order_id="ord_1")
    b = PaperSimBroker(protect=True, store=conn, venue="alpaca")  # restart
    assert b.order_status(p.broker_order_id)["status"] == "filled"
    assert b.place_bracket(symbol="CSCO", side="buy", qty=9, limit=103,
                           client_order_id="ord_1").broker_order_id == \
        p.broker_order_id                                    # still deduped
    [f] = b.check_triggers({"CSCO": 96})
    assert f["order_id"] == p.legs[0]["order_id"]
    c = PaperSimBroker(protect=True, store=conn, venue="alpaca")
    assert c.order_status(p.legs[1]["order_id"])["status"] == "cancelled"
    # another sim's orders are its own
    assert PaperSimBroker(protect=True, store=conn, venue="moomoo").orders \
        == {}


def test_re_protection_places_a_standalone_pair():
    sim = PaperSimBroker(protect=True)
    p = sim.place_oco(symbol="CSCO", side="sell", qty=4, stop_loss=97,
                      take_profit=115, client_order_id="x-rp")
    assert p.status == "accepted" and len(p.legs) == 2
    [f] = sim.check_triggers({"CSCO": 96})
    assert f["qty"] == "4"


# ── through the desk: book, custodian, position ───────────────────────────
class SimWorld(World):
    """test_protective_legs.World, on the built-in sim instead of Saxo."""

    def __init__(self):
        super().__init__()
        self.prices = {"CSCO": {"price": Decimal("103"), "stale": False}}
        self.venue = PaperSimBroker(protect=True, store=self.conn,
                                    venue="saxo")
        self.execute = build_execute(
            self.repo, {self.broker: self.venue}, self.market,
            account_for=lambda b: self.acct, clock=self.clock)
        self.custodian = Custodian(
            self.conn, self.repo, {self.broker: self.venue},
            resume_post=lambda b: (200, {}),
            account_broker=lambda a: self.broker,
            fx_rate_for=lambda i, a: 1_000_000, clock=self.clock,
            alert=lambda text, **kw: self.alerts.append(text),
            quote=lambda s: self.prices[s])

    def buy(self):
        out = self.execute(self.state("wi_buy", "buy", qty=9, size=1000.0,
                                      stop=97.0, target=115.0))
        self.custodian.run_once()                  # books the entry fill
        return out["order_ids"][0]

    def held(self):
        return self.repo.position(self.acct, "NASDAQ:CSCO")[0] / 1e6


def test_the_desk_books_the_sims_legs_and_counts_the_position_protected():
    w = SimWorld()
    w.buy()
    assert w.held() == 9
    assert sorted((lg["leg"], lg["status"]) for lg in w.legs()) == \
        [("stop", "placed"), ("target", "placed")]
    rep = w.custodian.run_once()
    assert not [f for f in rep["flags"] if "UNPROTECTED" in f]
    assert not [f for f in rep["flags"] if "DRIFT" in f]


def test_a_stop_reached_sells_the_position_and_tells_you():
    w = SimWorld()
    w.buy()
    w.prices["CSCO"]["price"] = Decimal("96.2")
    rep = w.custodian.run_once()
    assert [f["leg"] for f in rep["sim_triggered"]] == ["stop"]
    assert w.held() == 0                           # sold, and booked
    by = {lg["leg"]: lg["status"] for lg in w.legs()}
    assert by == {"stop": "filled", "target": "cancelled"}
    fill = w.conn.execute(
        "SELECT e.price_native FROM executions e JOIN orders o ON"
        " o.id=e.order_id WHERE o.leg='stop'").fetchone()
    assert fill[0] == 96_200_000                   # at the price seen
    assert any("Stop-loss reached on the built-in sim" in a
               for a in w.alerts)


def test_nothing_fires_while_the_market_is_shut_or_the_price_is_old():
    w = SimWorld()
    w.buy()
    w.prices["CSCO"] = {"price": Decimal("50"), "stale": True}
    assert w.custodian.run_once()["sim_triggered"] == []
    w.t["now"] = CLOSED
    w.prices["CSCO"] = {"price": Decimal("50"), "stale": False}
    assert w.custodian.run_once()["sim_triggered"] == []
    assert w.held() == 9


def test_a_desk_sell_cancels_the_sims_legs_first():
    """The Sell button / an exit review: the resting stop must not sell the
    same shares again (protection.cancel_for, as at Saxo)."""
    w = SimWorld()
    w.buy()
    w.execute(w.state("wi_sell", "sell", qty=9, kind="sell_review",
                      entry=104.0))
    w.custodian.run_once()
    assert w.held() == 0
    assert {lg["status"] for lg in w.legs()} == {"cancelled"}
    w.prices["CSCO"]["price"] = Decimal("90")
    assert w.custodian.run_once()["sim_triggered"] == []


def test_a_partial_desk_sell_re_protects_the_rest_at_the_sim():
    w = SimWorld()
    w.buy()
    w.execute(w.state("wi_half", "sell", qty=4, kind="sell_review",
                      entry=104.0))
    w.custodian.run_once()
    assert w.held() == 5
    working = [lg for lg in w.legs() if lg["status"] == "placed"]
    assert sorted(lg["leg"] for lg in working) == ["stop", "target"]
    assert {lg["qty"] for lg in working} == {5_000_000}
    w.prices["CSCO"]["price"] = Decimal("96")
    w.custodian.run_once()
    assert w.held() == 0
