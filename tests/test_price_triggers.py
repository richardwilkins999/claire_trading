"""Watchlist price triggers (DESIGN.md §15a) — offline.

The engine runs against the real Desk (override graph, gate, token), the
real approval authorization (approvals.thesis_action) and, for the
automatic mode, the real /internal/resume and execute node on a venue that
behaves like Saxo (tests.test_protective_legs.LegVenue)."""
import json
import sqlite3
import threading
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app import price_triggers as pt
from app import scheduler
from app.accounting import db
from app.accounting.money import to_micro
from tests.test_protective_legs import T0, World, _desk

ROOT = Path(__file__).resolve().parents[1]
CLOSED = T0 + 12 * 3600          # 02:00 UTC Wed = 22:00 New York: closed


class Quotes:
    """spark/quote/fx like app.tools.market.Market; records every call."""

    def __init__(self, price="103", stale=False, broken=()):
        self.price, self.stale, self.broken = Decimal(price), stale, \
            set(broken)
        self.calls = []

    def _one(self, s):
        if s in self.broken:
            raise RuntimeError(f"no quote for {s}")
        return {"symbol": s, "price": self.price, "currency": "USD",
                "stale": self.stale, "src": "test"}

    def spark(self, symbols):
        self.calls.append(("spark", tuple(symbols)))
        return {s: self._one(s) for s in symbols}

    def quote(self, symbol):
        self.calls.append(("quote", symbol))
        return self._one(symbol)

    def fx(self, a, b):
        return Decimal(1)


def body(**kw):
    b = {"ticker": "CSCO", "exchange": "NASDAQ", "direction": "buy",
         "condition": "lte", "trigger_price": 103.5, "qty": 5, "stop": 97,
         "target": 115, "broker": "saxo"}
    b.update(kw)
    return {k: v for k, v in b.items() if v is not None}


def make(w, **kw):
    return pt.create(w.conn, body(**kw), created_by="human (test)",
                     clock=w.clock)


def run(w, desk, market, *, resume_post=None, alerts=None, clock=None,
        env=None):
    return pt.run_once(w.conn, desk, market,
                       resume_post=resume_post or (lambda b: (599, {})),
                       alert=(lambda text, **k: alerts.append(text))
                       if alerts is not None else None,
                       clock=clock or w.clock, env=env)


def status(w, tid):
    return pt.get(w.conn, tid)


# ── evaluation ───────────────────────────────────────────────────────────
def test_lte_and_gte():
    m = to_micro("5")
    assert pt.hit("lte", m, Decimal("4.98")) and pt.hit("lte", m, Decimal(5))
    assert not pt.hit("lte", m, Decimal("5.01"))
    assert pt.hit("gte", m, Decimal("5.01")) and pt.hit("gte", m, Decimal(5))
    assert not pt.hit("gte", m, Decimal("4.99"))


def test_ask_mode_fires_once_into_an_approval_card():
    w = World()
    desk = _desk(w.conn, w.repo)
    t = make(w, note="buy the dip")
    alerts = []
    out = run(w, desk, Quotes("103"), alerts=alerts)
    assert [f["id"] for f in out["fired"]] == [t["id"]]
    row = status(w, t["id"])
    assert (row["status"], row["outcome"]) == ("fired", "card")
    assert row["fired_price"] == to_micro("103")
    wi = desk.work_item(row["work_item_id"])
    assert (wi["kind"], wi["state"]) == ("manual", "awaiting_approval")
    assert json.loads(wi["suggested_json"]) == {"qty": 5.0, "broker": "saxo"}
    th = json.loads(wi["thesis_json"])
    assert (th["direction"], th["entry_high"], th["stop_loss"],
            th["take_profit"]) == ("buy", 103.5, 97.0, 115.0)
    assert wi["trigger"].startswith("manual buy from trigger: trigger #")
    assert "buy the dip" in wi["trigger"]
    assert len(alerts) == 1 and "CSCO hit 103" in alerts[0] and \
        "≤ 103.50" in alerts[0] and "approval card ready" in alerts[0]
    # nothing was approved: the card waits for the human
    assert wi["token_state"] == "minted"
    assert not w.conn.execute("SELECT 1 FROM events WHERE item_id=? AND"
                              " to_state='pre_approved'",
                              (wi["id"],)).fetchone()
    # one-shot: the next tick at the same price does nothing
    again = run(w, desk, Quotes("102"), alerts=alerts)
    assert again["fired"] == [] and len(alerts) == 1
    assert w.conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0] \
        == 1


