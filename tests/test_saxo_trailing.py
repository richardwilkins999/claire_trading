"""The trailing floor as a Saxo trailing stop (2026-11-07): on a venue that
holds stops, the floor approved with an entry goes to Saxo as the bracket's
stop — TrailingStopIfTraded, starting at the stop and following the market
at (floor % of the entry). Checked against SIM's precheck the same day."""
import json
from decimal import Decimal

import httpx

from app.tools.brokers import PaperSimBroker, PlacedOrder
from app.tools.saxo_broker import SaxoBroker, SaxoSimWithLocalMarkets
from tests.test_live_trading import ref_handler
from tests.test_protective_legs import LegVenue, World


def saxo(sent):
    def extra(req):
        body = json.loads(req.content or b"{}")
        sent.append((req.url.path, body))
        if req.url.path.endswith("/precheck"):
            return httpx.Response(200, json={"PreCheckResult": "Ok"})
        if req.url.path.endswith("/orders"):
            return httpx.Response(200, json={"OrderId": "77", "Orders": [
                {"OrderId": "78"}, {"OrderId": "79"}]})
        return httpx.Response(200, json={"Data": []})
    return SaxoBroker("sim", token=lambda: "t", account_key="K==",
                      client=httpx.Client(transport=httpx.MockTransport(
                          ref_handler(extra))), order_gap_s=0)


def legs_of(body):
    return {o["OrderType"]: o for o in body.get("Orders", [])}


def test_bracket_stop_becomes_a_trailing_stop():
    sent = []
    b = saxo(sent)
    out = b.place_bracket(symbol="AAPL", side="buy", qty=10, limit=150,
                          stop_loss=140, take_profit=180,
                          trail_distance=12.003)
    assert out.status == "accepted"
    body = [s for p, s in sent if p.endswith("/trade/v2/orders")][0]
    legs = legs_of(body)
    trail = legs["TrailingStopIfTraded"]
    assert trail["OrderPrice"] == 140.0 and trail["BuySell"] == "Sell"
    # rounded UP onto the tick: never tighter than the floor asked for
    assert trail["TrailingStopDistanceToMarket"] == 12.01
    assert trail["TrailingStopStep"] == 0.01
    assert legs["Limit"]["OrderPrice"] == 180.0          # target unchanged
    assert "StopIfTraded" not in legs


def test_without_a_floor_the_stop_stays_fixed():
    sent = []
    saxo(sent).place_bracket(symbol="AAPL", side="buy", qty=10, limit=150,
                             stop_loss=140)
    body = [s for p, s in sent if p.endswith("/trade/v2/orders")][0]
    assert list(legs_of(body)) == ["StopIfTraded"]


def test_precheck_and_reprotect_send_the_trailing_stop_too():
    sent = []
    b = saxo(sent)
    out = b.precheck(symbol="AAPL", side="buy", qty=1, limit=150,
                     stop_loss=140, trail_distance=12)
    assert out["ok"] and out["legs"][0]["type"] == "TrailingStopIfTraded"
    assert out["legs"][0]["trail"] == 12.0
    b.place_oco(symbol="AAPL", side="sell", qty=4, stop_loss=146,
                take_profit=180, trail_distance=12)
    body = [s for p, s in sent if p.endswith("/trade/v2/orders")][-1]
    assert legs_of(body)["TrailingStopIfTraded"]["OrderPrice"] == 146.0


def test_router_keeps_the_floor_off_locally_simulated_markets():
    seen = []

    class V:
        environment, fractional = "sim", False

        def place_bracket(self, **kw):
            seen.append(kw)
            return PlacedOrder("1", "accepted")
    r = SaxoSimWithLocalMarkets(V(), PaperSimBroker())
    r.place_bracket(symbol="AAPL", side="buy", qty=1, limit=10, stop_loss=9,
                    trail_distance=0.8)
    assert seen[-1]["trail_distance"] == 0.8
    out = r.place_bracket(symbol="D05.SI", side="buy", qty=100, limit=40,
                          stop_loss=37, trail_distance=3.2)
    assert out.broker_order_id.startswith("sim-") and len(seen) == 1


