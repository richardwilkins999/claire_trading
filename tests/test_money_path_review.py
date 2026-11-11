"""Regression tests for the money/execution items of REVIEW-2026-11-03.md
(H5-H8, M3-M9, M13 caller, and the Low items in the money path). Each test
names the item it pins down. Offline: paper sim broker, fake market."""
import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.sqlite import SqliteSaver

from app.accounting import db
from app.accounting.money import to_micro
from app.accounting.repo import (InsufficientCash, LedgerError, Repo,
                                 RiskLimitExceeded)
from app.custodian import Custodian
from app.graph.nodes_execution import (PlacementUnknown, build_execute,
                                       submit_order)
from app.graph.pipeline import build_override_pipeline, build_pipeline, vet_thesis
from app.graph.state import Approval, Instrument, PipelineState, Thesis
from app.tools.brokers import PaperSimBroker, PlacedOrder
from app.watcher import Watcher
from tests.test_execution import NVDA, OPEN_TS, SAT_TS, World
from tests.test_graph import CFG, Script, init_state

T0 = OPEN_TS
SGX_C07 = Instrument(id="SGX:C07", ticker="C07", exchange="SGX",
                     currency="SGD", lot_size=100)


def _thesis(**kw):
    base = dict(ticker="NVDA", direction="buy", conviction=0.7,
                entry_low=99.0, entry_high=101.0, stop_loss=95.0,
                take_profit=120.0, currency="USD")
    base.update(kw)
    return Thesis(**base)


def _state(inst=NVDA, thesis=None, wi="wi_t"):
    return PipelineState(work_item_id=wi, ticker=inst.ticker,
                         instrument=inst, thesis=thesis)


# ── H5: the thesis is pinned to the run and its levels are sane ─────────────
def test_h5_thesis_levels_must_be_coherent():
    with pytest.raises(ValueError, match="entry_low must not exceed"):
        _thesis(entry_low=105.0, entry_high=101.0)
    with pytest.raises(ValueError, match="take-profit must be ABOVE"):
        _thesis(take_profit=90.0)
    with pytest.raises(ValueError, match="take-profit must be BELOW"):
        _thesis(direction="sell", stop_loss=110.0, take_profit=105.0)
    _thesis(direction="sell", stop_loss=110.0, take_profit=90.0)   # coherent


def test_h5_wrong_ticker_or_currency_downgrades_to_pass():
    th = vet_thesis(_state(SGX_C07), _thesis(ticker="AAPL", currency="USD"))
    assert th.direction == "pass"
    assert (th.ticker, th.currency) == ("C07", "SGD")      # pinned to the run
    assert "AAPL" in th.conditions[-1] and "SGX:C07" in th.conditions[-1]
    th = vet_thesis(_state(), _thesis(currency="GBp"))
    assert th.direction == "pass" and "GBp" in th.conditions[-1]


def test_h5_formatting_is_not_a_different_stock():
    th = vet_thesis(_state(SGX_C07), _thesis(ticker="C07.SI", currency="SGD",
                                             entry_low=3.4, entry_high=3.5,
                                             stop_loss=3.2, take_profit=4.0))
    assert th.direction == "buy" and th.ticker == "C07"


def test_h5_entry_outside_price_band_downgrades_to_pass():
    th = vet_thesis(_state(), _thesis(entry_low=170.0, entry_high=175.0,
                                      stop_loss=160.0, take_profit=200.0),
                    last_price=lambda inst: Decimal("100"))
    assert th.direction == "pass"
    assert "from the last price" in th.conditions[-1]
    ok = vet_thesis(_state(), _thesis(), last_price=lambda i: Decimal("100"))
    assert ok.direction == "buy"


def test_h5_mismatched_thesis_never_reaches_the_gate(saver=None):
    s = Script()
    s.deps.arbiter = lambda state: _thesis(ticker="AAPL")
    pipe = build_pipeline(s.deps, SqliteSaver(
        sqlite3.connect(":memory:", check_same_thread=False)))
    out = pipe.invoke(init_state(), CFG)
    assert out["thesis"].direction == "pass"
    assert out["thesis"].ticker == "NVDA"
    assert "execute" not in s.calls and out.get("approval") is None


def test_h5_execute_refuses_a_limit_far_from_the_live_price():
    w = World(price="100")
    st = w.state()
    st = st.model_copy(update={"thesis": _thesis(
        entry_low=170.0, entry_high=175.0, stop_loss=160.0,
        take_profit=200.0)})
    with pytest.raises(ValueError, match="band"):
        w.execute(st)
    assert w.broker.orders == {}


# ── H6: a failed FX lookup stops the booking, never uses 1.0 ────────────────
class FxDown:
    def fx(self, a, b):
        raise ConnectionError("fx source down")


