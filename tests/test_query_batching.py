"""Portfolio and Trades ask the database a fixed number of questions, however
many positions the book holds (REVIEW-2026-11-11 §4.3, "N+1 queries").

Three kinds of test. The first is a characterisation: a book with every
shape the pages know about, and the JSON both pages produced for it BEFORE
the queries were batched (kept at the bottom as text). The batched code must
produce the same bytes. The second counts SQL statements for 2 positions and
for 8. The third is the ledger's own position (Repo.position), which now
reads the lots through accounting/holdings.py (§3.1).
"""
import json
import time
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app import portfolio
from app.accounting import db, holdings
from app.accounting import repo as repo_module
from app.accounting.money import to_micro
from app.accounting.repo import Repo
from app.dash.views_orders import OrderViews

T0 = 1_786_456_800          # Tue 2026-08-11 14:00 UTC — NASDAQ open
NOW = T0 + 3 * 86400 + 3600  # Fri 2026-08-14 15:00 UTC
TODAY = "2026-08-14"


class FakeMarket:
    """Prices by symbol. VOD.L has none; euros cannot be turned into
    Singapore dollars (the rate source is down)."""
    PRICES = {"NVDA": "120", "CSCO": "105.5", "TSLA": "245", "ACA.PA": "16.2",
              "D05.SI": "36.4"}
    RATES = {("USD", "SGD"): "1.34", ("GBP", "USD"): "1.27"}

    def _price(self, s):
        if s in self.PRICES:
            return self.PRICES[s]
        return None if s == "VOD.L" else "50"

    def metrics(self, symbols):
        out = {}
        for s in symbols:
            px = self._price(s)
            if px is None:
                continue
            out[s] = {"symbol": s, "name": f"{s} plc", "price": Decimal(px),
                      "currency": {"ACA.PA": "EUR", "D05.SI": "SGD"}.get(
                          s, "USD"),
                      "change_pct": 1.5, "day_open": Decimal("118"),
                      "day_high": Decimal("121"), "day_low": Decimal("117"),
                      "volume": 5e6, "avg_volume": 2e6,
                      "week52_high": Decimal("150"),
                      "week52_low": Decimal("80"), "rsi": 61.0,
                      "mcap": None, "src": "twelvedata"}
        return out

    def spark(self, symbols):
        return {s: {"price": Decimal(self._price(s)), "stale": False}
                for s in symbols if self._price(s) is not None}

    def fx(self, a, b):
        if a == b:
            return Decimal(1)
        if (a, b) not in self.RATES:
            raise RuntimeError(f"no {a}→{b} rate")
        return Decimal(self.RATES[(a, b)])


class Book:
    """A desk.db in memory, with helpers that write the way the desk does
    (through Repo) and ids that do not change from run to run."""

    def __init__(self):
        self.conn = db.connect(":memory:")
        db.init(self.conn)
        self.repo = Repo(self.conn)
        self.t = T0
        self.n = 0

    def tick(self):
        self.t += 60                # every fill at its own second: no ties
        return self.t

    def account(self, id, broker, ccy, fee):
        self.repo.create_account(id, broker, "paper", ccy, fee_model=fee,
                                 ts=T0)
        self.repo.deposit(id, to_micro("1000000"), ts=T0)

    def item(self, id, kind, ticker, *, thesis=None, trigger=None,
             override_of=None, state="done", at=None):
        at = self.t if at is None else at
        self.conn.execute(
            "INSERT INTO work_items (id, kind, ticker, state, thread_id,"
            " thesis_json, trigger, override_of, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (id, kind, ticker, state, id,
             thesis if isinstance(thesis, str) or thesis is None
             else json.dumps(thesis), trigger, override_of, at, at))

    def report(self, wi, agent, payload, kind="analyst"):
        self.conn.execute(
            "INSERT INTO agent_reports (work_item_id, agent_id, kind,"
            " payload, created_at) VALUES (?,?,?,?,?)",
            (wi, agent, kind, payload if isinstance(payload, str)
             else json.dumps(payload), self.t))

    def event(self, wi, to_state, ts, payload=None):
        self.conn.execute(
            "INSERT INTO events (item_id, ts, actor, from_state, to_state,"
            " payload) VALUES (?,?,?,?,?,?)",
            (wi, ts, "human", None, to_state, payload))

    def order(self, acct, inst, side, qty, *, wi=None, limit=None, stop=None,
              target=None, trail=None, venue_id=None):
        self.n += 1
        oid = f"o{self.n:02d}"
        self.repo.create_order(
            id=oid, account_id=acct, instrument_id=inst, side=side,
            qty=to_micro(qty), work_item_id=wi, expires_at=T0 + 10 * 86400,
            ts=self.tick(), broker_order_id=venue_id,
            limit_price=to_micro(limit) if limit else None,
            stop_loss=to_micro(stop) if stop else None,
            take_profit=to_micro(target) if target else None,
            trail_pct=trail)
        return oid

    def fill(self, oid, qty, price, fx="1"):
        self.n += 1
        return self.repo.record_fill(
            oid, broker_fill_id=f"f{self.n:02d}", qty=to_micro(qty),
            price_native=to_micro(price), fx_rate=to_micro(fx),
            ts=self.tick())

    def trade(self, acct, inst, side, qty, price, fx="1", **kw):
        oid = self.order(acct, inst, side, qty, **kw)
        self.fill(oid, qty, price, fx)
        return oid

    def leg(self, parent, kind, acct, inst, side, qty, price, venue_id,
            wi=None):
        self.n += 1
        oid = f"o{self.n:02d}"
        self.repo.create_leg_order(
            id=oid, parent_order_id=parent, leg=kind, account_id=acct,
            instrument_id=inst, side=side, qty=to_micro(qty),
            broker_order_id=venue_id, ts=self.tick(),
            expires_at=T0 + 400 * 86400, work_item_id=wi,
            stop_loss=to_micro(price) if kind == "stop" else None,
            limit_price=to_micro(price) if kind == "target" else None)
        return oid

    def alert(self, inst, acct, side, pct, *, peak=None, fired=0, date=None,
              last=None):
        self.conn.execute(
            "INSERT INTO price_alerts (instrument_id, account_id, side,"
            " rule, threshold, armed, peak_base, last_fired_at,"
            " fire_count_today, fire_count_date)"
            " VALUES (?,?,?,'trail_pct',?,1,?,?,?,?)",
            (inst, acct, side, pct, peak, last, fired, date))


def _steady_ids(monkeypatch):
    """Executions and lots get random ids; a snapshot needs the same ones
    every run, so the ledger's uuid is replaced by a counter here."""
    seq = iter(range(1, 10_000))
    monkeypatch.setattr(repo_module, "uuid", SimpleNamespace(
        uuid4=lambda: SimpleNamespace(hex=f"{next(seq):012d}")))


