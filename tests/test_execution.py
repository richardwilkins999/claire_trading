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
    triggers = []
    watcher = Watcher(w.conn, w.repo, w.market,
                      lambda inst, trigger=None: (
                          reviews.append(inst["id"]) or
                          triggers.append(trigger) or
                          f"wi_sr_{len(reviews)}"),
                      clock=w.clock)
    r = watcher.tick()                          # price 100 ≈ entry: no breach
    assert r["fired"] == []
    w.market.price = Decimal("90")              # ~11% below 101 avg → breach
    r = watcher.tick()
    assert len(r["fired"]) == 1
    assert reviews == ["NASDAQ:NVDA"]
    # the exit review is told WHY it was ordered (it used to start blind)
    assert "watcher" in triggers[0] and "trailing alert hit" in triggers[0]
    r = watcher.tick()                          # same price: escalation gate
    assert r["fired"] == []                     # needs a further 3% decline
    w.market.price = Decimal("86")
    w.conn.execute(                             # pretend review still open
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
    watcher = Watcher(w.conn, w.repo, w.market,
                      lambda inst, trigger=None: "wi_x",
                      clock=w.clock)
    w.market.price = Decimal("50")              # huge drop, but market shut
    r = watcher.tick()
    assert r["fired"] == []
    assert "NASDAQ:NVDA" in r["closed"]


def test_sim_dedupes_on_client_order_id():
    from app.tools.brokers import PaperSimBroker
    b = PaperSimBroker(fill_mode="instant")
    a = b.place_bracket(symbol="AAPL", side="buy", qty=10, limit=100,
                        client_order_id="co_1")
    again = b.place_bracket(symbol="AAPL", side="buy", qty=10, limit=100,
                            client_order_id="co_1")
    assert again.broker_order_id == a.broker_order_id
    assert len(b.orders) == 1


def test_production_deps_wire_the_gate_price_check():
    from app.wiring import _last_price

    class M:
        def quote(self, sym):
            assert sym == "C07.SI"
            return {"price": "31.20"}

    class Inst:
        ticker, exchange = "C07", "SGX"
    assert _last_price(M())(Inst()) == "31.20"


# ── a buy approved by SHARES (the Approvals card's share count) ──────────
def _shares_state(w, qty, lot=1):
    st = w.state()
    inst = st.instrument.model_copy(update={"lot_size": lot})
    ap = st.approval.model_copy(update={"qty": qty, "size_base": 5000.0})
    return st.model_copy(update={"approval": ap, "instrument": inst})


def test_buy_by_shares_buys_exactly_that_many():
    w = World()
    out = w.execute(_shares_state(w, 7))
    o = w.repo.order(out["order_ids"][0])
    assert o["qty"] == to_micro("7")          # not re-sized from size_base


def test_buy_by_shares_floors_to_the_venue_lot():
    w = World()
    out = w.execute(_shares_state(w, 250, lot=100))
    assert w.repo.order(out["order_ids"][0])["qty"] == to_micro("200")
    w2 = World()
    with pytest.raises(Exception, match="less than one tradable lot"):
        w2.execute(_shares_state(w2, 50, lot=100))


# ── stop / target checked against the LIVE price, not just the limit ─────
def _levels_state(w, stop, target, entry=110.0):
    st = w.state()
    th = st.thesis.model_copy(update={"entry_low": entry, "entry_high": entry,
                                      "stop_loss": stop, "take_profit": target})
    return st.model_copy(update={"thesis": th})


def test_a_stop_above_the_live_price_is_refused_before_sending():
    # limit 110 with the stock at 100: it fills at ~100, so a 105 stop
    # (below the limit, so every earlier check passed) would sell at once
    w = World(price="100")
    with pytest.raises(ValueError, match="stop would sell straight away"):
        w.execute(_levels_state(w, stop=105.0, target=130.0))
    assert w.broker.snapshot()["orders"] == {}          # nothing was sent


def test_a_take_profit_below_the_live_price_is_refused():
    w = World(price="100")
    with pytest.raises(ValueError, match="take-profit .* would sell straight away"):
        w.execute(_levels_state(w, stop=90.0, target=99.0, entry=101.0))


def test_levels_either_side_of_the_live_price_go_through():
    w = World(price="100")
    out = w.execute(_levels_state(w, stop=95.0, target=120.0, entry=110.0))
    assert out["order_ids"]