def test_not_hit_records_the_last_price_and_stays_active():
    w = World()
    desk = _desk(w.conn, w.repo)
    t = make(w, trigger_price=100, limit_price=100, stop=95, target=110)
    out = run(w, desk, Quotes("103"))
    assert out["fired"] == []
    row = status(w, t["id"])
    assert row["status"] == "active" and row["last_price"] == to_micro("103")
    assert row["last_checked_at"] == T0


def test_stale_quote_is_ignored():
    w = World()
    desk = _desk(w.conn, w.repo)
    t = make(w)
    out = run(w, desk, Quotes("90", stale=True))
    assert out["fired"] == [] and out["stale"] == [t["id"]]
    row = status(w, t["id"])
    assert row["status"] == "active" and "stale" in row["last_error"]
    assert row["last_price"] is None


def test_closed_market_is_not_evaluated_at_all():
    w = World()
    desk = _desk(w.conn, w.repo)
    t = make(w)
    q = Quotes("90")
    out = run(w, desk, q, clock=lambda: CLOSED)
    assert out["closed"] == 1 and out["fired"] == [] and q.calls == []
    assert status(w, t["id"])["status"] == "active"


def test_the_claim_is_atomic_one_engine_wins():
    """Two engines read the same active row; only the conditional UPDATE
    that matched fires — the other sees it is no longer active."""
    w = World()
    desk = _desk(w.conn, w.repo)
    t = make(w)
    snap = status(w, t["id"])                      # both read this
    q = Quotes("103").spark(["CSCO"])["CSCO"]
    kw = dict(resume_post=lambda b: (599, {}), clock=w.clock)
    first = pt.evaluate(w.conn, desk, None, snap, q, **kw)
    second = pt.evaluate(w.conn, desk, None, snap, q, **kw)
    assert first["result"] == "fired" and second["result"] == "lost_race"
    assert w.conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0] \
        == 1


def test_a_cancelled_trigger_never_fires():
    w = World()
    desk = _desk(w.conn, w.repo)
    t = make(w)
    pt.cancel(w.conn, t["id"], clock=w.clock)
    assert run(w, desk, Quotes("90"))["fired"] == []
    with pytest.raises(pt.TriggerError) as e:
        pt.cancel(w.conn, t["id"], clock=w.clock)
    assert e.value.code == 409


def test_expiry_even_while_the_market_is_closed():
    w = World()
    desk = _desk(w.conn, w.repo)
    t = make(w, expires_in_days=1)
    later = T0 + 2 * 86400 + 12 * 3600             # closed, and past expiry
    out = run(w, desk, Quotes("90"), clock=lambda: later)
    assert out["expired"] == [t["id"]]
    assert status(w, t["id"])["status"] == "expired"
    assert pt.get(w.conn, t["id"])["work_item_id"] is None


def test_default_expiry_is_thirty_days():
    w = World()
    t = make(w)
    assert t["expires_at"] - t["created_at"] == 30 * 86400


def test_one_bad_symbol_does_not_stop_the_others():
    w = World()
    w.repo.add_instrument("NASDAQ:AAPL", "AAPL", "NASDAQ", "USD")
    desk = _desk(w.conn, w.repo)
    bad = make(w, ticker="AAPL")
    good = make(w)
    out = run(w, desk, Quotes("103", broken={"AAPL"}))
    assert [f["id"] for f in out["fired"]] == [good["id"]]
    assert status(w, bad["id"])["status"] == "active"
    assert "no quote" in status(w, bad["id"])["last_error"]