def _jpy_world(fx_rate_for, fill_mode="manual"):
    conn = db.connect(":memory:")
    db.init(conn)
    repo = Repo(conn)
    repo.create_account("acct", "alpaca", "paper", "USD",
                        fee_model={"type": "flat", "per_trade": "0"}, ts=T0)
    repo.deposit("acct", to_micro("50000"), ts=T0)
    repo.add_instrument("TSE:7203", "7203", "TSE", "JPY")
    broker = PaperSimBroker(fill_mode=fill_mode)
    c = Custodian(conn, repo, {"alpaca": broker},
                  resume_post=lambda b: (200, {}),
                  account_broker=lambda a: "alpaca",
                  fx_rate_for=fx_rate_for, clock=lambda: T0 + 60)
    return conn, repo, broker, c


def test_h6_server_fx_rate_for_raises_instead_of_one():
    from app.api.server import FxUnavailable, make_fx_rate_for
    conn, repo, _, _ = _jpy_world(lambda i, a: 0)
    repo.add_instrument("NASDAQ:NVDA", "NVDA", "NASDAQ", "USD")
    fx_for = make_fx_rate_for(conn, FxDown())
    with pytest.raises(FxUnavailable):
        fx_for("TSE:7203", "acct")
    assert fx_for("NASDAQ:NVDA", "acct") == to_micro("1")   # no lookup needed


def test_h6_custodian_leaves_fill_unbooked_and_books_it_later():
    rate = {"ok": False}

    def fx_for(i, a):
        if not rate["ok"]:
            raise RuntimeError("no JPY→USD rate")
        return to_micro("0.0067")
    conn, repo, broker, c = _jpy_world(fx_for)
    p = broker.place_bracket(symbol="7203.T", side="buy", qty=100, limit=3000)
    repo.create_order(id="o1", account_id="acct", instrument_id="TSE:7203",
                      side="buy", qty=to_micro("100"), ts=T0,
                      expires_at=T0 + 86400, broker_order_id=p.broker_order_id)
    broker.simulate_fill(p.broker_order_id, 100, "3000")
    r = c.run_once()
    assert r["fills"] == 0
    assert any("FILL NOT BOOKED" in f for f in r["flags"])
    assert repo.cash_balance("acct") == to_micro("50000")   # not $300,000 gone
    rate["ok"] = True
    assert c.run_once()["fills"] == 1
    assert repo.cash_balance("acct") == to_micro("50000") - to_micro("2010")


def test_h6_portfolio_marks_value_unavailable_without_fx():
    from app import portfolio
    conn, repo, _, _ = _jpy_world(lambda i, a: 0)
    repo.create_order(id="o1", account_id="acct", instrument_id="TSE:7203",
                      side="buy", qty=to_micro("100"), ts=T0,
                      expires_at=T0 + 86400)
    repo.record_fill("o1", broker_fill_id="f1", qty=to_micro("100"),
                     price_native=to_micro("3000"),
                     fx_rate=to_micro("0.0067"), ts=T0)

    class M(FxDown):
        def metrics(self, syms):
            return {s: {"price": 3100, "currency": "JPY"} for s in syms}
    d = portfolio.build(conn, M(), clock=lambda: T0)
    p = d["exchanges"]["TSE"][0]
    assert p["fx_unavailable"] is True
    assert p["value_base"] is None and p["unrealized_base"] is None


# ── H7: caps are measured in base currency; market orders are measured ──────
def _capped(repo, limits, ccy_inst=("TSE:7203", "7203", "TSE", "JPY")):
    repo.create_account("capped", "alpaca", "paper", "USD",
                        fee_model={"type": "flat", "per_trade": "0"},
                        risk_limits=limits, ts=T0)
    repo.deposit("capped", to_micro("100000"), ts=T0)
    repo.add_instrument(*ccy_inst)


def test_h7_jpy_order_is_converted_before_the_cap():
    conn = db.connect(":memory:")
    db.init(conn)
    repo = Repo(conn)
    _capped(repo, {"max_order_base": 25000})
    # 100 × ¥3000 ≈ $2,010 — used to be refused as "300,000"
    repo.check_risk("capped", "TSE:7203", "buy", to_micro("100"),
                    to_micro("3000"), T0, fx_rate=to_micro("0.0067"))
    with pytest.raises(RiskLimitExceeded, match="max_order_base"):
        repo.check_risk("capped", "TSE:7203", "buy", to_micro("2000"),
                        to_micro("3000"), T0, fx_rate=to_micro("0.0067"))
    # foreign currency and no rate: refused, not assumed 1.0
    with pytest.raises(RiskLimitExceeded, match="FX rate"):
        repo.check_risk("capped", "TSE:7203", "buy", to_micro("1"),
                        to_micro("3000"), T0)