def full_book():
    """Two accounts in two currencies; six positions and one closed out:

    us/NVDA   bought twice, sold in two goes (so the average of what is
              still held is not the average of everything bought, and one
              lot was closed by two sales); the old kind of alert that
              belongs to no account
    sg/NVDA   an override run; its own alert has fired twice today and the
              sell review it started has reached a verdict
    sg/CSCO   bought in two parts, each with a stop and a target resting at
              Saxo — the first stop trails; a stop and a target part filled
    us/TSLA   a short, part covered, guarded by a stop held in the sim; its
              alert has used up today's fires, and the only review is older
              than the last of them
    sg/ACA    euros in a Singapore-dollar account, and no exchange rate
    us/VOD    no price at all
    sg/D05    bought three times (once before the Trades page's window) and
              sold out in two sales: only in "closed" and on Trades
    """
    b = Book()
    b.account("us", "alpaca", "USD", {"type": "flat", "per_trade": "1"})
    b.account("sg", "saxo", "SGD", {"type": "pct", "pct": "0.001",
                                    "min": "2"})
    for iid, ccy in (("NASDAQ:NVDA", "USD"), ("NASDAQ:CSCO", "USD"),
                     ("NASDAQ:TSLA", "USD"), ("PARIS:ACA", "EUR"),
                     ("LSE:VOD", "GBP"), ("SGX:D05", "SGD")):
        ex, tk = iid.split(":")
        b.repo.add_instrument(iid, tk, ex, ccy, name=f"{tk} plc")

    b.t = T0 - 40 * 86400                       # six weeks ago
    b.trade("sg", "SGX:D05", "buy", "50", "30")
    b.t = T0

    # us/NVDA — a desk recommendation, a manual top-up, a reviewed sale
    b.item("wi_p1", "pipeline", "NVDA", thesis={
        "ticker": "NVDA", "direction": "buy", "conviction": 0.72,
        "entry_low": 99, "entry_high": 101, "stop_loss": 92,
        "take_profit": 130, "currency": "USD"})
    b.report("wi_p1", "fundamentals", {"summary": "earnings beat"})
    b.report("wi_p1", "technicals", {"summary": "above the 50-day"})
    b.report("wi_p1", "news", {"detail": "no summary here"})
    b.report("wi_p1", "macro", "not json at all")
    b.event("wi_p1", "approved", T0 + 30)
    b.event("wi_p1", "approved", T0 + 20)       # the earlier one counts
    b.trade("us", "NASDAQ:NVDA", "buy", "10", "100", wi="wi_p1")
    b.item("wi_m1", "manual", "NVDA", thesis={
        "conditions": ["MANUAL BUY 5 NVDA at 110"]}, trigger="manual buy")
    b.trade("us", "NASDAQ:NVDA", "buy", "5", "110", wi="wi_m1")
    b.item("wi_s1", "sell_review", "NVDA", thesis={"direction": "sell"},
           trigger="watcher: trailing alert hit — last 119.00")
    b.report("wi_s1", "technicals", {"summary": "momentum fading"})
    b.event("wi_s1", "approved", b.t + 5)
    b.trade("us", "NASDAQ:NVDA", "sell", "4", "120", wi="wi_s1")
    b.trade("us", "NASDAQ:NVDA", "sell", "2", "121", wi="wi_s1")
    b.alert("NASDAQ:NVDA", None, None, 8, peak=121.5)

    # sg/NVDA — the desk passed, the human overrode
    b.item("wi_p0", "pipeline", "NVDA", state="rejected",
           thesis={"direction": "hold", "conviction": 0.41})
    b.report("wi_p0", "fundamentals", {"summary": "priced for perfection"})
    b.report("wi_p0", "arbiter", {"direction": "hold", "conviction": 0.41},
             kind="thesis")
    b.item("wi_o1", "override", "NVDA", override_of="wi_p0", thesis={
        "direction": "buy", "stop_loss": 93.5, "take_profit": 128,
        "entry_low": 100, "entry_high": 102})
    b.event("wi_o1", "override", b.t, json.dumps({"note": "I like it"}))
    b.event("wi_o1", "approved", b.t + 1)
    b.trade("sg", "NASDAQ:NVDA", "buy", "3", "101", "1.35", wi="wi_o1")
    b.alert("NASDAQ:NVDA", "sg", 1, 10, peak=140.4, fired=2, date=TODAY,
            last=NOW - 600)
    b.item("wi_s2", "sell_review", "NVDA", state="done",
           thesis="{broken", at=NOW - 650)
    b.item("wi_s3", "sell_review", "NVDA", state="awaiting_approval",
           thesis={"direction": "hold"}, at=NOW - 500)  # the newest: shown

    # sg/CSCO — two buys, each with its own stop and target at the venue
    b.item("wi_c1", "pipeline", "CSCO", thesis={
        "direction": "buy", "stop_loss": 97, "take_profit": 115,
        "entry_low": 102, "entry_high": 104})
    c1 = b.order("sg", "NASDAQ:CSCO", "buy", "9", wi="wi_c1", limit="103",
                 stop="97", target="115", trail=8, venue_id="5000")
    b.fill(c1, "4", "103", "1.33")              # filled in two goes
    b.fill(c1, "5", "103.2", "1.33")
    b.leg(c1, "stop", "sg", "NASDAQ:CSCO", "sell", "9", "97", "5001",
          wi="wi_c1")
    g1 = b.leg(c1, "target", "sg", "NASDAQ:CSCO", "sell", "9", "115",
               "5002", wi="wi_c1")
    b.item("wi_c2", "pipeline", "CSCO", thesis={
        "direction": "buy", "stop_loss": 98, "take_profit": 116})
    c2 = b.trade("sg", "NASDAQ:CSCO", "buy", "6", "104", "1.34", wi="wi_c2",
                 limit="104", stop="98", target="116", venue_id="5003")
    s2 = b.leg(c2, "stop", "sg", "NASDAQ:CSCO", "sell", "6", "98", "5004",
               wi="wi_c2")
    b.leg(c2, "target", "sg", "NASDAQ:CSCO", "sell", "6", "116", "5005",
          wi="wi_c2")
    b.fill(s2, "2", "98", "1.34")               # the stop sold 2 of its 6
    b.fill(g1, "1", "115", "1.34")              # and a target 1 of its 9
    b.alert("NASDAQ:CSCO", "sg", 1, 8, fired=3, date="2026-08-13",
            last=NOW - 90000)                   # fired yesterday, not today

    b.t = T0 + 86400                            # the next day
    # us/TSLA — a short nobody's run asked for, booked from the broker
    t1 = b.order("us", "NASDAQ:TSLA", "sell", "7", limit="250", stop="262",
                 trail=5)
    b.fill(t1, "7", "250")
    b.leg(t1, "stop", "us", "NASDAQ:TSLA", "buy", "7", "262", "sim-00a1")
    b.trade("us", "NASDAQ:TSLA", "buy", "2", "240")
    b.alert("NASDAQ:TSLA", "us", -1, 6, peak=238.0, fired=6, date=TODAY,
            last=NOW - 300)
    # after NVDA's alert fired, but before TSLA's: not TSLA's review
    b.item("wi_s4", "sell_review", "TSLA", thesis={"direction": "sell"},
           at=NOW - 600)

    # sg/ACA and us/VOD — one without a rate, one without a price
    b.trade("sg", "PARIS:ACA", "buy", "20", "15", "1.5")
    b.alert("PARIS:ACA", "sg", 1, 7.5, peak=24.0)
    b.item("wi_v1", "screen", "VOD", trigger="screen: cheap telecoms")
    b.trade("us", "LSE:VOD", "buy", "100", "0.7", "1.27", wi="wi_v1")

    # sg/D05 — in and out: the first sale closes two lots and part of a
    # third, the second the rest
    b.trade("sg", "SGX:D05", "buy", "60", "35")
    b.trade("sg", "SGX:D05", "buy", "40", "35.5")
    b.trade("sg", "SGX:D05", "sell", "120", "36")
    b.trade("sg", "SGX:D05", "sell", "30", "36.2")
    return b


def _steady_today(monkeypatch):
    """Portfolio asks the wall clock which day it is when it decides whether
    an alert "fired today"; the snapshot needs that day to stand still."""
    monkeypatch.setattr(portfolio, "time", SimpleNamespace(
        time=lambda: NOW, gmtime=time.gmtime, strftime=time.strftime))


@pytest.fixture
def book(monkeypatch):
    _steady_ids(monkeypatch)
    _steady_today(monkeypatch)
    return full_book()


def portfolio_json(b, market=None):
    data = portfolio.build(b.conn, market or FakeMarket(), clock=lambda: NOW,
                           env={"SAXO_SIM_EXECUTION": "enabled"})
    return json.dumps(data, default=str)


def trades_json(b, days=30, market=None):
    view = OrderViews()
    view.ctx = SimpleNamespace(conn=b.conn, clock=lambda: NOW,
                               market=market or FakeMarket(), repo=b.repo,
                               env={})
    return json.dumps(view._trades(days), default=str)


# ── the pages say what they said before ──────────────────────────────────
def test_portfolio_is_what_it_was_before_the_queries_were_batched(book):
    got = portfolio_json(book)
    assert json.loads(got) == json.loads(PORTFOLIO_BEFORE)
    assert got == PORTFOLIO_BEFORE          # field order and all


def test_trades_is_what_it_was_before_the_queries_were_batched(book):
    got = trades_json(book)
    assert json.loads(got) == json.loads(TRADES_BEFORE)
    assert got == TRADES_BEFORE


