"""Moving a held position's stop-loss / take-profit (app/level_change.py),
and the per-broker table that says where that is allowed
(app/capabilities.py)."""
import contextlib
import json
from decimal import Decimal

import httpx
import pytest

from app import capabilities, level_change, portfolio, protection
from app.accounting import db
from app.accounting.money import to_micro
from app.accounting.repo import Repo
from app.tools.brokers import BrokerError, PaperSimBroker
from app.tools.saxo_broker import SaxoBroker, SaxoSimWithLocalMarkets
from tests.test_live_trading import ref_handler

T0 = 1_793_000_000


class World:
    """A long of 9 CSCO bought at 103 on a paper account, protected by the
    built-in sim: stop-loss 97, take-profit 115, resting as two orders."""

    def __init__(self, broker="alpaca", environment="paper"):
        self.conn = db.connect(":memory:")
        db.init(self.conn)
        self.repo = r = Repo(self.conn)
        self.acct = f"{broker}-paper"
        r.create_account(self.acct, broker, environment, "USD",
                         fee_model={"type": "flat", "per_trade": "0"}, ts=T0,
                         allow_live=environment == "live",
                         external_ref="K==" if environment == "live" else None)
        r.add_instrument("NASDAQ:CSCO", "CSCO", "NASDAQ", "USD")
        r.deposit(self.acct, to_micro("50000"), ts=T0)
        self.conn.execute(
            "INSERT INTO work_items (id, kind, ticker, state, thread_id,"
            " created_at, updated_at) VALUES ('wi1','pipeline','CSCO','done',"
            "'wi1',?,?)", (T0, T0))
        self.sim = PaperSimBroker(protect=True)
        placed = self.sim.place_bracket(symbol="CSCO", side="buy", qty=9,
                                        limit=103, stop_loss=97,
                                        take_profit=115)
        r.create_order(id="o1", account_id=self.acct,
                       instrument_id="NASDAQ:CSCO", side="buy",
                       qty=to_micro("9"), expires_at=T0 + 999, ts=T0,
                       work_item_id="wi1", limit_price=to_micro("103"),
                       broker_order_id=placed.broker_order_id)
        r.record_fill("o1", broker_fill_id="f1", qty=to_micro("9"),
                      price_native=to_micro("103"), fx_rate=to_micro("1"),
                      ts=T0)
        protection.persist_legs(r, parent="o1", legs=placed.legs, side="sell",
                                qty=to_micro("9"), ts=T0, work_item_id="wi1")
        self.brokers = {broker: self.sim}

    def change(self, **kw):
        return level_change.change(self.repo, self.brokers, self.acct,
                                   "NASDAQ:CSCO", ts=T0 + 60, env={}, **kw)

    def legs(self):
        return {l["leg"]: l["price"] for l in portfolio.legs_for(
            self.conn, self.acct, "NASDAQ:CSCO")}


# ── the table ─────────────────────────────────────────────────────────────
def test_every_broker_row_names_every_part():
    for venue, row in capabilities.CAN_CHANGE.items():
        assert set(row) == set(capabilities.PARTS), venue


def test_a_broker_without_a_row_can_change_nothing_but_the_desks_alert():
    c = capabilities.can_change("some-new-broker")
    assert c == {"stop_loss": False, "take_profit": False,
                 "trailing_stop": False, "trailing_alert": True}


def test_where_a_positions_orders_rest_decides_the_row():
    v = capabilities.venue_of
    assert v("alpaca", "paper", "NASDAQ", sim_on=True) == "builtin"
    assert v("moomoo", "paper", "NASDAQ", sim_on=True) == "builtin"
    assert v("saxo", "paper", "NASDAQ", sim_on=False) == "builtin"
    assert v("saxo", "paper", "NASDAQ", sim_on=True) == "saxo"
    assert v("saxo", "paper", "SGX", sim_on=True) == "builtin"
    # the order's own id settles it: the sim's ids start "sim-"
    assert v("saxo", "paper", "NASDAQ", sim_on=True,
             order_id="sim-ab12") == "builtin"
    assert v("saxo_live", "live", "NASDAQ", sim_on=True) == "saxo_live"
    assert v("newbroker", "live", "NASDAQ", sim_on=True) == "unknown"


def test_real_money_is_off_until_it_has_been_proven_on_sim():
    assert not any(capabilities.CAN_CHANGE["saxo_live"].values())
    assert capabilities.CAN_CHANGE["saxo"]["trailing_stop"] is False


