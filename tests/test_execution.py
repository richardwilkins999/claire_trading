"""Executor + custodian + watcher tests — the async order lifecycle
(DESIGN.md §11/§12/§15), offline with the paper sim broker."""
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.accounting import db
from app.accounting.money import to_micro
from app.accounting.repo import Repo
from app.custodian import Custodian
from app.graph.nodes_execution import build_execute, position_qty
from app.graph.state import Approval, Instrument, PipelineState, Thesis
from app.tools.brokers import PaperSimBroker
from app.watcher import Watcher

OPEN_TS = 1_786_456_800          # Tue 2026-08-11 14:00 UTC — NASDAQ open
SAT_TS = OPEN_TS - 3 * 86400     # Sat 2026-08-08
NVDA = Instrument(id="NASDAQ:NVDA", ticker="NVDA", exchange="NASDAQ",
                  currency="USD")


class FakeMarket:
    def __init__(self, price="100"):
        self.price = Decimal(price)

    def quote(self, symbol):
        return {"symbol": symbol, "price": self.price, "currency": "USD",
                "stale": False}

    def fx(self, a, b):
        return Decimal(1)


class World:
    def __init__(self, ts=OPEN_TS, fill_mode="manual", price="100"):
        self.t = {"now": ts}
        self.clock = lambda: self.t["now"]
        self.conn = db.connect(":memory:")
        db.init(self.conn)
        self.repo = Repo(self.conn)
        self.repo.create_account("acct", "alpaca", "paper", "USD",
                                 fee_model={"type": "flat", "per_trade": "0"},
                                 ts=ts)
        self.repo.add_instrument("NASDAQ:NVDA", "NVDA", "NASDAQ", "USD")
        self.repo.deposit("acct", to_micro("50000"), ts=ts)
        self.broker = PaperSimBroker(fill_mode=fill_mode)
        self.market = FakeMarket(price)
        self.execute = build_execute(
            self.repo, {"alpaca": self.broker}, self.market,
            account_for=lambda b: "acct", clock=self.clock)
        self.resume_calls = []
        self.custodian = Custodian(
            self.conn, self.repo, {"alpaca": self.broker},
            resume_post=self._resume, account_broker=lambda a: "alpaca",
            fx_rate_for=lambda i, a: to_micro("1"), clock=self.clock)

    def _resume(self, body):
        self.resume_calls.append(body)
        return 200, {"ok": True}

    def state(self, wi="wi_x", direction="buy", size=1000.0):
        self.conn.execute(
            "INSERT OR IGNORE INTO work_items (id, kind, ticker, state,"
            " thread_id, created_at, updated_at)"
            " VALUES (?, 'pipeline', 'NVDA', 'executing', ?, ?, ?)",
            (wi, wi, self.clock(), self.clock()))
        kw = dict(entry_low=99.0, entry_high=101.0, stop_loss=95.0,
                  take_profit=120.0) if direction == "buy" else \
            dict(entry_low=99.0, entry_high=101.0, stop_loss=110.0)
        return PipelineState(
            work_item_id=wi, ticker="NVDA", instrument=NVDA,
            thesis=Thesis(ticker="NVDA", direction=direction, conviction=0.7,
                          currency="USD", narrative_path="n", **kw),
            approval=Approval(status="approved", size_base=size,
                              broker="alpaca", actor="human", token="t",
                              at=datetime.now(timezone.utc)))


def test_buy_exceeding_balance_never_reaches_the_venue():
    w = World()                                 # balance 50k
    with pytest.raises(Exception, match="holds 50,?000|holds 50000"):
        w.execute(w.state(size=60000.0))
    assert w.broker.orders == {}                # venue never saw it
    (n,) = w.conn.execute("SELECT COUNT(*) FROM orders").fetchone()
    assert n == 0


def test_risk_cap_checked_before_placement():
    w = World()
    w.repo.create_account("capped", "alpaca", "paper", "USD",
                          fee_model={"type": "flat", "per_trade": "0"},
                          risk_limits={"max_order_base": 100}, ts=OPEN_TS)
    w.repo.deposit("capped", to_micro("50000"), ts=OPEN_TS)
    from app.graph.nodes_execution import build_execute
    execute = build_execute(w.repo, {"alpaca": w.broker}, w.market,
                            account_for=lambda b: "capped", clock=w.clock)
    from app.accounting.repo import RiskLimitExceeded
    with pytest.raises(RiskLimitExceeded):
        execute(w.state(size=1000.0))
    assert w.broker.orders == {}                # refused BEFORE placement


def test_sell_review_never_flips_short():
    w = World(fill_mode="instant")
    w.execute(w.state())                        # buy ~10 shares
    w.custodian.run_once()
    st = w.state(wi="wi_sr", direction="sell")
    st = st.model_copy(update={"kind": "sell_review",
                               "approval": st.approval.model_copy(
                                   update={"qty": 999.0})})
    out = w.execute(st)
    o = w.repo.order(out["order_ids"][0])
    assert o["qty"] == to_micro("10")           # capped at held, not 999


def test_sell_with_no_position_and_no_qty_refused():
    w = World()
    st = w.state(wi="wi_s2", direction="sell")  # approval.qty defaults None
    with pytest.raises(ValueError, match="quantity is zero"):
        w.execute(st)
    assert w.broker.orders == {}