def test_a_trade_already_in_flight_defers_instead_of_failing():
    w = World()
    desk = _desk(w.conn, w.repo)
    desk.manual_trade("CSCO", "NASDAQ", "buy", 103.0, stop=97.0,
                      background=False)
    t = make(w)
    out = run(w, desk, Quotes("103"))
    assert out["deferred"] and out["fired"] == []
    row = status(w, t["id"])
    assert row["status"] == "active" and row["fired_at"] is None
    assert "already" in row["last_error"]


# ── sells ────────────────────────────────────────────────────────────────
def test_exit_trigger_sells_shares_held_with_no_stop():
    w = World()
    w.buy_with_legs()                               # 9 CSCO on saxo-paper
    desk = _desk(w.conn, w.repo)
    t = make(w, direction="sell", condition="gte", trigger_price=110,
             qty=9, stop=None, target=None)
    assert t["kind"] == "exit"
    run(w, desk, Quotes("111"))
    row = status(w, t["id"])
    wi = desk.work_item(row["work_item_id"])
    th = json.loads(wi["thesis_json"])
    assert th["direction"] == "sell" and th["stop_loss"] is None
    assert "MANUAL EXIT" in th["conditions"][0]


def test_sell_trigger_fails_cleanly_when_the_shares_are_gone():
    w = World()
    w.buy_with_legs()
    desk = _desk(w.conn, w.repo)
    t = make(w, direction="sell", condition="gte", trigger_price=110,
             qty=9, stop=None, target=None)
    # a stop leg filled meanwhile and sold 6 of the 9
    w.conn.execute("UPDATE lots SET qty_remaining=? WHERE qty_remaining>0",
                   (to_micro("3"),))
    alerts = []
    out = run(w, desk, Quotes("111"), alerts=alerts)
    assert [f["id"] for f in out["failed"]] == [t["id"]]
    row = status(w, t["id"])
    assert row["status"] == "failed" and row["work_item_id"] is None
    assert "now holds 3" in row["status_reason"]
    assert "FAILED" in alerts[0]
    assert w.conn.execute("SELECT COUNT(*) FROM work_items WHERE"
                          " kind='manual'").fetchone()[0] == 0


# ── validation, up front ─────────────────────────────────────────────────
@pytest.mark.parametrize("kw, msg", [
    ({"stop": None}, "needs a stop"),
    ({"stop": 104}, "BELOW"),
    ({"target": 100}, "target must sit ABOVE"),
    ({"amount": 500}, "exactly one"),
    ({"qty": None}, "exactly one"),
    ({"limit_price": 140}, "more than 25%"),
    ({"condition": "lt"}, "condition"),
    ({"direction": "hold"}, "direction"),
    ({"broker": "nyse"}, "account"),
    ({"ticker": "<b>"}, "invalid ticker"),
    ({"exchange": "MARS"}, "trading hours"),
    ({"expires_in_days": 400}, "expiry"),
    ({"expires_in_days": 0}, "expiry"),
    ({"trail_pct": 95}, "trailing"),
    ({"note": "x" * 121}, "note"),
    ({"qty": -1}, "positive"),
    ({"qty": True}, "number"),
    ({"trigger_price": "abc"}, "number"),
    ({"mode": "yolo"}, "mode"),
    ({"ticker": "D05", "exchange": "SGX", "qty": 50}, "board lots"),
    ({"direction": "sell", "stop": None, "target": None}, "SHORT"),
    ({"direction": "sell", "stop": 99, "target": None}, "ABOVE"),
    ({"direction": "sell", "amount": 500, "qty": None}, "shares"),
])
def test_create_refuses_incoherent_rules(kw, msg):
    w = World()
    with pytest.raises(pt.TriggerError, match=msg):
        make(w, **kw)
    assert w.conn.execute("SELECT COUNT(*) FROM price_triggers"
                          ).fetchone()[0] == 0