def test_the_page_is_told_what_it_may_offer():
    w = World()

    class M:
        def metrics(self, syms):
            return {s: {"price": 105, "currency": "USD"} for s in syms}

        def fx(self, a, b):
            return Decimal(1)
    d = portfolio.build(w.conn, M(), clock=lambda: T0, env={})
    p = d["exchanges"]["NASDAQ"][0]
    assert p["can_change"] == {
        "stop_loss": True, "take_profit": True, "trailing_stop": False,
        "trailing_alert": True, "venue": "builtin",
        "venue_label": "the desk's simulation"}


# ── the change itself ─────────────────────────────────────────────────────
def test_moving_the_stop_moves_the_resting_order_and_the_book():
    w = World()
    out = w.change(stop=99.5, price=105)
    assert out["ok"] and [(c["leg"], c["from"], c["to"])
                          for c in out["changed"]] == [("stop", "97", "99.5")]
    assert w.legs() == {"stop": 99.5, "target": 115}
    # the sim now sells at the NEW stop, not the old one
    assert w.sim.check_triggers({"CSCO": 99}) != []
    ev = w.conn.execute("SELECT actor, to_state, payload FROM events WHERE"
                        " to_state='levels_changed'").fetchone()
    assert ev["actor"] == "human"
    assert json.loads(ev["payload"])["changed"][0]["to"] == "99.5"


def test_both_can_move_at_once_and_an_unchanged_one_is_left_alone():
    w = World()
    out = w.change(stop=100, target=115, price=105)
    assert [c["leg"] for c in out["changed"]] == ["stop"]
    out = w.change(stop=101, target=120, price=105)
    assert {c["leg"] for c in out["changed"]} == {"stop", "target"}
    assert w.legs() == {"stop": 101, "target": 120}


@pytest.mark.parametrize("kw, words", [
    ({"stop": 106, "price": 105}, "below the price now"),
    ({"target": 104, "price": 105}, "above the price now"),
    ({"stop": 116}, "below the take-profit"),
    ({"target": 96}, "above the stop-loss"),
    ({"stop": 0}, "more than zero"),
    ({}, "give a new"),
])
def test_a_level_on_the_wrong_side_is_refused(kw, words):
    w = World()
    with pytest.raises(level_change.LevelChangeError) as e:
        w.change(**kw)
    assert words in str(e.value)
    assert w.legs() == {"stop": 97, "target": 115}      # nothing moved


def test_a_broker_whose_row_says_no_is_refused(monkeypatch):
    w = World()
    monkeypatch.setitem(capabilities.CAN_CHANGE, "builtin",
                        {**capabilities.NOTHING, "take_profit": True})
    with pytest.raises(level_change.LevelChangeError) as e:
        w.change(stop=99, price=105)
    assert e.value.status == 409 and "does not let the desk change" in str(
        e.value)
    assert w.change(target=118, price=105)["ok"]        # its row allows this
    assert w.legs() == {"stop": 97, "target": 118}


def test_a_row_that_says_yes_without_an_adapter_method_is_refused():
    w = World()

    class NoChange:                                     # an older adapter
        pass
    w.brokers = {"alpaca": NoChange()}
    with pytest.raises(level_change.LevelChangeError) as e:
        w.change(stop=99, price=105)
    assert e.value.status == 409 and "can change this order" in str(e.value)


def test_no_resting_order_means_nothing_to_change():
    w = World()
    w.conn.execute("UPDATE orders SET status='cancelled' WHERE leg='target'")
    with pytest.raises(level_change.LevelChangeError) as e:
        w.change(target=120, price=105)
    assert "no take-profit order resting" in str(e.value)
    with pytest.raises(level_change.LevelChangeError) as e:
        level_change.change(w.repo, w.brokers, w.acct, "NASDAQ:NOPE",
                            stop=1, ts=T0, env={})
    assert e.value.status == 404


def test_a_venue_refusal_is_reported_and_the_book_is_not_touched():
    w = World()

    class Refuses(PaperSimBroker):
        def change_leg(self, broker_order_id, price, *, symbol=None):
            raise BrokerError("TooFarFromEntryOrder")
    w.brokers = {"alpaca": Refuses(protect=True)}
    with pytest.raises(level_change.LevelChangeError) as e:
        w.change(stop=99, price=105)
    assert e.value.status == 502 and "TooFarFromEntryOrder" in str(e.value)
    assert w.legs() == {"stop": 97, "target": 115}