def test_h7_max_position_pct_in_base_currency():
    conn = db.connect(":memory:")
    db.init(conn)
    repo = Repo(conn)
    _capped(repo, {"max_position_pct": 20},
            ("HKEX:0700", "0700", "HKEX", "HKD"))
    # HK$100,000 ≈ US$12,800 = 12.8% of $100k equity → fine once converted
    repo.check_risk("capped", "HKEX:0700", "buy", to_micro("250"),
                    to_micro("400"), T0, fx_rate=to_micro("0.128"))
    with pytest.raises(RiskLimitExceeded, match="max_position_pct"):
        repo.check_risk("capped", "HKEX:0700", "buy", to_micro("500"),
                        to_micro("400"), T0, fx_rate=to_micro("0.128"))


def test_h7_market_order_uses_reference_price():
    conn = db.connect(":memory:")
    db.init(conn)
    repo = Repo(conn)
    _capped(repo, {"max_order_base": 1000},
            ("NASDAQ:NVDA", "NVDA", "NASDAQ", "USD"))
    with pytest.raises(RiskLimitExceeded, match="max_order_base"):
        repo.check_risk("capped", "NASDAQ:NVDA", "sell", to_micro("100"),
                        None, T0, ref_price=to_micro("100"))
    with pytest.raises(RiskLimitExceeded, match="no reference price"):
        repo.check_risk("capped", "NASDAQ:NVDA", "sell", to_micro("1"),
                        None, T0)


def test_h7_execute_exit_is_not_trapped_by_the_cap():
    """Orders are still measured (tests above); but selling what the
    desk holds is an exit, and a size cap must never trap the desk in a
    position. Before exits were exempt, this ~$1000 sell under a $500 cap
    was refused — leaving the position unsellable through the desk."""
    w = World(fill_mode="instant")
    w.execute(w.state())                        # buy 10
    w.custodian.run_once()
    w.conn.execute("UPDATE broker_accounts SET risk_limits=?",
                   (json.dumps({"max_order_base": 500}),))
    w.execute(w.state(wi="wi_s", direction="sell"))   # ~$1000 exit > $500
    assert len(w.broker.orders) == 2            # the venue saw the sell


# ── H8: custodian isolation, reserved cash, restarts, expiry ────────────────
def test_h8_second_approval_cannot_spend_reserved_cash():
    w = World(fill_mode="instant")              # balance 50k, fills instantly
    w.execute(w.state(wi="wi_a", size=30000.0))
    with pytest.raises(InsufficientCash, match="committed to open orders"):
        w.execute(w.state(wi="wi_b", size=30000.0))
    assert len(w.broker.orders) == 1            # the venue saw one order
    r = w.custodian.run_once()
    assert r["fills"] == 1 and not any("NOT BOOKED" in f for f in r["flags"])
    w.repo.assert_invariants("acct")


def test_h8_unknown_order_after_restart_is_flagged_not_fatal():
    w = World()
    w.execute(w.state())
    w.conn.execute(
        "INSERT INTO work_items (id, kind, ticker, state, thread_id,"
        " approval_token, token_state, expires_at, created_at, updated_at)"
        " VALUES ('wi_old','pipeline','NVDA','awaiting_approval','wi_old',"
        " 'tok','minted',?,?,?)", (T0 - 10, T0 - 100, T0 - 100))
    w.custodian.brokers["alpaca"] = PaperSimBroker()   # claire-api restarted
    r = w.custodian.run_once()
    assert any("RECONCILE" in f and "does not know" in f for f in r["flags"])
    assert r["approvals_expired"] == ["wi_old"]  # other duties still ran
    assert w.repo.order(w.repo.open_orders()[0]["id"])["status"] == "placed"


def test_h8_one_bad_order_does_not_stop_the_others():
    w = World()
    (o1,) = w.execute(w.state(wi="wi_1"))["order_ids"]
    (o2,) = w.execute(w.state(wi="wi_2"))["order_ids"]
    b1 = w.repo.order(o1)["broker_order_id"]
    b2 = w.repo.order(o2)["broker_order_id"]
    w.broker.simulate_fill(b2, 10, "100")
    real = w.broker.order_status

    def flaky(boid):
        if boid == b1:
            raise RuntimeError("venue hiccup")
        return real(boid)
    w.broker.order_status = flaky
    r = w.custodian.run_once()
    assert r["fills"] == 1
    assert w.repo.order(o2)["status"] == "filled"
    assert any(o1 in f and "poll error" in f for f in r["flags"])


def test_h8_timer_custodian_never_expires_a_live_order_it_cannot_cancel():
    w = World()
    (oid,) = w.execute(w.state())["order_ids"]
    o = w.repo.order(oid)
    timer = Custodian(w.conn, w.repo, {}, resume_post=w._resume,
                      account_broker=lambda a: "alpaca",
                      fx_rate_for=lambda i, a: to_micro("1"),
                      clock=lambda: o["expires_at"] + 60)
    r = timer.run_once()
    assert oid not in r["order_expired"]
    assert w.repo.order(oid)["status"] == "placed"
    assert any("no adapter to cancel" in f for f in r["flags"])
    # the in-process custodian cancels at the venue, then expires
    w.t["now"] = o["expires_at"] + 60
    assert oid in w.custodian.run_once()["order_expired"]
    assert w.broker.order_status(o["broker_order_id"])["status"] == "cancelled"