def test_sell_rules_follow_what_the_account_holds():
    w = World()
    w.buy_with_legs()                               # 9 CSCO
    with pytest.raises(pt.TriggerError, match="holds only 9"):
        make(w, direction="sell", condition="gte", trigger_price=110,
             qty=10, stop=None, target=None)
    with pytest.raises(pt.TriggerError, match="no stop or target"):
        make(w, direction="sell", condition="gte", trigger_price=110,
             qty=9, stop=120, target=None)
    # held on ANOTHER account: refused rather than silently becoming a short
    w.repo.create_account("alpaca-paper", "alpaca", "paper", "USD",
                          fee_model={"type": "flat", "per_trade": "0"},
                          ts=T0)
    with pytest.raises(pt.TriggerError, match="another account"):
        make(w, direction="sell", condition="gte", trigger_price=110,
             qty=9, stop=120, target=None, broker="alpaca")
    short = None
    w2 = World()
    short = make(w2, direction="sell", condition="gte", trigger_price=110,
                 qty=5, stop=118, target=100)
    assert short["kind"] == "short"


def test_whole_share_accounts_refuse_fractions():
    w = World()
    with pytest.raises(pt.TriggerError, match="whole shares"):
        pt.create(w.conn, body(qty=2.5), created_by="t",
                  env={"SAXO_SIM_EXECUTION": "enabled"}, clock=w.clock)
    assert pt.create(w.conn, body(qty=2.5), created_by="t", env={},
                     clock=w.clock)["qty"] == 2.5


def test_amount_buy_and_default_limit():
    w = World()
    t = make(w, qty=None, amount=1000)
    assert t["amount_base"] == 1000.0 and t["qty"] is None
    assert t["limit_price"] == t["trigger_price"] == 103.5
    assert t["rule"] == ("Buy 1000.00 worth of CSCO when price ≤ 103.50 "
                         "(purchase price 103.50)")
    assert make(w, trigger_price=5, limit_price=5.01, stop=4.5, target=None,
                ticker="CSCO")["rule"] == ("Buy 5 CSCO when price ≤ 5.00 "
                                           "(purchase price 5.01)")


# ── auto mode: paper only ────────────────────────────────────────────────
def test_auto_is_refused_for_the_live_account():
    w = World(live=True)                    # account saxo-live / saxo_live
    with pytest.raises(pt.TriggerError) as e:
        make(w, broker="saxo_live", mode="auto")
    assert e.value.code == 403 and "paper accounts" in e.value.message
    # asking is fine on the live account: it only creates a card
    t = make(w, broker="saxo_live", mode="ask")
    assert t["mode"] == "ask"
    # and the table itself refuses an auto rule on saxo_live
    with pytest.raises(sqlite3.IntegrityError):
        w.conn.execute("UPDATE price_triggers SET mode='auto' WHERE id=?",
                       (t["id"],))


def test_no_shorting_triggers_on_the_live_account():
    w = World(live=True)
    with pytest.raises(pt.TriggerError, match="no shorting"):
        make(w, broker="saxo_live", direction="sell", condition="gte",
             trigger_price=110, stop=118, target=None)


def test_auto_rechecks_the_account_at_fire_time():
    """Belt and braces: if the account behind an auto rule is (somehow) no
    longer paper when it fires, the engine only creates the card."""
    w = World()
    desk = _desk(w.conn, w.repo)
    t = make(w, mode="auto")
    w.conn.execute("UPDATE broker_accounts SET environment='live'"
                   " WHERE id='saxo-paper'")
    posted, alerts = [], []
    run(w, desk, Quotes("103"), alerts=alerts,
        resume_post=lambda b: posted.append(b) or (200, {}))
    row = status(w, t["id"])
    assert posted == [] and row["outcome"].startswith("card (auto refused")
    assert desk.work_item(row["work_item_id"])["state"] == \
        "awaiting_approval"
    assert "NOT placed automatically" in alerts[0]