def test_the_sim_refuses_an_order_that_is_no_longer_resting():
    sim = PaperSimBroker(protect=True)
    p = sim.place_bracket(symbol="CSCO", side="buy", qty=9, limit=103,
                          stop_loss=97, take_profit=115)
    stop = p.legs[0]["order_id"]
    sim.cancel(stop)
    with pytest.raises(BrokerError):
        sim.change_leg(stop, 99)
    with pytest.raises(BrokerError):
        sim.change_leg(p.broker_order_id, 99)           # the entry itself


# ── Saxo ──────────────────────────────────────────────────────────────────
def saxo(sent, orders):
    def extra(req):
        body = json.loads(req.content or b"{}")
        sent.append((req.method, req.url.path, body))
        if req.method == "PATCH":
            for o in orders:
                if str(o["OrderId"]) == str(body.get("OrderId")):
                    o["Price"] = body["OrderPrice"]
            return httpx.Response(200, json={"OrderId": body.get("OrderId")})
        if req.url.path.endswith("/orders/me"):
            return httpx.Response(200, json={"Data": orders})
        return httpx.Response(200, json={"Data": []})
    return SaxoBroker("sim", token=lambda: "t", account_key="K==",
                      client=httpx.Client(transport=httpx.MockTransport(
                          ref_handler(extra))), order_gap_s=0)


def working(order_id, otype, price, side="Sell"):
    return {"OrderId": order_id, "AccountKey": "K==", "OpenOrderType": otype,
            "Price": price, "Amount": 10, "BuySell": side, "Status": "Working",
            "OrderRelation": "Oco",
            "DisplayAndFormat": {"Symbol": "AAPL:xnas"}}


def test_saxo_changes_only_the_price_and_sends_the_rest_back_unchanged():
    sent, orders = [], [working("78", "StopIfTraded", 140.0),
                        working("79", "Limit", 180.0)]
    out = saxo(sent, orders).change_leg("78", 143.337, symbol="AAPL")
    [(_, path, body)] = [s for s in sent if s[0] == "PATCH"]
    assert path.endswith("/trade/v2/orders")
    assert body["OrderId"] == "78" and body["OrderType"] == "StopIfTraded"
    assert body["Amount"] == 10 and body["BuySell"] == "Sell"
    assert body["AccountKey"] == "K==" and body["AssetType"] == "Stock"
    assert body["OrderDuration"] == {"DurationType": "GoodTillCancel"}
    # a sell stop rounds DOWN onto the tick; read back from Saxo afterwards
    assert body["OrderPrice"] == 143.33
    assert out == {"price": "143.33", "verified": True}


def test_saxo_take_profit_rounds_up_and_a_trailing_stop_is_refused():
    sent, orders = [], [working("78", "TrailingStopIfTraded", 140.0),
                        working("79", "Limit", 180.0)]
    b = saxo(sent, orders)
    b.change_leg("79", 185.001, symbol="AAPL")
    assert [s[2]["OrderPrice"] for s in sent if s[0] == "PATCH"] == [185.01]
    with pytest.raises(BrokerError) as e:
        b.change_leg("78", 141, symbol="AAPL")
    assert "trailing stop" in str(e.value)
    with pytest.raises(BrokerError) as e:
        b.change_leg("999", 141, symbol="AAPL")
    assert "no working order" in str(e.value)
    assert len([s for s in sent if s[0] == "PATCH"]) == 1


def test_saxo_live_asks_the_armed_guard_first():
    def guard():
        raise RuntimeError("not armed")
    b = SaxoBroker("live", token=lambda: "t", account_key="K==", guard=guard,
                   client=httpx.Client(transport=httpx.MockTransport(
                       ref_handler(lambda req: httpx.Response(500)))),
                   order_gap_s=0)
    with pytest.raises(RuntimeError):
        b.change_leg("78", 141, symbol="AAPL")


def test_the_saxo_paper_router_sends_each_order_to_its_owner():
    class V:
        environment, fractional = "sim", False

        def change_leg(self, oid, price, *, symbol=None):
            return {"price": "1", "verified": True, "who": "saxo"}
    local = PaperSimBroker(protect=True)
    p = local.place_bracket(symbol="D05.SI", side="buy", qty=100, limit=30,
                            stop_loss=28)
    r = SaxoSimWithLocalMarkets(V(), local)
    assert r.change_leg("78", 141, symbol="AAPL")["who"] == "saxo"
    assert r.change_leg(p.legs[0]["order_id"], 28.5,
                        symbol="D05.SI") == {"price": "28.5", "verified": True}