# ── M3: split keeps closures in step ────────────────────────────────────────
def test_m3_split_after_partial_sale_holds_invariants():
    conn = db.connect(":memory:")
    db.init(conn)
    repo = Repo(conn)
    repo.create_account("acct", "alpaca", "paper", "USD",
                        fee_model={"type": "flat", "per_trade": "0"}, ts=T0)
    repo.add_instrument("NASDAQ:NVDA", "NVDA", "NASDAQ", "USD")
    repo.deposit("acct", to_micro("100000"), ts=T0)
    for oid, side, qty in (("b", "buy", "100"), ("s", "sell", "40")):
        repo.create_order(id=oid, account_id="acct", instrument_id="NASDAQ:NVDA",
                          side=side, qty=to_micro(qty), expires_at=T0 + 999,
                          ts=T0)
        repo.record_fill(oid, broker_fill_id=f"f{oid}", qty=to_micro(qty),
                         price_native=to_micro("100"), fx_rate=to_micro("1"),
                         ts=T0)
    pl_before = repo.realized_pl("acct")
    repo.apply_split("NASDAQ:NVDA", 2, ex_date=T0 + 1, ts=T0 + 1)
    repo.assert_invariants("acct")
    lot = conn.execute("SELECT * FROM lots").fetchone()
    assert (lot["qty_opened"], lot["qty_remaining"]) == (to_micro("200"),
                                                         to_micro("120"))
    (closed,) = conn.execute("SELECT SUM(qty) FROM lot_closures").fetchone()
    assert closed == to_micro("80")
    assert repo.realized_pl("acct") == pl_before    # money doesn't move


# ── M4: the order row exists before the venue call; no double placement ─────
class Exploding(PaperSimBroker):
    """Asserts the book already holds the order, then dies mid-call."""

    def __init__(self, conn):
        super().__init__(fill_mode="manual")
        self.conn, self.calls = conn, 0

    def place_bracket(self, **kw):
        self.calls += 1
        (n,) = self.conn.execute(
            "SELECT COUNT(*) FROM orders WHERE submitted_at IS NOT NULL"
        ).fetchone()
        assert n == 1, "venue called before the order row was persisted"
        raise ConnectionError("connection reset")


def test_m4_row_persisted_before_venue_and_never_replaced_blindly():
    w = World()
    venue = Exploding(w.conn)
    execute = build_execute(w.repo, {"alpaca": venue}, w.market,
                            account_for=lambda b: "acct", clock=w.clock)
    with pytest.raises(ConnectionError):
        execute(w.state())
    (o,) = w.conn.execute("SELECT * FROM orders").fetchall()
    assert o["status"] == "pending_session" and o["submitted_at"] == T0
    assert o["client_order_id"] == o["id"]
    # a retry of the same approval does not place a second order …
    with pytest.raises(PlacementUnknown):
        execute(w.state())
    # … nor does the custodian; it flags the order for reconciliation
    c = Custodian(w.conn, w.repo, {"alpaca": venue}, resume_post=w._resume,
                  account_broker=lambda a: "alpaca",
                  fx_rate_for=lambda i, a: to_micro("1"),
                  clock=lambda: T0 + 600)
    r = c.run_once()
    assert venue.calls == 1
    assert any("RECONCILE" in f and o["id"] in f for f in r["flags"])
    # and past its TTL it is not marked expired: the venue may hold it
    c.clock = lambda: o["expires_at"] + 60
    c.run_once()
    assert w.repo.order(o["id"])["status"] == "pending_session"


def test_m4_execute_retry_is_idempotent():
    w = World()
    (a,) = w.execute(w.state())["order_ids"]
    (b,) = w.execute(w.state())["order_ids"]
    assert a == b and len(w.broker.orders) == 1


def test_m4_client_order_id_handed_to_venues_that_take_one():
    seen = {}

    class WithClientId(PaperSimBroker):
        def place_bracket(self, *, client_order_id=None, **kw):
            seen["cid"] = client_order_id
            return super().place_bracket(**kw)
    w = World()
    execute = build_execute(w.repo, {"alpaca": WithClientId()}, w.market,
                            account_for=lambda b: "acct", clock=w.clock)
    (oid,) = execute(w.state())["order_ids"]
    assert seen["cid"] == oid


def test_m4_only_one_placer_wins_the_claim():
    w = World(ts=SAT_TS)
    (oid,) = w.execute(w.state())["order_ids"]  # queued: market closed
    first = submit_order(w.repo, w.broker, oid, symbol="NVDA", ts=T0)
    second = submit_order(w.repo, w.broker, oid, symbol="NVDA", ts=T0)
    assert first.status == "accepted" and second is None
    assert len(w.broker.orders) == 1