def _live_gate(w, desk):
    """claire-api's real /internal/resume, called in-process."""
    from fastapi.testclient import TestClient

    from app.api.main import create_app
    client = TestClient(create_app(desk, w.conn, "sec", clock=w.clock))

    def resume_post(b):
        r = client.post("/internal/resume", json=b,
                        headers={"x-claire-secret": "sec"})
        return r.status_code, r.json()
    return resume_post


def test_auto_mode_places_the_pre_approved_order_end_to_end():
    from app.wiring import make_prepare
    w = World()
    desk = _desk(w.conn, w.repo, execute=w.execute,
                 prepare=make_prepare(w.repo))
    t = make(w, mode="auto", trail_pct=6)
    alerts = []
    env = {"SAXO_SIM_EXECUTION": "enabled"}        # Saxo SIM: whole shares
    out = run(w, desk, Quotes("103"), alerts=alerts,
              resume_post=_live_gate(w, desk), env=env)
    assert out["fired"][0]["outcome"] == "placed", out
    row = status(w, t["id"])
    wi = row["work_item_id"]
    assert desk.work_item(wi)["state"] == "executing"
    order = w.conn.execute("SELECT * FROM orders WHERE work_item_id=? AND"
                           " leg IS NULL", (wi,)).fetchone()
    # exactly the 5 shares asked for, at the trigger's limit, bracketed
    assert (order["side"], order["qty"]) == ("buy", 5_000_000)
    assert order["limit_price"] == to_micro("103.5")
    assert {l["leg"] for l in w.legs()} == {"stop", "target"}
    # the audit trail: pre-approved by the human, when the rule was made,
    # then the ordinary token-checked authorization
    evs = [(e["actor"], e["to_state"], json.loads(e["payload"] or "{}"))
           for e in w.conn.execute("SELECT * FROM events WHERE item_id=?"
                                   " ORDER BY id", (wi,))]
    states = [s for _, s, _ in evs]
    assert states.index("pre_approved") < states.index("authorize:approve")
    pre = next(p for a, s, p in evs if s == "pre_approved")
    assert next(a for a, s, p in evs if s == "pre_approved") == "human"
    assert pre["pre_approved_by"] == "human (test)"
    assert pre["pre_approved_at"] == T0 and pre["trigger_id"] == t["id"]
    auth = next(p for a, s, p in evs if s == "authorize:approve")
    assert auth["broker"] == "saxo" and auth["trail_pct"] == 6
    assert "placed automatically" in alerts[0]


def test_auto_mode_still_obeys_execution_checks():
    """Pre-approval skips the click, not the rules: a cash shortfall is
    refused by thesis_action and the card stays for the human."""
    w = World()
    desk = _desk(w.conn, w.repo)
    t = make(w, mode="auto", qty=1000)              # ~103k vs 50k cash
    alerts = []
    run(w, desk, Quotes("103"), alerts=alerts,
        resume_post=lambda b: pytest.fail("must not resume"))
    row = status(w, t["id"])
    assert row["outcome"].startswith("refused: size")
    assert desk.work_item(row["work_item_id"])["state"] == \
        "awaiting_approval"
    assert "automatic placement refused" in alerts[0]


def test_size_for_shares_gives_back_exactly_the_shares():
    from app.graph.nodes_execution import FRACTION_STEP, position_qty
    for qty, quote, whole in ((5, "103", True), (50, "4.98", True),
                              (7, "0.1234", True), (3, "250.5", False)):
        size = pt.size_for_shares(Decimal(qty), Decimal(quote), Decimal(1),
                                  whole)
        got = position_qty(size, Decimal(quote), 1,
                           Decimal(1) if whole else FRACTION_STEP)
        assert got == qty, (qty, quote, size, got)