def test_the_snapshot_book_has_every_shape(book):
    """If the book stopped exercising a branch, the snapshot would still
    pass and prove less. Pin what it is meant to contain."""
    d = json.loads(portfolio_json(book))
    held = {(p["account_id"], p["ticker"]): p
            for ps in d["exchanges"].values() for p in ps}
    assert set(held) == {("us", "NVDA"), ("sg", "NVDA"), ("sg", "CSCO"),
                         ("us", "TSLA"), ("sg", "ACA"), ("us", "VOD")}
    assert held["us", "TSLA"]["side"] == "short"
    assert held["us", "VOD"]["price_unavailable"]
    assert held["sg", "ACA"]["fx_unavailable"]
    legs = held["sg", "CSCO"]["legs"]
    assert [l["leg"] for l in legs] == ["stop", "stop", "target", "target"]
    assert "trails" in legs[0] and "trails" not in legs[1]
    assert {l["status"] for l in legs} == {"placed", "partially_filled"}
    fired = held["sg", "NVDA"]["levels"]
    assert fired["alert_fired_today"] == 2
    assert fired["alert_review"] == {"id": "wi_s3", "verdict": "hold",
                                     "state": "awaiting_approval"}
    assert held["us", "TSLA"]["levels"]["alert_done_today"] is True
    assert "alert_review" not in held["us", "TSLA"]["levels"]
    assert "alert_fired_today" not in held["sg", "CSCO"]["levels"]
    assert len(d["accounts"]) == 2 and len(d["totals"]) == 2
    t = json.loads(trades_json(book))
    assert {x["leg"] for x in t["trades"]} == {None, "stop", "target"}
    assert max(len(x["lot"]["closed_by"]) for x in t["trades"]
               if x["lot"]) == 2
    assert any(x["closes"] and x["closes"]["held_days"] > 30
               for x in t["trades"])        # closed a lot older than the page


def test_a_shorter_trades_page_shows_the_same_rows(book):
    """A fill reads the same whichever window it is seen through: what it
    closed and what closed it do not depend on whether THOSE fills are on
    the page. (The lookups are scoped to the page's fills; this is the
    check that the scope is not too narrow.)"""
    month = json.loads(trades_json(book, days=30))["trades"]
    recent = json.loads(trades_json(book, days=3))["trades"]
    since = NOW - 3 * 86400
    assert 0 < len(recent) < len(month)
    assert recent == [x for x in month if x["executed_at"] >= since]


def test_one_position_asked_alone_gets_the_same_answer(book):
    """fills_for / legs_for / realized_for / levels_for are still used on
    their own (the chart, Telegram, level changes); each must agree with
    what the whole-book lookups hand the Portfolio page."""
    data = portfolio.build(book.conn, FakeMarket(), clock=lambda: NOW)
    positions = [p for ps in data["exchanges"].values() for p in ps]
    assert len(positions) == 6
    for p in positions:
        a, i = p["account_id"], p["instrument_id"]
        assert portfolio.fills_for(book.conn, a, i) == p["fills"]
        assert portfolio.legs_for(book.conn, a, i) == p["legs"]
        assert portfolio.realized_for(book.conn, a, i) == p["realized_base"]
        fx = None
        if not p["price_unavailable"] and not p["fx_unavailable"]:
            fx = FakeMarket().fx(p["market"]["currency"], p["base_currency"])
        assert portfolio.levels_for(book.conn, p, p["fills"], fx=fx) == \
            p["levels"]
    # a position the desk does not hold: nothing, not somebody else's rows
    assert portfolio.fills_for(book.conn, "us", "SGX:D05") == []
    assert portfolio.legs_for(book.conn, "us", "NASDAQ:CSCO") == []
    assert portfolio.realized_for(book.conn, "us", "NASDAQ:CSCO") == 0.0
    assert portfolio.realized_for(book.conn, "sg", "SGX:D05") != 0.0


# ── and ask the database the same number of questions for 2 as for 8 ─────
def busy_book(n):
    """n positions over two accounts, each of a kind that used to cost the
    most statements: bought in two parts, part sold, a trailing stop and a
    target resting at the venue, an alert that fired today and the sell
    review it started."""
    b = Book()
    b.account("us", "alpaca", "USD", {"type": "flat", "per_trade": "1"})
    b.account("sg", "saxo", "SGD", {"type": "flat", "per_trade": "2"})
    for k in range(n):
        acct, tk = ("us", "sg")[k % 2], f"T{k:02d}"
        iid = f"NASDAQ:{tk}"
        b.repo.add_instrument(iid, tk, "NASDAQ", "USD")
        b.item(f"wi_b{k}", "pipeline", tk, thesis={
            "direction": "buy", "stop_loss": 90, "take_profit": 130})
        b.report(f"wi_b{k}", "fundamentals", {"summary": "fine"})
        b.event(f"wi_b{k}", "approved", b.t)
        first = b.order(acct, iid, "buy", "10", wi=f"wi_b{k}", limit="100",
                        stop="90", target="130", trail=8,
                        venue_id=str(6000 + 10 * k))
        b.fill(first, "10", "100", "1.3")
        b.leg(first, "stop", acct, iid, "sell", "10", "90",
              str(6001 + 10 * k), wi=f"wi_b{k}")
        b.leg(first, "target", acct, iid, "sell", "10", "130",
              str(6002 + 10 * k), wi=f"wi_b{k}")
        b.item(f"wi_o{k}", "override", tk, override_of=f"wi_b{k}", thesis={
            "direction": "buy", "stop_loss": 90, "take_profit": 130})
        b.event(f"wi_o{k}", "override", b.t, json.dumps({"note": "more"}))
        b.trade(acct, iid, "buy", "5", "104", "1.3", wi=f"wi_o{k}")
        b.item(f"wi_r{k}", "sell_review", tk, thesis={"direction": "sell"},
               at=NOW - 200)
        b.trade(acct, iid, "sell", "3", "110", "1.3", wi=f"wi_r{k}")
        b.alert(iid, acct, 1, 8, peak=150.0, fired=1, date=TODAY,
                last=NOW - 300)
    return b


def statements(conn, fn):
    """Every SQL statement `fn` runs on `conn`."""
    seen = []
    conn.set_trace_callback(seen.append)
    try:
        fn()
    finally:
        conn.set_trace_callback(None)
    return seen


def test_portfolio_runs_the_same_number_of_statements_for_2_and_8(
        monkeypatch):
    _steady_today(monkeypatch)
    counts = {}
    for n in (2, 8):
        b = busy_book(n)
        got = {}
        counts[n] = len(statements(b.conn, lambda: got.update(
            portfolio.build(b.conn, FakeMarket(), clock=lambda: NOW))))
        positions = [p for ps in got["exchanges"].values() for p in ps]
        assert len(positions) == n
        # each one really has the rows that used to be fetched one by one
        for p in positions:
            assert len(p["fills"]) == 3 and len(p["legs"]) == 2
            assert p["legs"][0]["trails"] == 8.0
            assert p["levels"]["stop_loss"] == 90
            assert p["levels"]["alert_review"]["verdict"] == "sell"
            assert p["realized_base"] != 0
    assert counts[2] == counts[8], counts


def test_trades_runs_the_same_number_of_statements_for_2_and_8():
    counts = {}
    for n in (2, 8):
        b = busy_book(n)
        got = []
        counts[n] = len(statements(b.conn, lambda: got.append(
            json.loads(trades_json(b)))))
        trades = got[0]["trades"]
        assert len(trades) == 3 * n         # two buys and a sell each
        assert all(x["lot"] or x["closes"] for x in trades)
        assert sum("your note: more" in x["details"] for x in trades) == n
        assert sum(x["approved_at"] is not None for x in trades) == n
    assert counts[2] == counts[8], counts