# ── M5: a failed execution is recoverable; stale approvals expire ───────────
SECRET = "s3cret"


@pytest.fixture
def api_world():
    conn = db.connect(":memory:")
    db.init(conn)
    repo = Repo(conn)
    repo.create_account("alpaca-paper", "alpaca", "paper", "USD",
                        fee_model={"type": "flat", "per_trade": "0"}, ts=T0)
    repo.deposit("alpaca-paper", to_micro("10000"), ts=T0)
    script = Script()
    fail = {"n": 1}

    def execute(state):
        script.calls.append("execute")
        script.theses = getattr(script, "theses", []) + [
            (state.thesis.entry_high, state.thesis.stop_loss,
             state.thesis.take_profit)]
        if fail["n"]:
            fail["n"] -= 1
            raise RuntimeError("venue down")
        return {"order_ids": ["ord_1"]}
    script.deps.execute = execute
    from app.api.desk import Desk
    from app.api.main import create_app
    desk = Desk(conn, script.deps, SqliteSaver(
        sqlite3.connect(":memory:", check_same_thread=False)), repo,
        clock=lambda: T0)
    client = TestClient(create_app(desk, conn, SECRET, clock=lambda: T0))

    def resume_post(body):
        r = client.post("/internal/resume", json=body,
                        headers={"x-claire-secret": SECRET})
        return r.status_code, r.json()
    wi = desk.start_run(NVDA, background=False)
    return conn, repo, script, resume_post, wi


def _row(conn, wi):
    return conn.execute("SELECT * FROM work_items WHERE id=?", (wi,)).fetchone()


def test_m5_failed_execution_keeps_the_approval_retryable(api_world):
    from app.approvals import AuthError, retry_pending, thesis_action
    conn, repo, script, resume_post, wi = api_world
    with pytest.raises(AuthError) as e:
        thesis_action(conn, {"work_item_id": wi, "action": "approve",
                             "size_base": 1000.0, "broker": "alpaca",
                             "token": _row(conn, wi)["approval_token"]},
                      resume_post, clock=lambda: T0)
    assert e.value.code == 500 and "venue down" in e.value.message
    r = _row(conn, wi)
    assert (r["state"], r["token_state"]) == ("approved", "pending_resume")
    assert conn.execute("SELECT COUNT(*) FROM events WHERE item_id=? AND"
                        " to_state='resume_failed'", (wi,)).fetchone()[0] == 1
    assert retry_pending(conn, resume_post, clock=lambda: T0) == [wi]
    r = _row(conn, wi)
    assert (r["state"], r["token_state"]) == ("executing", "burned")
    assert script.calls.count("execute") == 2


def test_m5_approved_without_order_expires_after_an_hour(api_world):
    conn, repo, script, resume_post, wi = api_world
    tok = _row(conn, wi)["approval_token"]
    code, _ = resume_post({"work_item_id": wi, "status": "approved",
                           "actor": "human", "token": tok,
                           "size_base": 1000, "broker": "alpaca"})
    assert code == 500
    c = Custodian(conn, repo, {}, resume_post=lambda b: (500, {}),
                  account_broker=lambda a: "alpaca",
                  fx_rate_for=lambda i, a: to_micro("1"),
                  clock=lambda: T0 + 3601)
    assert wi in c.run_once()["approvals_expired"]
    r = _row(conn, wi)
    assert (r["state"], r["token_state"]) == ("expired", "burned")
    code, _ = resume_post({"work_item_id": wi, "status": "approved",
                           "actor": "human", "token": tok,
                           "size_base": 1000, "broker": "alpaca"})
    assert code == 409                          # an expired approval stays dead
    assert script.calls.count("execute") == 1


def test_low_token_claim_is_atomic(api_world):
    """A resume in flight holds the token; a concurrent one is refused even
    though every field it reads still says awaiting_approval."""
    conn, repo, script, resume_post, wi = api_world
    tok = _row(conn, wi)["approval_token"]
    conn.execute("UPDATE work_items SET token_state='resuming', updated_at=?"
                 " WHERE id=?", (T0, wi))
    code, body = resume_post({"work_item_id": wi, "status": "rejected",
                              "actor": "human", "token": tok})
    assert code == 409 and "another resume" in body["detail"]
    code, _ = resume_post({"work_item_id": wi, "status": "approved",
                           "actor": "human", "token": tok[:-1],
                           "size_base": 1, "broker": "alpaca"})
    assert code == 403                          # wrong length: still constant-time