# ── the routes ────────────────────────────────────────────────────────────
@contextlib.contextmanager
def serve(conn, repo, market):
    """A dashboard on a free port over this book: (base url, its key)."""
    import threading

    from app.dashboards import create_server
    srv = create_server(conn, repo, market, secret="s", dash_key="k3y",
                        api_base="http://127.0.0.1:9", clock=lambda: T0 + 60,
                        port=0, sse_interval=None, env={})
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", "k3y"
    finally:
        srv.shutdown()


def test_the_api_route_is_for_a_human_with_the_secret():
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    from app.api.main import create_app
    w = World()
    client = TestClient(create_app(SimpleNamespace(repo=w.repo), w.conn,
                                   "sec", clock=lambda: T0 + 60,
                                   brokers=w.brokers))
    body = {"account_id": w.acct, "instrument_id": "NASDAQ:CSCO",
            "stop": 99, "price": 105}
    hdr = {"x-claire-secret": "sec"}
    assert client.post("/internal/levels", json=body).status_code == 403
    assert client.post("/internal/levels", json={**body, "actor": "claire"},
                       headers=hdr).status_code == 403
    assert client.post("/internal/levels", json={**body, "stop": 110},
                       headers=hdr).status_code == 422
    assert client.post("/internal/levels", json={**body, "stop": -1},
                       headers=hdr).status_code == 422
    r = client.post("/internal/levels", json=body, headers=hdr)
    assert r.status_code == 200 and r.json()["changed"][0]["to"] == "99"
    assert w.legs() == {"stop": 99, "target": 115}


def test_the_dashboard_route_needs_the_key_and_adds_the_price_now(
        monkeypatch):
    import httpx

    from app.dash import claire_api
    w = World()
    seen = []
    monkeypatch.setattr(claire_api, "post", lambda ctx, path, body=None,
                        **kw: seen.append((path, body)) or {"ok": True})

    class M:
        def quote(self, s):
            return {"price": 105.5}
    with serve(w.conn, w.repo, M()) as (base, key):
        body = {"account_id": w.acct, "instrument_id": "NASDAQ:CSCO",
                "stop": "99.5"}
        with httpx.Client(timeout=5) as c:
            assert c.post(base + "/api/position-levels",
                          json=body).status_code == 403
            h = {"X-Dash-Key": key}
            assert c.post(base + "/api/position-levels", json={
                **body, "stop": "abc"}, headers=h).status_code == 422
            assert c.post(base + "/api/position-levels", json={
                "account_id": w.acct, "instrument_id": "NASDAQ:CSCO"},
                headers=h).status_code == 422
            assert c.post(base + "/api/position-levels", json=body,
                          headers=h).status_code == 200
    assert seen == [("/internal/levels", {
        "account_id": w.acct, "instrument_id": "NASDAQ:CSCO",
        "actor": "human", "stop": 99.5, "price": 105.5})]


def test_the_orders_page_is_told_which_resting_orders_may_be_moved(
        monkeypatch):
    import httpx

    w = World()

    class M:
        def spark(self, syms):
            return {s: {"price": 105} for s in syms}
    with serve(w.conn, w.repo, M()) as (base, key):
        d = httpx.get(base + "/api/orders", headers={"X-Dash-Key": key},
                      timeout=5).json()
    legs = {o["leg"]: o for o in d["legs"]}
    assert legs["stop"]["can_change"] and legs["target"]["can_change"]
    assert legs["stop"]["venue_label"] == "the desk's simulation"


# ── the pages ─────────────────────────────────────────────────────────────
def test_home_and_orders_offer_the_edit_only_through_the_shared_editor():
    from pathlib import Path
    web = Path(__file__).resolve().parents[1] / "web"
    home = (web / "index.html").read_text()
    orders = (web / "orders.html").read_text()
    lv = (web / "levels.js").read_text()
    for page in (home, orders):
        assert '<script src="/levels.js"></script>' in page
        assert "levelsOpen(" in page and "levelsRestore()" in page
        assert "lvlbox-" in page
    # the bar's handles exist only where levelsCan says yes
    assert "levelsCan(p)" in home and 'role="slider"' in home
    assert "can.stop.ok ? f.stop : null" in home
    # the Orders page asks the server's flag, and says why when it is fixed
    assert "o.can_change" in orders and "🔒 fixed" in orders
    # the editor posts to the two routes and nowhere else
    assert "/api/position-levels" in lv and "/api/position-floor" in lv
    assert "p.can_change" in lv
    # Home has the Sell button, the same one as Portfolio
    assert '<script src="/sell.js"></script>' in home
    assert "sellButton(k)" in home and "sellbox-" in home