def test_position_qty_lot_floor():
    assert position_qty(Decimal(1000), Decimal(100), 1) == 10
    assert position_qty(Decimal(1000), Decimal("3.42"), 100) == 200  # SGX lots
    assert position_qty(Decimal(300), Decimal("3.42"), 100) == 0


def test_execute_places_order_fills_async_custodian_completes():
    w = World()
    out = w.execute(w.state())
    (oid,) = out["order_ids"]
    o = w.repo.order(oid)
    assert o["status"] == "placed"
    assert o["qty"] == to_micro("10")           # $1000 at $100 limit-priced
    assert w.repo.cash_balance("acct") == to_micro("50000")  # nothing moved yet

    # broker fills in two parts; custodian records each idempotently
    w.broker.simulate_fill(o["broker_order_id"], 4, "100.5")
    r1 = w.custodian.run_once()
    assert r1["fills"] == 1
    assert w.repo.order(oid)["status"] == "partially_filled"
    w.broker.simulate_fill(o["broker_order_id"], 6, "101")
    r2 = w.custodian.run_once()
    assert w.repo.order(oid)["status"] == "filled"
    assert r2["completed"] == ["wi_x"]          # executing → done
    qty, cost, comm = w.repo.position("acct", "NASDAQ:NVDA")
    assert qty == to_micro("10")
    w.repo.assert_invariants("acct")
    # re-poll: no duplicate fills
    assert w.custodian.run_once()["fills"] == 0


def test_order_ttl_cancels_and_partial_fill_closes_done():
    w = World()
    (oid,) = w.execute(w.state())["order_ids"]
    o = w.repo.order(oid)
    w.broker.simulate_fill(o["broker_order_id"], 3, "100")
    w.custodian.run_once()
    w.t["now"] = o["expires_at"] + 60           # past end-of-session TTL
    r = w.custodian.run_once()
    assert oid in r["order_expired"]
    assert w.repo.order(oid)["status"] == "expired"
    assert w.broker.order_status(o["broker_order_id"])["status"] == "cancelled"
    wi = w.conn.execute("SELECT state FROM work_items WHERE id='wi_x'").fetchone()
    assert wi["state"] == "done"                # kept the 3 filled shares


def test_closed_market_queues_then_custodian_places_at_open():
    w = World(ts=SAT_TS)
    (oid,) = w.execute(w.state())["order_ids"]
    assert w.repo.order(oid)["status"] == "pending_session"
    assert w.broker.orders == {}                # nothing reached the venue
    w.custodian.run_once()
    assert w.repo.order(oid)["status"] == "pending_session"  # still Saturday
    w.t["now"] = 1_786_368_600                  # Mon 2026-08-10 13:30 UTC: open
    r = w.custodian.run_once()
    assert r["placed"] == [oid]
    assert w.repo.order(oid)["status"] == "placed"
    assert len(w.broker.orders) == 1


def test_custodian_expires_stale_approvals_via_reaper():
    w = World()
    w.conn.execute(
        "INSERT INTO work_items (id, kind, ticker, state, thread_id,"
        " approval_token, token_state, expires_at, created_at, updated_at)"
        " VALUES ('wi_old', 'pipeline', 'NVDA', 'awaiting_approval', 'wi_old',"
        " 'tok123', 'minted', ?, ?, ?)",
        (w.clock() - 10, w.clock() - 100, w.clock() - 100))
    r = w.custodian.run_once()
    assert r["approvals_expired"] == ["wi_old"]
    assert w.resume_calls[0]["actor"] == "reaper"
    assert w.resume_calls[0]["status"] == "expired"


def test_watcher_escalating_alerts_launch_one_sell_review():
    w = World(fill_mode="instant")
    w.execute(w.state())                        # buy 10 @ ~101 instantly
    w.custodian.run_once()                      # record fills
    reviews = []
    watcher = Watcher(w.conn, w.repo, w.market,
                      lambda inst: (reviews.append(inst["id"]) or
                                    f"wi_sr_{len(reviews)}"),
                      clock=w.clock)
    r = watcher.tick()                          # price 100 ≈ entry: no breach
    assert r["fired"] == []
    w.market.price = Decimal("90")              # ~11% below 101 avg → breach
    r = watcher.tick()
    assert len(r["fired"]) == 1
    assert reviews == ["NASDAQ:NVDA"]
    r = watcher.tick()                          # same price: escalation gate
    assert r["fired"] == []                     # needs a further 3% decline
    w.market.price = Decimal("86")
    open_review = w.conn.execute(               # pretend review still open
        "INSERT INTO work_items (id, kind, ticker, state, thread_id,"
        " created_at, updated_at) VALUES ('wi_sr', 'sell_review', 'NVDA',"
        " 'awaiting_approval', 'wi_sr', ?, ?)", (w.clock(), w.clock()))
    r = watcher.tick()
    assert len(r["fired"]) == 1                 # alert fired (escalated) …
    assert len(reviews) == 1                    # … but no second review stacked


def test_watcher_respects_closed_market():
    w = World(fill_mode="instant", ts=SAT_TS)
    w.t["now"] = OPEN_TS
    w.execute(w.state())
    w.custodian.run_once()
    w.t["now"] = SAT_TS                         # Saturday
    watcher = Watcher(w.conn, w.repo, w.market, lambda inst: "wi_x",
                      clock=w.clock)
    w.market.price = Decimal("50")              # huge drop, but market shut
    r = watcher.tick()
    assert r["fired"] == []
    assert "NASDAQ:NVDA" in r["closed"]