def test_low_dashboard_authorize_is_conditional(api_world):
    from app.approvals import AuthError, thesis_action
    conn, repo, script, resume_post, wi = api_world
    tok = _row(conn, wi)["approval_token"]
    conn.execute("UPDATE work_items SET token_state='resuming' WHERE id=?",
                 (wi,))
    with pytest.raises(AuthError) as e:
        thesis_action(conn, {"work_item_id": wi, "action": "reject",
                             "token": tok}, resume_post, clock=lambda: T0)
    assert e.value.code == 409
    assert "execute" not in script.calls


# ── M6: a rejected queued order moves its item out of executing ─────────────
class Refuses(PaperSimBroker):
    def place_bracket(self, **kw):
        return PlacedOrder("", "rejected", "symbol halted")


def test_m6_rejected_pending_session_order_fails_its_item():
    w = World(ts=SAT_TS)
    (oid,) = w.execute(w.state())["order_ids"]
    w.custodian.brokers["alpaca"] = Refuses()
    w.t["now"] = 1_786_368_600                  # Monday, open
    r = w.custodian.run_once()
    assert w.repo.order(oid)["status"] == "rejected"
    assert any("symbol halted" in f for f in r["flags"])
    st = w.conn.execute("SELECT state FROM work_items WHERE id='wi_x'"
                        ).fetchone()["state"]
    assert st == "failed"


# ── M7: one trailing rule per position, fresh peak per position ─────────────
def _trail_rows(conn):
    return conn.execute("SELECT * FROM price_alerts WHERE rule='trail_pct'"
                        " ORDER BY id").fetchall()


def test_m7_repeat_approvals_keep_one_rule():
    w = World()
    for i, pct in enumerate((5.0, 7.0)):
        st = w.state(wi=f"wi_{i}")
        st = st.model_copy(update={"approval": st.approval.model_copy(
            update={"trail_pct": pct})})
        w.execute(st)
    rows = _trail_rows(w.conn)
    assert len(rows) == 1
    assert (rows[0]["threshold"], rows[0]["account_id"], rows[0]["side"]) == \
        (7.0, "acct", 1)
    with pytest.raises(sqlite3.IntegrityError):
        w.conn.execute("INSERT INTO price_alerts (instrument_id, account_id,"
                       " side, rule, threshold) VALUES"
                       " ('NASDAQ:NVDA','acct',1,'trail_pct',9)")


def test_m7_migration_dedupes_existing_rows(tmp_path):
    path = tmp_path / "desk.db"
    conn = db.connect(path)
    db.init(conn)
    conn.execute("DROP INDEX idx_alerts_trail_unique")
    for thr in (8, 5, 6):
        conn.execute("INSERT INTO price_alerts (instrument_id, rule,"
                     " threshold) VALUES ('NASDAQ:NVDA','trail_pct',?)", (thr,))
    # a database from before this migration existed: it has not had it, and
    # says so (migrations now run once per database — PRAGMA user_version)
    conn.execute("PRAGMA user_version = 0")
    db.init(conn)                               # re-runs the migrations
    rows = _trail_rows(conn)
    assert len(rows) == 1 and rows[0]["threshold"] == 6   # newest kept


def test_m7_rebought_position_starts_a_fresh_peak():
    w = World(fill_mode="instant")
    w.execute(w.state(wi="wi_1"))
    w.custodian.run_once()
    watcher = Watcher(w.conn, w.repo, w.market, lambda inst, trigger=None: "x",
                      clock=w.clock)
    w.market.price = Decimal("150")
    watcher.tick()
    assert _trail_rows(w.conn)[0]["peak_base"] == 150.0
    # sell out at 150, then buy back near 100
    st = w.state(wi="wi_2", direction="sell")
    st = st.model_copy(update={"thesis": Thesis(
        ticker="NVDA", direction="sell", conviction=0.6, currency="USD",
        entry_low=148.0, entry_high=150.0)})
    w.execute(st)
    w.custodian.run_once()
    assert w.repo.position("acct", "NASDAQ:NVDA")[0] == 0
    watcher.tick()                              # flat → alert removed
    assert _trail_rows(w.conn) == []
    w.market.price = Decimal("100")
    w.execute(w.state(wi="wi_3"))
    w.custodian.run_once()
    r = watcher.tick()
    assert r["fired"] == []                     # old 150 peak would have fired
    assert _trail_rows(w.conn)[0]["peak_base"] == pytest.approx(101.0)


def test_m7_rules_are_keyed_by_account_and_side():
    w = World(fill_mode="instant")
    w.repo.create_account("acct2", "saxo", "paper", "USD",
                          fee_model={"type": "flat", "per_trade": "0"}, ts=T0)
    w.repo.deposit("acct2", to_micro("50000"), ts=T0)
    w.execute(w.state(wi="wi_1"))
    ex2 = build_execute(w.repo, {"alpaca": w.broker}, w.market,
                        account_for=lambda b: "acct2", clock=w.clock)
    ex2(w.state(wi="wi_2"))
    w.custodian.run_once()
    Watcher(w.conn, w.repo, w.market, lambda inst, trigger=None: "x",
            clock=w.clock).tick()
    keys = {(r["account_id"], r["side"]) for r in _trail_rows(w.conn)}
    assert keys == {("acct", 1), ("acct2", 1)}