# ── through execution: the floor is on the order row ─────────────────────
class TrailVenue(LegVenue):
    def __init__(self):
        super().__init__()
        self.trails = []

    def place_bracket(self, *, trail_distance=None, **kw):
        self.trails.append(("bracket", trail_distance))
        return super().place_bracket(**kw)

    def place_oco(self, *, trail_distance=None, **kw):
        self.trails.append(("oco", trail_distance))
        return super().place_oco(**kw)


def world():
    w = World()
    w.venue = TrailVenue()
    from app.custodian import Custodian
    from app.graph.nodes_execution import build_execute
    w.execute = build_execute(w.repo, {w.broker: w.venue}, w.market,
                              account_for=lambda b: w.acct, clock=w.clock)
    w.custodian = Custodian(
        w.conn, w.repo, {w.broker: w.venue}, resume_post=lambda b: (200, {}),
        account_broker=lambda a: w.broker,
        fx_rate_for=lambda i, a: 1_000_000, clock=w.clock,
        alert=lambda text, **kw: w.alerts.append(text))
    return w


def buy(w, trail):
    st = w.state("wi_buy", "buy", size=1000.0, stop=97.0, target=115.0)
    st = st.model_copy(update={"approval": st.approval.model_copy(
        update={"trail_pct": trail})})
    return w.execute(st)["order_ids"][0]


def test_execution_sends_the_approved_floor_as_a_distance():
    w = world()
    oid = buy(w, 8)
    assert w.repo.order(oid)["trail_pct"] == 8
    # 8% of the 103 entry
    assert w.venue.trails == [("bracket", float(Decimal("103") * 8 / 100))]


def test_no_floor_no_trailing_stop():
    w = world()
    buy(w, None)
    assert w.venue.trails == [("bracket", None)]


def test_an_exit_carries_no_floor():
    w = world()
    entry = buy(w, 8)
    o = w.repo.order(entry)
    w.venue.fill(o["broker_order_id"], o["qty"] / 1e6, "103")
    w.custodian.run_once()
    st = w.state("wi_sell", "sell", qty=5, kind="manual")
    st = st.model_copy(update={"approval": st.approval.model_copy(
        update={"trail_pct": 8})})
    sell = w.execute(st)["order_ids"][0]
    assert w.repo.order(sell)["trail_pct"] is None
    assert w.venue.trails[-1] == ("bracket", None)


def test_re_protection_keeps_trailing():
    w = world()
    entry = buy(w, 8)
    o = w.repo.order(entry)
    w.venue.fill(o["broker_order_id"], o["qty"] / 1e6, "103")
    w.custodian.run_once()
    out = w.execute(w.state("wi_sell", "sell", qty=5, kind="manual"))
    sell = w.repo.order(out["order_ids"][0])
    w.venue.fill(sell["broker_order_id"], 5, "104")
    w.custodian.run_once()
    assert ("oco", float(Decimal("103") * 8 / 100)) in w.venue.trails
    assert w.repo.order(sell["id"])["protect_state"] == "done"


# ── the 2026-11-11 port: Orders and Portfolio say "trailing stop" ─────────
def test_portfolio_and_orders_mark_the_saxo_trailing_stop():
    from app.portfolio import legs_for
    w = world()
    entry = buy(w, 8)
    o = w.repo.order(entry)
    w.venue.fill(o["broker_order_id"], o["qty"] / 1e6, "103")
    w.custodian.run_once()
    legs = {l["leg"]: l for l in legs_for(w.conn, w.acct, "NASDAQ:CSCO")}
    assert legs["stop"]["trails"] == float(Decimal("103") * 8 / 100)
    assert legs["stop"]["trail_pct"] == 8 and "trails" not in legs["target"]


def test_a_stop_without_a_floor_is_not_called_trailing():
    from app.portfolio import legs_for
    w = world()
    entry = buy(w, None)
    o = w.repo.order(entry)
    w.venue.fill(o["broker_order_id"], o["qty"] / 1e6, "103")
    w.custodian.run_once()
    assert all("trails" not in l for l in legs_for(w.conn, w.acct,
                                                   "NASDAQ:CSCO"))