# ── schema & schedule ────────────────────────────────────────────────────
def test_table_is_created_idempotently_on_an_old_database():
    conn = db.connect(":memory:")
    db.init(conn)
    conn.execute("DROP TABLE price_triggers")      # a desk.db from before
    db.init(conn)
    db.init(conn)
    assert conn.execute("SELECT COUNT(*) FROM price_triggers").fetchone()[0] \
        == 0


def test_the_job_is_seeded_and_registered():
    spec = {j: s for j, _, _, s in scheduler.DEFAULTS}["price_triggers"]
    assert spec == {"type": "interval", "minutes": 2, "require_open": True}
    assert "price_triggers" in scheduler.JOB_INVOKES
    src = (ROOT / "app" / "api" / "server.py").read_text()
    assert '"price_triggers": run_price_triggers' in src


def test_a_disabled_schedule_does_not_run():
    conn = db.connect(":memory:")
    db.init(conn)
    scheduler.seed(conn)
    ran = []
    sched = scheduler.Scheduler(conn, {"price_triggers":
                                       lambda spec: ran.append(spec) or {}},
                                clock=lambda: T0)
    scheduler.update(conn, "price_triggers", enabled=False)
    sched.tick()
    assert ran == []
    scheduler.update(conn, "price_triggers", enabled=True)
    sched.tick()
    assert len(ran) == 1


# ── the dashboard API & page ─────────────────────────────────────────────
@pytest.fixture
def dash():
    from app.dashboards import create_server
    from tests.test_dashboards import DKEY, FakeMarket
    w = World(live=True)
    w.repo.create_account("saxo-paper", "saxo", "paper", "USD",
                          fee_model={"type": "flat", "per_trade": "0"},
                          ts=T0)
    srv = create_server(w.conn, w.repo, FakeMarket(), secret="s",
                        dash_key=DKEY, api_base="http://127.0.0.1:9",
                        clock=lambda: T0, port=0, sse_interval=None)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    yield base, w, DKEY
    srv.shutdown()


def test_trigger_routes_need_the_key(dash):
    base, w, key = dash
    with httpx.Client(base_url=base, timeout=5) as c:
        for h in ({}, {"X-Dash-Key": "wrong"}):
            assert c.post("/api/price-triggers", json=body(),
                          headers=h).status_code == 403
        t = c.post("/api/price-triggers", json=body(),
                   headers={"X-Dash-Key": key})
        assert t.status_code == 200, t.text
        tid = t.json()["id"]
        assert t.json()["created_by"].startswith("human via the paired")
        assert c.get("/api/price-triggers").json()[0]["id"] == tid
        for h in ({}, {"X-Dash-Key": "wrong"}):
            assert c.delete(f"/api/price-triggers?id={tid}",
                            headers=h).status_code == 403
        assert pt.get(w.conn, tid)["status"] == "active"
        r = c.delete(f"/api/price-triggers?id={tid}",
                     headers={"X-Dash-Key": key})
        assert r.status_code == 200 and r.json()["status"] == "cancelled"
        assert c.delete(f"/api/price-triggers?id={tid}",
                        headers={"X-Dash-Key": key}).status_code == 409
        # a cross-site DELETE is refused before the key is even looked at
        assert c.delete(f"/api/price-triggers?id={tid}", headers={
            "X-Dash-Key": key, "Origin": "http://evil.example"}
        ).status_code == 403


def test_api_refuses_auto_on_live_with_a_clear_message(dash):
    base, w, key = dash
    with httpx.Client(base_url=base, timeout=5,
                      headers={"X-Dash-Key": key}) as c:
        r = c.post("/api/price-triggers",
                   json=body(broker="saxo_live", mode="auto"))
        assert r.status_code == 403
        assert "only for paper accounts" in r.json()["error"]
        assert c.post("/api/price-triggers", json=body(
            broker="saxo", mode="auto")).json()["mode"] == "auto"
        bad = c.post("/api/price-triggers", json=body(stop=None))
        assert bad.status_code == 400 and "stop" in bad.json()["error"]