# ── M8: shorts on the portfolio page ────────────────────────────────────────
def test_m8_short_position_valued_correctly():
    from app import portfolio
    w = World()
    w.repo.create_order(id="s1", account_id="acct", instrument_id="NASDAQ:NVDA",
                        side="sell", qty=to_micro("10"), expires_at=T0 + 999,
                        ts=T0)
    w.repo.record_fill("s1", broker_fill_id="f1", qty=to_micro("10"),
                       price_native=to_micro("100"), fx_rate=to_micro("1"),
                       ts=T0)

    class M:
        def metrics(self, syms):
            return {s: {"price": 90, "currency": "USD"} for s in syms}

        def fx(self, a, b):
            return Decimal(1)
    d = portfolio.build(w.conn, M(), clock=lambda: T0)
    p = d["exchanges"]["NASDAQ"][0]
    assert p["side"] == "short" and p["qty"] == -10
    assert p["avg_cost_base"] == 100
    assert p["value_base"] == -900
    assert p["unrealized_base"] == 100          # sold at 100, now 90: a gain
    assert p["unrealized_pct"] == 10
    acct = d["accounts"][0]
    assert acct["equity"] == 50000 + 1000 - 900  # cash incl. proceeds − buyback


# ── M9: overrides go through prepare (board lots) ───────────────────────────
def test_m9_override_graph_runs_prepare():
    from app.wiring import make_prepare
    conn = db.connect(":memory:")
    db.init(conn)
    repo = Repo(conn)
    s = Script()
    s.deps.prepare = make_prepare(repo)
    g = build_override_pipeline(s.deps, SqliteSaver(
        sqlite3.connect(":memory:", check_same_thread=False)))
    inst = Instrument(id="SGX:C07", ticker="C07", exchange="SGX",
                      currency="SGD")                 # lot_size defaults to 1
    th = _thesis(ticker="C07", currency="SGD", entry_low=3.5, entry_high=3.5,
                 stop_loss=3.2, take_profit=4.0)
    g.invoke(PipelineState(work_item_id="wi_test", kind="override",
                           ticker="C07", instrument=inst, thesis=th), CFG)
    assert g.get_state(CFG).values["instrument"].lot_size == 100


# ── M13: zero screener picks start nothing ──────────────────────────────────
def test_m13_zero_picks_start_no_runs(monkeypatch):
    from app import autonomous, screener
    from app.api.server import choose_picks
    fallback = []
    monkeypatch.setattr(autonomous, "pick_candidates",
                        lambda *a, **k: fallback.append(1) or [("X", "SGX")])
    monkeypatch.setattr(screener, "screener_pick", lambda *a, **k: [])
    picker, picks = choose_picks(None, None, ["SGX"], set())
    assert picks == [] and fallback == [] and "no picks" in picker

    def boom(*a, **k):
        raise screener.ScreenerError("provider down")
    monkeypatch.setattr(screener, "screener_pick", boom)
    picker, picks = choose_picks(None, None, ["SGX"], set())
    assert picks == [("X", "SGX", "top of book")]
    assert picker.startswith("deterministic fallback")


# ── Low: record_fill idempotency; daily trade cap ───────────────────────────
def test_low_record_fill_replay_inside_the_lock_is_a_noop():
    w = World()
    w.repo.create_order(id="o1", account_id="acct", instrument_id="NASDAQ:NVDA",
                        side="buy", qty=to_micro("10"), expires_at=T0 + 999,
                        ts=T0)
    kw = dict(broker_fill_id="f1", qty=to_micro("5"),
              price_native=to_micro("100"), fx_rate=to_micro("1"), ts=T0,
              commission=to_micro("1"))
    a = w.repo.record_fill("o1", **kw)
    b = w.repo.record_fill("o1", **kw)
    assert a == b
    assert w.conn.execute("SELECT COUNT(*) FROM cash_transactions WHERE"
                          " kind='commission'").fetchone()[0] == 1
    w.repo.assert_invariants("acct")