# ── one way to work out a position (REVIEW-2026-11-11 §3.1) ──────────────
def test_the_ledgers_position_is_the_lots_added_up_its_old_way(book):
    """Repo.position now reads through accounting/holdings.py. Its three
    numbers, worked out again here from the lots by hand: net shares
    (a short negative), the cost of the LONG lots cut to a whole micro-unit
    lot by lot, and the commission still attached."""
    b = book
    # fractional shares at an awkward price, in four lots: each lot costs
    # 777,776.3 micro-units and loses the 0.3, so the ledger's figure is
    # 3,111,104 — not the 3,111,105 that cutting the total would give
    b.repo.add_instrument("NASDAQ:FRAC", "FRAC", "NASDAQ", "USD")
    for _ in range(4):
        b.trade("us", "NASDAQ:FRAC", "buy", "0.7", "1.111109")
    assert b.repo.position("us", "NASDAQ:FRAC")[1] == 3_111_104
    pairs = b.conn.execute("SELECT DISTINCT account_id, instrument_id"
                           " FROM lots").fetchall()
    assert len(pairs) == 8
    for acct, inst in [tuple(r) for r in pairs] + [("us", "SGX:D05"),
                                                    (None, "NASDAQ:NVDA"),
                                                    ("us", None)]:
        lots = b.conn.execute(
            "SELECT qty_opened, qty_remaining, cost_per_share_base,"
            " commission_allocated FROM lots WHERE account_id=? AND"
            " instrument_id=? AND qty_remaining>0", (acct, inst)).fetchall()
        by_hand = (
            sum(-l[1] if l[0] < 0 else l[1] for l in lots),
            sum(l[1] * l[2] // 1_000_000 for l in lots if l[0] > 0),
            sum(l[3] for l in lots))
        got = b.repo.position(acct, inst)
        assert got == by_hand, (acct, inst)
        assert all(type(n) is int for n in got)
        assert got == holdings.ledger_position(b.conn, acct, inst)
        if acct and inst:
            assert holdings.net(b.conn, account_id=acct, instrument_id=inst) \
                == Decimal(got[0]) / 1_000_000
    assert b.repo.position("us", "NASDAQ:TSLA") == (-5_000_000, 0, 714_286)
    assert b.repo.position("sg", "SGX:D05") == (0, 0, 0)       # sold out


def test_the_ledger_no_longer_spells_out_net_shares_itself():
    from pathlib import Path
    src = Path(repo_module.__file__).read_text().replace("\n", "")
    assert "THEN -qty_remaining" not in src
    assert "holdings.ledger_position" in src


# ── the snapshots ────────────────────────────────────────────────────────
# What the two pages sent for `book` on 2026-11-15, before the queries were
# batched — the JSON exactly as it left the server, cut into lines. If a
# page gains or changes a field ON PURPOSE these have to be made again:
#     ./venv/bin/python -m tests.test_query_batching
# prints both, ready to paste over the old ones.
PORTFOLIO_BEFORE = (
    '{"exchanges": {"NASDAQ": [{"account_id": "sg", "broker": "saxo", "en'
    'vironment": "paper", "base_currency": "SGD", "instrument_id": "NASDA'
    'Q:CSCO", "ticker": "CSCO", "exchange": "NASDAQ", "currency": "USD", '
    '"symbol": "CSCO", "qty": 12.0, "side": "long", "cost_base": 1659.43,'
    ' "avg_cost_base": 138.28583333333333, "commissions": 2.5, "opened_at'
    '": 1786457520, "market": {"name": "CSCO plc", "price": "105.5", "cur'
    'rency": "USD", "change_pct": 1.5, "day_open": "118", "day_high": "12'
    '1", "day_low": "117", "volume": 5000000.0, "avg_volume": 2000000.0, '
    '"week52_high": "150", "week52_low": "80", "rsi": 61.0, "mcap": null,'
    ' "src": "twelvedata"}, "price_unavailable": false, "fx_unavailable":'
    ' false, "value_base": 1696.44, "unrealized_base": 37.01, "unrealized'
    '_pct": 2.230283892661938, "fees_paid_base": 2.5, "exit_fee_est_base"'
    ': 2.0, "net_base": 32.51, "net_pct": 1.9561594050290927, "realized_b'
    'ase": 0.27, "fills": [{"ts": 1786457520, "side": "buy", "qty": 4.0, '
    '"price_native": 103.0, "commission": 2.0, "fx_rate": 1.33, "work_ite'
    'm_id": "wi_c1"}, {"ts": 1786457580, "side": "buy", "qty": 5.0, "pric'
    'e_native": 103.2, "commission": 0.0, "fx_rate": 1.33, "work_item_id"'
    ': "wi_c1"}, {"ts": 1786457820, "side": "buy", "qty": 6.0, "price_nat'
    'ive": 104.0, "commission": 2.0, "fx_rate": 1.34, "work_item_id": "wi'
    '_c2"}, {"ts": 1786458000, "side": "sell", "qty": 2.0, "price_native"'
    ': 98.0, "commission": 2.0, "fx_rate": 1.34, "work_item_id": "wi_c2"}'
    ', {"ts": 1786458060, "side": "sell", "qty": 1.0, "price_native": 115'
    '.0, "commission": 2.0, "fx_rate": 1.34, "work_item_id": "wi_c1"}], "'
    'levels": {"avg_cost_native": 103.58333333333333, "stop_loss": 98, "t'
    'ake_profit": 116, "entry_low": null, "entry_high": null, "alert": 95'
    '.29666666666667, "work_item_id": "wi_c2", "alert_rule": "trailing \\u'
    '22128% of 103.58", "alert_peak": null, "alert_pct": 8.0, "alert_ref"'
    ': 103.58333333333333}, "legs": [{"leg": "stop", "side": "sell", "qty'
    '": 9.0, "price": 97.0, "status": "placed", "venue_order": "5001", "t'
    'rails": 8.24, "trail_pct": 8.0}, {"leg": "stop", "side": "sell", "qt'
    'y": 6.0, "price": 98.0, "status": "partially_filled", "venue_order":'
    ' "5004"}, {"leg": "target", "side": "sell", "qty": 9.0, "price": 115'
    '.0, "status": "partially_filled", "venue_order": "5002"}, {"leg": "t'
    'arget", "side": "sell", "qty": 6.0, "price": 116.0, "status": "place'
    'd", "venue_order": "5005"}], "can_change": {"stop_loss": true, "take'
    '_profit": true, "trailing_stop": false, "trailing_alert": true, "ven'
    'ue": "saxo", "venue_label": "Saxo SIM"}, "holding_days": 3, "is_open'
    '": true, "next_open": "2026-08-14T11:00:00-04:00", "weight_pct": 77.'
    '85977859778598}, {"account_id": "us", "broker": "alpaca", "environme'
    'nt": "paper", "base_currency": "USD", "instrument_id": "NASDAQ:TSLA"'
    ', "ticker": "TSLA", "exchange": "NASDAQ", "currency": "USD", "symbol'
    '": "TSLA", "qty": -5.0, "side": "short", "cost_base": -1250.0, "avg_'
    'cost_base": 250.0, "commissions": 0.714286, "opened_at": 1786543320,'
    ' "market": {"name": "TSLA plc", "price": "245", "currency": "USD", "'
    'change_pct": 1.5, "day_open": "118", "day_high": "121", "day_low": "'
    '117", "volume": 5000000.0, "avg_volume": 2000000.0, "week52_high": "'
    '150", "week52_low": "80", "rsi": 61.0, "mcap": null, "src": "twelved'
    'ata"}, "price_unavailable": false, "fx_unavailable": false, "value_b'
    'ase": -1225.0, "unrealized_base": 25.0, "unrealized_pct": 2.0, "fees'
    '_paid_base": 0.714286, "exit_fee_est_base": 1.0, "net_base": 23.2857'
    '14, "net_pct": 1.8617932377243191, "realized_base": 18.714286, "fill'
    's": [{"ts": 1786543320, "side": "sell", "qty": 7.0, "price_native": '
    '250.0, "commission": 1.0, "fx_rate": 1.0, "work_item_id": null}, {"t'
    's": 1786543500, "side": "buy", "qty": 2.0, "price_native": 240.0, "c'
    'ommission": 1.0, "fx_rate": 1.0, "work_item_id": null}], "levels": {'
    '"avg_cost_native": 250.0, "stop_loss": null, "take_profit": null, "e'
    'ntry_low": null, "entry_high": null, "alert": 295.12, "work_item_id"'
    ': null, "alert_rule": "trailing +24% of 238.00", "alert_peak": 238.0'
    ', "alert_pct": 6.0, "alert_ref": 238.0, "alert_set_level": 252.28, "'
    'alert_live_pct": 24.0, "alert_fired_today": 6, "alert_done_today": t'
    'rue, "alert_last_fired": 1786719300}, "legs": [{"leg": "stop", "side'
    '": "buy", "qty": 7.0, "price": 262.0, "status": "placed", "venue_ord'
    'er": "sim-00a1"}], "can_change": {"stop_loss": true, "take_profit": '
    'true, "trailing_stop": false, "trailing_alert": true, "venue": "buil'
    'tin", "venue_label": "the desk\'s simulation"}, "holding_days": 2, "i'
    's_open": true, "next_open": "2026-08-14T11:00:00-04:00", "weight_pct'
    '": 53.14533622559653}, {"account_id": "us", "broker": "alpaca", "env'
    'ironment": "paper", "base_currency": "USD", "instrument_id": "NASDAQ'
    ':NVDA", "ticker": "NVDA", "exchange": "NASDAQ", "currency": "USD", "'
    'symbol": "NVDA", "qty": 9.0, "side": "long", "cost_base": 950.0, "av'
    'g_cost_base": 105.55555555555556, "commissions": 1.4, "opened_at": 1'
    '786456920, "market": {"name": "NVDA plc", "price": "120", "currency"'
    ': "USD", "change_pct": 1.5, "day_open": "118", "day_high": "121", "d'
    'ay_low": "117", "volume": 5000000.0, "avg_volume": 2000000.0, "week5'
    '2_high": "150", "week52_low": "80", "rsi": 61.0, "mcap": null, "src"'
    ': "twelvedata"}, "price_unavailable": false, "fx_unavailable": false'
    ', "value_base": 1080.0, "unrealized_base": 130.0, "unrealized_pct": '
    '13.68421052631579, "fees_paid_base": 1.4, "exit_fee_est_base": 1.0, '
    '"net_base": 127.6, "net_pct": 13.41181416859365, "realized_base": 11'
    '9.4, "fills": [{"ts": 1786456920, "side": "buy", "qty": 10.0, "price'
    '_native": 100.0, "commission": 1.0, "fx_rate": 1.0, "work_item_id": '
    '"wi_p1"}, {"ts": 1786457040, "side": "buy", "qty": 5.0, "price_nativ'
    'e": 110.0, "commission": 1.0, "fx_rate": 1.0, "work_item_id": "wi_m1'
    '"}, {"ts": 1786457160, "side": "sell", "qty": 4.0, "price_native": 1'
    '20.0, "commission": 1.0, "fx_rate": 1.0, "work_item_id": "wi_s1"}, {'
    '"ts": 1786457280, "side": "sell", "qty": 2.0, "price_native": 121.0,'
    ' "commission": 1.0, "fx_rate": 1.0, "work_item_id": "wi_s1"}], "leve'
    'ls": {"avg_cost_native": 105.55555555555556, "stop_loss": null, "tak'
    'e_profit": null, "entry_low": null, "entry_high": null, "alert": 111'
    '.78, "work_item_id": "wi_m1", "alert_rule": "trailing \\u22128% of 12'
    '1.50", "alert_peak": 121.5, "alert_pct": 8.0, "alert_ref": 121.5}, "'
    'legs": [], "can_change": {"stop_loss": true, "take_profit": true, "t'
    'railing_stop": false, "trailing_alert": true, "venue": "builtin", "v'
    'enue_label": "the desk\'s simulation"}, "holding_days": 3, "is_open":'
    ' true, "next_open": "2026-08-14T11:00:00-04:00", "weight_pct": 46.85'
    '466377440347}, {"account_id": "sg", "broker": "saxo", "environment":'
    ' "paper", "base_currency": "SGD", "instrument_id": "NASDAQ:NVDA", "t'
    'icker": "NVDA", "exchange": "NASDAQ", "currency": "USD", "symbol": "'
    'NVDA", "qty": 3.0, "side": "long", "cost_base": 409.05, "avg_cost_ba'
    'se": 136.35, "commissions": 2.0, "opened_at": 1786457400, "market": '
    '{"name": "NVDA plc", "price": "120", "currency": "USD", "change_pct"'
    ': 1.5, "day_open": "118", "day_high": "121", "day_low": "117", "volu'
    'me": 5000000.0, "avg_volume": 2000000.0, "week52_high": "150", "week'
    '52_low": "80", "rsi": 61.0, "mcap": null, "src": "twelvedata"}, "pri'
    'ce_unavailable": false, "fx_unavailable": false, "value_base": 482.4'
    ', "unrealized_base": 73.35, "unrealized_pct": 17.931793179317932, "f'
    'ees_paid_base": 2.0, "exit_fee_est_base": 2.0, "net_base": 69.35, "n'
    'et_pct": 16.871426833718527, "realized_base": 0.0, "fills": [{"ts": '
    '1786457400, "side": "buy", "qty": 3.0, "price_native": 101.0, "commi'
    'ssion": 2.0, "fx_rate": 1.35, "work_item_id": "wi_o1"}], "levels": {'
    '"avg_cost_native": 101.0, "stop_loss": 93.5, "take_profit": 128, "en'
    'try_low": 100, "entry_high": 102, "alert": 88.01194029850745, "work_'
    'item_id": "wi_o1", "alert_rule": "trailing \\u221216% of 104.78", "al'
    'ert_peak": 140.4, "alert_pct": 10.0, "alert_ref": 104.77611940298507'
    ', "alert_set_level": 94.29850746268657, "alert_live_pct": 16.0, "ale'
    'rt_fired_today": 2, "alert_done_today": false, "alert_last_fired": 1'
    '786719000, "alert_review": {"id": "wi_s3", "state": "awaiting_approv'
    'al", "verdict": "hold"}}, "legs": [], "can_change": {"stop_loss": tr'
    'ue, "take_profit": true, "trailing_stop": false, "trailing_alert": t'
    'rue, "venue": "saxo", "venue_label": "Saxo SIM"}, "holding_days": 3,'
    ' "is_open": true, "next_open": "2026-08-14T11:00:00-04:00", "weight_'
    'pct": 22.14022140221402}], "PARIS": [{"account_id": "sg", "broker": '
    '"saxo", "environment": "paper", "base_currency": "SGD", "instrument_'
    'id": "PARIS:ACA", "ticker": "ACA", "exchange": "PARIS", "currency": '
    '"EUR", "symbol": "ACA.PA", "qty": 20.0, "side": "long", "cost_base":'
    ' 450.0, "avg_cost_base": 22.5, "commissions": 2.0, "opened_at": 1786'
    '543620, "market": {"name": "ACA.PA plc", "price": "16.2", "currency"'
    ': "EUR", "change_pct": 1.5, "day_open": "118", "day_high": "121", "d'
    'ay_low": "117", "volume": 5000000.0, "avg_volume": 2000000.0, "week5'
    '2_high": "150", "week52_low": "80", "rsi": 61.0, "mcap": null, "src"'
    ': "twelvedata"}, "price_unavailable": false, "fx_unavailable": true,'
    ' "value_base": null, "unrealized_base": null, "unrealized_pct": null'
    ', "fees_paid_base": 2.0, "exit_fee_est_base": null, "net_base": null'
    ', "net_pct": null, "realized_base": 0.0, "fills": [{"ts": 1786543620'
    ', "side": "buy", "qty": 20.0, "price_native": 15.0, "commission": 2.'
    '0, "fx_rate": 1.5, "work_item_id": null}], "levels": {"avg_cost_nati'
    've": 15.0, "stop_loss": null, "take_profit": null, "entry_low": null'
    ', "entry_high": null, "alert": 14.8, "work_item_id": null, "alert_ru'
    'le": "trailing \\u22128% of 16.00", "alert_peak": 24.0, "alert_pct": '
    '7.5, "alert_ref": 16.0}, "legs": [], "can_change": {"stop_loss": tru'
    'e, "take_profit": true, "trailing_stop": false, "trailing_alert": tr'
    'ue, "venue": "saxo", "venue_label": "Saxo SIM"}, "holding_days": 2, '
    '"is_open": true, "next_open": "2026-08-14T17:00:00+02:00", "weight_p'
    'ct": null}], "LSE": [{"account_id": "us", "broker": "alpaca", "envir'
    'onment": "paper", "base_currency": "USD", "instrument_id": "LSE:VOD"'
    ', "ticker": "VOD", "exchange": "LSE", "currency": "GBP", "symbol": "'
    'VOD.L", "qty": 100.0, "side": "long", "cost_base": 88.9, "avg_cost_b'
    'ase": 0.889, "commissions": 1.0, "opened_at": 1786543740, "market": '
    '{"name": null, "price": null, "currency": null, "change_pct": null, '
    '"day_open": null, "day_high": null, "day_low": null, "volume": null,'
    ' "avg_volume": null, "week52_high": null, "week52_low": null, "rsi":'
    ' null, "mcap": null, "src": null}, "price_unavailable": true, "fx_un'
    'available": false, "value_base": null, "unrealized_base": null, "unr'
    'ealized_pct": null, "fees_paid_base": 1.0, "exit_fee_est_base": null'
    ', "net_base": null, "net_pct": null, "realized_base": 0.0, "fills": '
    '[{"ts": 1786543740, "side": "buy", "qty": 100.0, "price_native": 0.7'
    ', "commission": 1.0, "fx_rate": 1.27, "work_item_id": "wi_v1"}], "le'
    'vels": {"avg_cost_native": 0.7, "stop_loss": null, "take_profit": nu'
    'll, "entry_low": null, "entry_high": null, "alert": null, "work_item'
    '_id": null}, "legs": [], "can_change": {"stop_loss": true, "take_pro'
    'fit": true, "trailing_stop": false, "trailing_alert": true, "venue":'
    ' "builtin", "venue_label": "the desk\'s simulation"}, "holding_days":'
    ' 2, "is_open": true, "next_open": "2026-08-14T16:00:00+01:00", "weig'
    'ht_pct": null}]}, "closed": [{"ticker": "D05", "exchange": "SGX", "q'
    'ty": 30.0, "realized": 17.5, "closed_at": 1786544220, "holding_days"'
    ': 0}, {"ticker": "D05", "exchange": "SGX", "qty": 50.0, "realized": '
    '296.2, "closed_at": 1786544100, "holding_days": 41}, {"ticker": "D05'
    '", "exchange": "SGX", "qty": 60.0, "realized": 55.74, "closed_at": 1'
    '786544100, "holding_days": 0}, {"ticker": "D05", "exchange": "SGX", '
    '"qty": 10.0, "realized": 4.14, "closed_at": 1786544100, "holding_day'
    's": 0}, {"ticker": "TSLA", "exchange": "NASDAQ", "qty": 2.0, "realiz'
    'ed": 18.714286, "closed_at": 1786543500, "holding_days": 0}, {"ticke'
    'r": "CSCO", "exchange": "NASDAQ", "qty": 1.0, "realized": 14.61, "cl'
    'osed_at": 1786458060, "holding_days": 0}, {"ticker": "CSCO", "exchan'
    'ge": "NASDAQ", "qty": 2.0, "realized": -14.34, "closed_at": 17864580'
    '00, "holding_days": 0}, {"ticker": "NVDA", "exchange": "NASDAQ", "qt'
    'y": 2.0, "realized": 40.8, "closed_at": 1786457280, "holding_days": '
    '0}, {"ticker": "NVDA", "exchange": "NASDAQ", "qty": 4.0, "realized":'
    ' 78.6, "closed_at": 1786457160, "holding_days": 0}], "accounts": [{"'
    'id": "us", "broker": "alpaca", "ccy": "USD", "cash": 1000346.1, "pos'
    'itions_value": -145.0, "equity": 1000201.1}, {"id": "sg", "broker": '
    '"saxo", "ccy": "SGD", "cash": 997848.87, "positions_value": 2178.84,'
    ' "equity": 1000027.71}], "totals": [{"ccy": "SGD", "value": 2178.84,'
    ' "unrealized": 110.36, "net": 101.86, "cash": 997848.87, "equity": 1'
    '000027.71, "positions": 3, "unpriced": 1, "accounts": 1}, {"ccy": "U'
    'SD", "value": -145.0, "unrealized": 155.0, "net": 150.885714, "cash"'
    ': 1000346.1, "equity": 1000201.1, "positions": 3, "unpriced": 1, "ac'
    'counts": 1}], "by_share_currency": [{"ccy": "EUR", "positions": 1, "'
    'unpriced": 0, "value": 324.0, "cost": 300.0, "gain": 24.0}, {"ccy": '
    '"GBP", "positions": 1, "unpriced": 1, "value": 0.0, "cost": 0.0, "ga'
    'in": 0.0}, {"ccy": "USD", "positions": 4, "unpriced": 0, "value": 14'
    '81.0, "cost": 1246.0, "gain": 235.0}], "total_value": null, "positio'
    'n_count": 6}'
)

TRADES_BEFORE = (
    '{"trades": [{"id": "ex_000000000031", "order_id": "o39", "account_id'
    '": "sg", "side": "sell", "qty": 30.0, "price": 36.2, "currency": "SG'
    'D", "fees": 2.0, "other_fees": 0.0, "gross": 1086.0, "net": 1084.0, '
    '"executed_at": 1786544220, "work_item_id": null, "ticker": "D05", "e'
    'xchange": "SGX", "name": "D05 plc", "leg": null, "order_limit": null'
    ', "order_stop": null, "parent_order_id": null, "account": "Saxo SIM '
    '(paper)", "base_currency": "SGD", "symbol": "D05.SI", "reason": "not'
    ' linked to a Claire run (booked from the broker)", "details": [], "a'
    'pproved_at": null, "closes": {"pl": 17.5, "cost": 1065.0, "qty": 30.'
    '0, "held_days": 0, "pct": 1.643192488262911}, "lot": null}, {"id": "'
    'ex_000000000030", "order_id": "o37", "account_id": "sg", "side": "se'
    'll", "qty": 120.0, "price": 36.0, "currency": "SGD", "fees": 4.32, "'
    'other_fees": 0.0, "gross": 4320.0, "net": 4315.68, "executed_at": 17'
    '86544100, "work_item_id": null, "ticker": "D05", "exchange": "SGX", '
    '"name": "D05 plc", "leg": null, "order_limit": null, "order_stop": n'
    'ull, "parent_order_id": null, "account": "Saxo SIM (paper)", "base_c'
    'urrency": "SGD", "symbol": "D05.SI", "reason": "not linked to a Clai'
    're run (booked from the broker)", "details": [], "approved_at": null'
    ', "closes": {"pl": 356.08, "cost": 3955.0, "qty": 120.0, "held_days"'
    ': 41, "pct": 9.003286978508216}, "lot": null}, {"id": "ex_0000000000'
    '28", "order_id": "o35", "account_id": "sg", "side": "buy", "qty": 40'
    '.0, "price": 35.5, "currency": "SGD", "fees": 2.0, "other_fees": 0.0'
    ', "gross": 1420.0, "net": 1422.0, "executed_at": 1786543980, "work_i'
    'tem_id": null, "ticker": "D05", "exchange": "SGX", "name": "D05 plc"'
    ', "leg": null, "order_limit": null, "order_stop": null, "parent_orde'
    'r_id": null, "account": "Saxo SIM (paper)", "base_currency": "SGD", '
    '"symbol": "D05.SI", "reason": "not linked to a Claire run (booked fr'
    'om the broker)", "details": [], "approved_at": null, "closes": null,'
    ' "lot": {"opened": 40.0, "remaining": 0.0, "cost": 35.5, "realized":'
    ' 21.64, "closed_qty": 40.0, "closed_by": ["ex_000000000030", "ex_000'
    '000000031"]}}, {"id": "ex_000000000026", "order_id": "o33", "account'
    '_id": "sg", "side": "buy", "qty": 60.0, "price": 35.0, "currency": "'
    'SGD", "fees": 2.1, "other_fees": 0.0, "gross": 2100.0, "net": 2102.1'
    ', "executed_at": 1786543860, "work_item_id": null, "ticker": "D05", '
    '"exchange": "SGX", "name": "D05 plc", "leg": null, "order_limit": nu'
    'll, "order_stop": null, "parent_order_id": null, "account": "Saxo SI'
    'M (paper)", "base_currency": "SGD", "symbol": "D05.SI", "reason": "n'
    'ot linked to a Claire run (booked from the broker)", "details": [], '
    '"approved_at": null, "closes": null, "lot": {"opened": 60.0, "remain'
    'ing": 0.0, "cost": 35.0, "realized": 55.74, "closed_qty": 60.0, "clo'
    'sed_by": ["ex_000000000030"]}}, {"id": "ex_000000000024", "order_id"'
    ': "o31", "account_id": "us", "side": "buy", "qty": 100.0, "price": 0'
    '.7, "currency": "GBP", "fees": 1.0, "other_fees": 0.0, "gross": 88.9'
    ', "net": 89.9, "executed_at": 1786543740, "work_item_id": "wi_v1", "'
    'ticker": "VOD", "exchange": "LSE", "name": "VOD plc", "leg": null, "'
    'order_limit": null, "order_stop": null, "parent_order_id": null, "ac'
    'count": "Claire sim \\u00b7 alpaca (simulated, not a broker)", "base_'
    'currency": "USD", "symbol": "VOD.L", "reason": "screen: cheap teleco'
    'ms", "details": [], "approved_at": null, "closes": null, "lot": {"op'
    'ened": 100.0, "remaining": 100.0, "cost": 0.889, "realized": null, "'
    'closed_qty": null, "closed_by": [], "last": null}}, {"id": "ex_00000'
    '0000022", "order_id": "o29", "account_id": "sg", "side": "buy", "qty'
    '": 20.0, "price": 15.0, "currency": "EUR", "fees": 2.0, "other_fees"'
    ': 0.0, "gross": 450.0, "net": 452.0, "executed_at": 1786543620, "wor'
    'k_item_id": null, "ticker": "ACA", "exchange": "PARIS", "name": "ACA'
    ' plc", "leg": null, "order_limit": null, "order_stop": null, "parent'
    '_order_id": null, "account": "Saxo SIM (paper)", "base_currency": "S'
    'GD", "symbol": "ACA.PA", "reason": "not linked to a Claire run (book'
    'ed from the broker)", "details": [], "approved_at": null, "closes": '
    'null, "lot": {"opened": 20.0, "remaining": 20.0, "cost": 22.5, "real'
    'ized": null, "closed_qty": null, "closed_by": [], "last": 16.2}}, {"'
    'id": "ex_000000000021", "order_id": "o27", "account_id": "us", "side'
    '": "buy", "qty": 2.0, "price": 240.0, "currency": "USD", "fees": 1.0'
    ', "other_fees": 0.0, "gross": 480.0, "net": 481.0, "executed_at": 17'
    '86543500, "work_item_id": null, "ticker": "TSLA", "exchange": "NASDA'
    'Q", "name": "TSLA plc", "leg": null, "order_limit": null, "order_sto'
    'p": null, "parent_order_id": null, "account": "Claire sim \\u00b7 alp'
    'aca (simulated, not a broker)", "base_currency": "USD", "symbol": "T'
    'SLA", "reason": "not linked to a Claire run (booked from the broker)'
    '", "details": [], "approved_at": null, "closes": {"pl": 18.714286, "'
    'cost": 480.0, "qty": 2.0, "held_days": 0, "pct": 3.898809583333333},'
    ' "lot": null}, {"id": "ex_000000000019", "order_id": "o24", "account'
    '_id": "us", "side": "sell", "qty": 7.0, "price": 250.0, "currency": '
    '"USD", "fees": 1.0, "other_fees": 0.0, "gross": 1750.0, "net": 1749.'
    '0, "executed_at": 1786543320, "work_item_id": null, "ticker": "TSLA"'
    ', "exchange": "NASDAQ", "name": "TSLA plc", "leg": null, "order_limi'
    't": 250.0, "order_stop": 262.0, "parent_order_id": null, "account": '
    '"Claire sim \\u00b7 alpaca (simulated, not a broker)", "base_currency'
    '": "USD", "symbol": "TSLA", "reason": "not linked to a Claire run (b'
    'ooked from the broker)", "details": [], "approved_at": null, "closes'
    '": null, "lot": {"opened": 7.0, "remaining": 5.0, "cost": 250.0, "re'
    'alized": 18.714286, "closed_qty": 2.0, "closed_by": ["ex_00000000002'
    '1"], "last": 245.0, "unrealized": 25.0}}, {"id": "ex_000000000018", '
    '"order_id": "o17", "account_id": "sg", "side": "sell", "qty": 1.0, "'
    'price": 115.0, "currency": "USD", "fees": 2.0, "other_fees": 0.0, "g'
    'ross": 154.1, "net": 152.1, "executed_at": 1786458060, "work_item_id'
    '": "wi_c1", "ticker": "CSCO", "exchange": "NASDAQ", "name": "CSCO pl'
    'c", "leg": "target", "order_limit": 115.0, "order_stop": null, "pare'
    'nt_order_id": "o13", "account": "Saxo SIM (paper)", "base_currency":'
    ' "SGD", "symbol": "CSCO", "reason": "Target reached \\u2014 the take-'
    'profit order at 115 sold the shares at the broker automatically", "d'
    'etails": ["protecting the buy from run wi_c1"], "approved_at": null,'
    ' "closes": {"pl": 14.61, "cost": 136.99, "qty": 1.0, "held_days": 0,'
    ' "pct": 10.665012044674793}, "lot": null}, {"id": "ex_000000000017",'
    ' "order_id": "o20", "account_id": "sg", "side": "sell", "qty": 2.0, '
    '"price": 98.0, "currency": "USD", "fees": 2.0, "other_fees": 0.0, "g'
    'ross": 262.64, "net": 260.64, "executed_at": 1786458000, "work_item_'
    'id": "wi_c2", "ticker": "CSCO", "exchange": "NASDAQ", "name": "CSCO '
    'plc", "leg": "stop", "order_limit": null, "order_stop": 98.0, "paren'
    't_order_id": "o18", "account": "Saxo SIM (paper)", "base_currency": '
    '"SGD", "symbol": "CSCO", "reason": "Stop-loss hit \\u2014 the protect'
    'ive stop at 98 sold the shares at the broker automatically", "detail'
    's": ["protecting the buy from run wi_c2"], "approved_at": null, "clo'
    'ses": {"pl": -14.34, "cost": 273.98, "qty": 2.0, "held_days": 0, "pc'
    't": -5.233958683115556}, "lot": null}, {"id": "ex_000000000015", "or'
    'der_id": "o18", "account_id": "sg", "side": "buy", "qty": 6.0, "pric'
    'e": 104.0, "currency": "USD", "fees": 2.0, "other_fees": 0.0, "gross'
    '": 836.16, "net": 838.16, "executed_at": 1786457820, "work_item_id":'
    ' "wi_c2", "ticker": "CSCO", "exchange": "NASDAQ", "name": "CSCO plc"'
    ', "leg": null, "order_limit": 104.0, "order_stop": 98.0, "parent_ord'
    'er_id": null, "account": "Saxo SIM (paper)", "base_currency": "SGD",'
    ' "symbol": "CSCO", "reason": "Claire\'s desk recommended this buy and'
    ' you approved it", "details": [], "approved_at": null, "closes": nul'
    'l, "lot": {"opened": 6.0, "remaining": 6.0, "cost": 139.36, "realize'
    'd": null, "closed_qty": null, "closed_by": [], "last": 105.5, "unrea'
    'lized": 12.059999999999945}}, {"id": "ex_000000000013", "order_id": '
    '"o13", "account_id": "sg", "side": "buy", "qty": 5.0, "price": 103.2'
    ', "currency": "USD", "fees": 0.0, "other_fees": 0.0, "gross": 686.28'
    ', "net": 686.28, "executed_at": 1786457580, "work_item_id": "wi_c1",'
    ' "ticker": "CSCO", "exchange": "NASDAQ", "name": "CSCO plc", "leg": '
    'null, "order_limit": 103.0, "order_stop": 97.0, "parent_order_id": n'
    'ull, "account": "Saxo SIM (paper)", "base_currency": "SGD", "symbol"'
    ': "CSCO", "reason": "Claire\'s desk recommended this buy and you appr'
    'oved it", "details": [], "approved_at": null, "closes": null, "lot":'
    ' {"opened": 5.0, "remaining": 5.0, "cost": 137.256, "realized": null'
    ', "closed_qty": null, "closed_by": [], "last": 105.5, "unrealized": '
    '20.57000000000002}}, {"id": "ex_000000000011", "order_id": "o13", "a'
    'ccount_id": "sg", "side": "buy", "qty": 4.0, "price": 103.0, "curren'
    'cy": "USD", "fees": 2.0, "other_fees": 0.0, "gross": 547.96, "net": '
    '549.96, "executed_at": 1786457520, "work_item_id": "wi_c1", "ticker"'
    ': "CSCO", "exchange": "NASDAQ", "name": "CSCO plc", "leg": null, "or'
    'der_limit": 103.0, "order_stop": 97.0, "parent_order_id": null, "acc'
    'ount": "Saxo SIM (paper)", "base_currency": "SGD", "symbol": "CSCO",'
    ' "reason": "Claire\'s desk recommended this buy and you approved it",'
    ' "details": [], "approved_at": null, "closes": null, "lot": {"opened'
    '": 4.0, "remaining": 1.0, "cost": 136.99, "realized": 0.27, "closed_'
    'qty": 3.0, "closed_by": ["ex_000000000017", "ex_000000000018"], "las'
    't": 105.5, "unrealized": 4.3799999999999955}}, {"id": "ex_0000000000'
    '09", "order_id": "o11", "account_id": "sg", "side": "buy", "qty": 3.'
    '0, "price": 101.0, "currency": "USD", "fees": 2.0, "other_fees": 0.0'
    ', "gross": 409.05, "net": 411.05, "executed_at": 1786457400, "work_i'
    'tem_id": "wi_o1", "ticker": "NVDA", "exchange": "NASDAQ", "name": "N'
    'VDA plc", "leg": null, "order_limit": null, "order_stop": null, "par'
    'ent_order_id": null, "account": "Saxo SIM (paper)", "base_currency":'
    ' "SGD", "symbol": "NVDA", "reason": "The desk passed on it (41% conv'
    'iction) \\u2014 you overrode it and approved the trade", "details": ['
    '"your note: I like it", "fundamentals: priced for perfection"], "app'
    'roved_at": 1786457281, "closes": null, "lot": {"opened": 3.0, "remai'
    'ning": 3.0, "cost": 136.35, "realized": null, "closed_qty": null, "c'
    'losed_by": [], "last": 120.0, "unrealized": 73.35000000000005}}, {"i'
    'd": "ex_000000000008", "order_id": "o09", "account_id": "us", "side"'
    ': "sell", "qty": 2.0, "price": 121.0, "currency": "USD", "fees": 1.0'
    ', "other_fees": 0.0, "gross": 242.0, "net": 241.0, "executed_at": 17'
    '86457280, "work_item_id": "wi_s1", "ticker": "NVDA", "exchange": "NA'
    'SDAQ", "name": "NVDA plc", "leg": null, "order_limit": null, "order_'
    'stop": null, "parent_order_id": null, "account": "Claire sim \\u00b7 '
    'alpaca (simulated, not a broker)", "base_currency": "USD", "symbol":'
    ' "NVDA", "reason": "Sell review after a price alert, approved by you'
    ' \\u2014 watcher: trailing alert hit \\u2014 last 119.00", "details": '
    '["technicals: momentum fading"], "approved_at": 1786457045, "closes"'
    ': {"pl": 40.8, "cost": 200.0, "qty": 2.0, "held_days": 0, "pct": 20.'
    '4}, "lot": null}, {"id": "ex_000000000007", "order_id": "o07", "acco'
    'unt_id": "us", "side": "sell", "qty": 4.0, "price": 120.0, "currency'
    '": "USD", "fees": 1.0, "other_fees": 0.0, "gross": 480.0, "net": 479'
    '.0, "executed_at": 1786457160, "work_item_id": "wi_s1", "ticker": "N'
    'VDA", "exchange": "NASDAQ", "name": "NVDA plc", "leg": null, "order_'
    'limit": null, "order_stop": null, "parent_order_id": null, "account"'
    ': "Claire sim \\u00b7 alpaca (simulated, not a broker)", "base_curren'
    'cy": "USD", "symbol": "NVDA", "reason": "Sell review after a price a'
    'lert, approved by you \\u2014 watcher: trailing alert hit \\u2014 last'
    ' 119.00", "details": ["technicals: momentum fading"], "approved_at":'
    ' 1786457045, "closes": {"pl": 78.6, "cost": 400.0, "qty": 4.0, "held'
    '_days": 0, "pct": 19.65}, "lot": null}, {"id": "ex_000000000005", "o'
    'rder_id": "o05", "account_id": "us", "side": "buy", "qty": 5.0, "pri'
    'ce": 110.0, "currency": "USD", "fees": 1.0, "other_fees": 0.0, "gros'
    's": 550.0, "net": 551.0, "executed_at": 1786457040, "work_item_id": '
    '"wi_m1", "ticker": "NVDA", "exchange": "NASDAQ", "name": "NVDA plc",'
    ' "leg": null, "order_limit": null, "order_stop": null, "parent_order'
    '_id": null, "account": "Claire sim \\u00b7 alpaca (simulated, not a b'
    'roker)", "base_currency": "USD", "symbol": "NVDA", "reason": "Your m'
    'anual buy 5 NVDA at 110 \\u2014 approved by you", "details": [], "app'
    'roved_at": null, "closes": null, "lot": {"opened": 5.0, "remaining":'
    ' 5.0, "cost": 110.0, "realized": null, "closed_qty": null, "closed_b'
    'y": [], "last": 120.0, "unrealized": 50.0}}, {"id": "ex_000000000003'
    '", "order_id": "o03", "account_id": "us", "side": "buy", "qty": 10.0'
    ', "price": 100.0, "currency": "USD", "fees": 1.0, "other_fees": 0.0,'
    ' "gross": 1000.0, "net": 1001.0, "executed_at": 1786456920, "work_it'
    'em_id": "wi_p1", "ticker": "NVDA", "exchange": "NASDAQ", "name": "NV'
    'DA plc", "leg": null, "order_limit": null, "order_stop": null, "pare'
    'nt_order_id": null, "account": "Claire sim \\u00b7 alpaca (simulated,'
    ' not a broker)", "base_currency": "USD", "symbol": "NVDA", "reason":'
    ' "Claire\'s desk recommended this buy (72% conviction) and you approv'
    'ed it", "details": ["fundamentals: earnings beat", "technicals: abov'
    'e the 50-day"], "approved_at": 1786456820, "closes": null, "lot": {"'
    'opened": 10.0, "remaining": 4.0, "cost": 100.0, "realized": 119.4, "'
    'closed_qty": 6.0, "closed_by": ["ex_000000000007", "ex_000000000008"'
    '], "last": 120.0, "unrealized": 80.0}}], "summary": {"by_ccy": [{"cc'
    'y": "SGD", "realized": 373.85, "unrealized": 110.36000000000001, "fe'
    'es": 22.42}, {"ccy": "USD", "realized": 138.114286, "unrealized": 15'
    '5.0, "fees": 7.0}], "wins": 6, "losses": 1, "buys": 11, "sells": 7, '
    '"priced": false}, "days": 30, "now": 1786719600}'
)


def _as_python(name, text, width=68):
    lines = [f"{name} = ("]
    lines += ["    " + repr(text[at:at + width])
              for at in range(0, len(text), width)]
    return "\n".join(lines + [")"])


if __name__ == "__main__":
    patch = pytest.MonkeyPatch()
    _steady_ids(patch)
    _steady_today(patch)
    fresh = full_book()
    print(_as_python("PORTFOLIO_BEFORE", portfolio_json(fresh)), end="\n\n")
    print(_as_python("TRADES_BEFORE", trades_json(fresh)))
    patch.undo()