def test_watchlist_data_and_page(dash):
    base, w, key = dash
    w.conn.execute("INSERT INTO watchlist (ticker, exchange, note, added_at)"
                   " VALUES ('CSCO','NASDAQ','<img src=x>',?)", (T0,))
    pt.create(w.conn, body(broker="saxo"), created_by="t", clock=w.clock)
    with httpx.Client(base_url=base, timeout=5) as c:
        d = c.get("/api/watchlist-data").json()
        row = d["rows"][0]
        assert row["ticker"] == "CSCO" and row["quote"]["price"] == 101.0
        assert row["is_open"] is True and len(row["triggers"]) == 1
        assert {a["broker"]: a["auto_ok"] for a in d["accounts"]} == {
            "saxo_live": False, "saxo": True}
        page = c.get("/watchlist")
        assert page.status_code == 200 and "price-triggers" in page.text
    assert "['/watchlist', 'Watchlist']" in (ROOT / "web" /
                                             "nav.js").read_text()


def test_page_escapes_what_it_renders():
    import re
    src = (ROOT / "web" / "watchlist.html").read_text()
    # every inline handler argument goes through jsArg, never '${…}'
    assert not re.search(r"""on\w+="[^"]*\('\$\{""", src)
    # (a raw ticker appears only in confirm()/toast() text, never as HTML)
    for raw in ("${r.note}", "${r.name}", "${t.rule}",
                "${t.status_reason}", "${q.error}", "${e.message}",
                "${r.work_item_id}", "${t.work_item_id}"):
        assert raw not in src, raw
    # drawChart writes level labels raw, so the page escapes them first
    assert "label: esc(" in src


def test_routes_are_key_gated_in_source_and_not_override_prefixed():
    src = (ROOT / "app" / "dash" / "handler.py").read_text()
    i = src.index('if u.path == "/api/price-triggers":\n                #')
    assert "_paired()" in src[i:i + 400]
    j = src.index("def do_DELETE")
    assert "_paired()" in src[j:j + 600]
    for route in ("/api/price-triggers", "/api/watchlist-data"):
        assert not route.startswith("/api/override")


# ── editing (Orders page) ────────────────────────────────────────────────
def test_edit_replaces_the_trigger_atomically():
    w = World()
    t = make(w)
    new = pt.replace(w.conn, t["id"], body(trigger_price=100, qty=7),
                     created_by="human (test)", clock=w.clock)
    assert new["replaces"] == t["id"] and new["status"] == "active"
    assert new["trigger_price"] == 100.0 and new["qty"] == 7.0
    old = status(w, t["id"])
    assert old["status"] == "cancelled"
    assert f"replaced by #{new['id']}" in old["status_reason"]


def test_a_refused_edit_changes_nothing():
    w = World()
    t = make(w)
    with pytest.raises(pt.TriggerError):            # buy stop above limit
        pt.replace(w.conn, t["id"], body(stop=120), created_by="human",
                   clock=w.clock)
    assert status(w, t["id"])["status"] == "active"
    (n,) = w.conn.execute("SELECT COUNT(*) FROM price_triggers").fetchone()
    assert n == 1


def test_a_fired_trigger_cannot_be_edited_back_to_life():
    w = World()
    t = make(w)
    w.conn.execute("UPDATE price_triggers SET status='fired' WHERE id=?",
                   (t["id"],))
    with pytest.raises(pt.TriggerError) as e:
        pt.replace(w.conn, t["id"], body(), created_by="human",
                   clock=w.clock)
    assert e.value.code == 409
    (n,) = w.conn.execute("SELECT COUNT(*) FROM price_triggers").fetchone()
    assert n == 1