def test_low_trade_cap_counts_queued_orders_on_the_venue_day():
    conn = db.connect(":memory:")
    db.init(conn)
    from app import sessions
    sessions.seed_db(conn)
    repo = Repo(conn)
    _capped(repo, {"max_trades_per_day": 1}, ("SGX:C07", "C07", "SGX", "SGD"))
    # 07:30 SGT Tue 11 Aug = 23:30 UTC Mon 10 Aug: a different UTC day from
    # 10:00 SGT, but the same SGX trading day
    early = int(datetime(2026, 8, 10, 23, 30, tzinfo=timezone.utc).timestamp())
    later = int(datetime(2026, 8, 11, 2, 0, tzinfo=timezone.utc).timestamp())
    repo.create_order(id="q1", account_id="capped", instrument_id="SGX:C07",
                      side="sell", qty=to_micro("100"), status="pending_session",
                      limit_price=to_micro("3"), expires_at=later + 9999,
                      ts=early)
    with pytest.raises(RiskLimitExceeded, match="max_trades_per_day"):
        repo.check_risk("capped", "SGX:C07", "sell", to_micro("100"),
                        to_micro("3"), later)
    # the next SGX day is free again
    repo.check_risk("capped", "SGX:C07", "sell", to_micro("100"),
                    to_micro("3"), later + 86400)
    # a venue-rejected order is not a trade
    conn.execute("UPDATE orders SET status='rejected'")
    repo.check_risk("capped", "SGX:C07", "sell", to_micro("100"),
                    to_micro("3"), later)


# ── follow-ups: symbol validation at the API, isolated scheduled starts ─────
def test_runbody_rejects_bad_ticker_with_422(api_world):
    from app.api.desk import Desk
    from app.api.main import create_app
    conn, repo, script, resume_post, wi = api_world
    desk = Desk(conn, script.deps, SqliteSaver(
        sqlite3.connect(":memory:", check_same_thread=False)), repo,
        clock=lambda: T0)
    client = TestClient(create_app(desk, conn, SECRET, clock=lambda: T0))
    r = client.post("/api/run", json={"ticker": "<img src=x>",
                                      "exchange": "NASDAQ", "currency": "USD"})
    assert r.status_code == 422


def test_one_bad_pick_does_not_stop_the_others():
    from app.api.server import start_runs

    class D:
        def __init__(self):
            self.started = []

        def start_run(self, inst, trigger=None):
            if inst.ticker == "BAD":
                raise ValueError("invalid ticker 'BAD'")
            self.started.append(inst.id)
            return f"wi_{inst.ticker}"
    d = D()
    started, skipped = start_runs(d, [
        ("BAD", "SGX", "t", "r"), ("X", "NOWHERE", "t", "r"),
        ("D05", "SGX", "t", "r")])
    assert d.started == ["SGX:D05"]
    assert [s["ticker"] for s in skipped] == ["BAD", "X"]
    assert started[0]["work_item"] == "wi_D05"


def test_a_refused_trade_can_be_amended_then_retried(api_world):
    """Saxo refused a bracket as TooFarFromEntryOrder (2026-11-04); the human
    edits the levels and retries. Execution must see the NEW levels — it
    reads the thesis from the graph state, not desk.db."""
    from app.approvals import AuthError, thesis_action
    conn, repo, script, resume_post, wi = api_world
    with pytest.raises(AuthError):
        thesis_action(conn, {"work_item_id": wi, "action": "approve",
                             "size_base": 1000.0, "broker": "alpaca",
                             "token": _row(conn, wi)["approval_token"]},
                      resume_post, clock=lambda: T0)
    seen = script.theses
    assert seen and seen[-1][1] is not None
    entry = seen[-1][0]
    code, out = amend_post(resume_post, wi, entry=entry, stop=entry * 0.95,
                           target=entry * 1.08)
    assert code == 200, out
    code, _ = resume_post({"work_item_id": wi, "status": "approved",
                           "actor": "human",
                           "token": _row(conn, wi)["approval_token"],
                           "size_base": 1000.0, "broker": "alpaca"})
    assert code == 200
    assert script.theses[-1] == (entry, entry * 0.95, entry * 1.08)
    t = json.loads(_row(conn, wi)["thesis_json"])
    assert t["stop_loss"] == entry * 0.95
    events = [r[0] for r in conn.execute(
        "SELECT to_state FROM events WHERE item_id=? ORDER BY id", (wi,))]
    assert "amended" in events


def test_amend_refuses_incoherent_levels_and_unrefused_trades(api_world):
    conn, repo, script, resume_post, wi = api_world
    code, _ = amend_post(resume_post, wi, entry=100, stop=95)
    assert code == 409                       # still awaiting approval
    from app.approvals import AuthError, thesis_action
    with pytest.raises(AuthError):
        thesis_action(conn, {"work_item_id": wi, "action": "approve",
                             "size_base": 1000.0, "broker": "alpaca",
                             "token": _row(conn, wi)["approval_token"]},
                      resume_post, clock=lambda: T0)
    code, _ = amend_post(resume_post, wi, entry=100, stop=105)   # buy: stop above
    assert code == 422


def amend_post(resume_post, wi, **levels):
    """POST /internal/amend through the same TestClient as resume_post."""
    client = resume_post.__closure__[0].cell_contents
    r = client.post("/internal/amend",
                    json={"work_item_id": wi, "actor": "human", **levels},
                    headers={"x-claire-secret": SECRET})
    return r.status_code, r.json()
