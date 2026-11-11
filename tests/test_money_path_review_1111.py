"""The four money-path fixes from REVIEW-2026-11-11.md.

B1  a sell review that failed to start was still counted as a fired alert
B2  an order cancelled at its TTL could lose a fill that landed just before
B3  FX: a stale rate was reused with no age limit, ahead of a fresh source
B6  the ledger's own consistency check was never run in production
"""
from decimal import Decimal

import pytest

from app.accounting import db
from app.accounting.money import to_micro
from app.accounting.repo import Repo
from app.custodian import Custodian
from app.dashboards import post_sell_review
from app.tools.brokers import BrokerError
from app.tools.market import Market, MarketError
from tests.test_watcher_trailing import T0, _desk, _rule


# ── B1: a failed launch is not a fire ───────────────────────────────────────
def _breach(w):
    """Seed the rule at 100, then drop 9% — past the 8% trailing alert."""
    w.tick()
    w.market.price = Decimal("91")


def _health(conn):
    return conn.execute("SELECT ok FROM service_health WHERE service="
                        "'watcher' ORDER BY rowid DESC LIMIT 1").fetchone()[0]


def test_b1_launch_returning_nothing_is_not_counted():
    conn, w, launched = _desk(Decimal("100"))
    working = w.start_sell_review
    w.start_sell_review = lambda inst, trigger=None: None
    _breach(w)
    r = w.tick()
    assert r["fired"] == []
    assert r["launch_failed"][0]["instrument"] == "NASDAQ:X"
    rule = _rule(conn)
    assert rule["fire_count_today"] in (0, None)    # the day's fire not spent
    assert rule["last_fired_at"] is None
    assert _health(conn) == "degraded"              # visible on its tile

    # claire-api is back: the SAME 8% breach fires — no further 3% needed
    w.start_sell_review = working
    r = w.tick()
    assert len(r["fired"]) == 1 and r["launch_failed"] == []
    assert len(launched) == 1 and "8" in launched[0]
    assert _rule(conn)["fire_count_today"] == 1
    assert _health(conn) == "ok"


def test_b1_launch_raising_is_caught_and_alerted_once_a_day():
    conn, w, _ = _desk(Decimal("100"))
    sent = []
    w.alert = lambda text, title="Claire": sent.append((title, text))

    def down(inst, trigger=None):
        raise RuntimeError("claire-api answered HTTP 503: restarting")
    w.start_sell_review = down
    _breach(w)
    r1 = w.tick()
    r2 = w.tick()                                   # still down ten min later
    assert "HTTP 503" in r1["launch_failed"][0]["error"]
    assert r2["launch_failed"] and r2["fired"] == []
    assert _rule(conn)["fire_count_today"] in (0, None)
    assert len(sent) == 1                           # not one per tick
    assert "NOT started" in sent[0][0] and "X" in sent[0][1]


def test_b1_escalation_on_an_open_review_still_counts():
    """The other branch of _fire is untouched: an already-open review is
    escalated and the fire is counted, with no launch at all."""
    conn, w, launched = _desk(Decimal("100"))
    _breach(w)
    w.tick()                                        # opens the review
    w.start_sell_review = lambda inst, trigger=None: None   # would fail now
    w.market.price = Decimal("88")                  # 12%: past 8% + 3%
    r = w.tick()
    assert r["fired"][0]["escalated"] is True and r["launch_failed"] == []
    assert _rule(conn)["fire_count_today"] == 2


class _Resp:
    def __init__(self, code, body):
        self.status_code, self._body, self.text = code, body, str(body)

    def json(self):
        return self._body


INST = {"ticker": "X", "exchange": "NASDAQ", "currency": "USD",
        "id": "NASDAQ:X"}


def test_b1_post_sell_review_raises_instead_of_returning_none():
    seen = {}

    def ok(url, json=None, timeout=None):
        seen.update(url=url, json=json, timeout=timeout)
        return _Resp(200, {"work_item_id": "wi_1"})
    assert post_sell_review("http://api", INST, "why", post=ok) == "wi_1"
    assert seen["url"] == "http://api/api/run"
    assert seen["json"]["kind"] == "sell_review"
    assert seen["json"]["trigger"] == "why" and seen["timeout"] == 10

    with pytest.raises(RuntimeError, match="HTTP 500"):
        post_sell_review("http://api", INST,
                         post=lambda *a, **k: _Resp(500, "boom"))
    with pytest.raises(RuntimeError, match="no work item"):
        post_sell_review("http://api", INST,
                         post=lambda *a, **k: _Resp(200, {}))

    def refused(*a, **k):
        raise ConnectionError("connection refused")
    with pytest.raises(ConnectionError):
        post_sell_review("http://api", INST, post=refused)


# ── B2: the venue's final word before an order is expired ───────────────────
class RaceVenue:
    """`before` is what the poll sees; `after` is the truth once the cancel
    has gone through (a fill that landed in between shows up only there)."""
    def __init__(self, before, after):
        self.before, self.after, self.cancelled = before, after, False
        self.status_error = None

    def order_status(self, oid):
        if not self.cancelled:
            return self.before
        if self.status_error:
            raise RuntimeError(self.status_error)
        return self.after

    def cancel(self, oid):
        if self.cancelled:                  # Saxo: 404 the second time
            raise BrokerError("HTTP 404 OrderNotFound")
        self.cancelled = True


def _ttl_world(before, after):
    conn = db.connect(":memory:")
    db.init(conn)
    repo = Repo(conn)
    repo.create_account("saxo-paper", "saxo", "paper", "USD",
                        fee_model={"type": "flat", "per_trade": "0"}, ts=T0)
    repo.deposit("saxo-paper", to_micro("10000"), ts=T0)
    repo.add_instrument("NYSE:PG", "PG", "NYSE", "USD")
    conn.execute(
        "INSERT INTO work_items (id, kind, ticker, state, thread_id,"
        " created_at, updated_at) VALUES ('wi_pg', 'pipeline', 'PG',"
        " 'executing', 'wi_pg', ?, ?)", (T0, T0))
    repo.create_order(id="o1", work_item_id="wi_pg", account_id="saxo-paper",
                      instrument_id="NYSE:PG", side="buy", qty=to_micro("5"),
                      limit_price=to_micro("162"), status="placed",
                      expires_at=T0 + 60, ts=T0, fx_rate=to_micro("1"))
    repo.mark_order("o1", "placed", ts=T0, broker_order_id="saxo-o1")
    venue = RaceVenue(before, after)
    clock = {"t": T0 + 120}                 # already past the TTL
    c = Custodian(conn, repo, {"saxo": venue}, resume_post=lambda b: (200, {}),
                  account_broker=lambda a: "saxo",
                  fx_rate_for=lambda i, a: to_micro("1"),
                  clock=lambda: clock["t"])
    return conn, repo, venue, c


def _item(conn):
    return conn.execute("SELECT state FROM work_items WHERE id='wi_pg'"
                        ).fetchone()[0]


WORKING = {"status": "placed", "fills": []}


def test_b2_fill_between_poll_and_cancel_is_booked():
    conn, repo, venue, c = _ttl_world(WORKING, {
        "status": "cancelled", "ended": "Cancelled",
        "fills": [{"fill_id": "f1", "qty": "2", "price": "161.5"}]})
    r = c.run_once()
    assert repo.position("saxo-paper", "NYSE:PG")[0] == to_micro("2")
    assert r["fills"] == 1
    assert repo.order("o1")["status"] == "expired"  # our TTL, not "cancelled"
    assert "o1" in r["order_expired"]
    assert _item(conn) == "done"                    # it kept the 2 shares
    repo.assert_invariants("saxo-paper")


def test_b2_full_fill_just_before_the_cancel_completes_the_order():
    conn, repo, venue, c = _ttl_world(WORKING, {
        "status": "filled",
        "fills": [{"fill_id": "f1", "qty": "5", "price": "162"}]})
    r = c.run_once()
    assert repo.position("saxo-paper", "NYSE:PG")[0] == to_micro("5")
    assert repo.order("o1")["status"] == "filled"
    assert "o1" not in r["order_expired"]
    assert _item(conn) == "done"


def test_b2_nothing_filled_expires_as_before():
    conn, repo, venue, c = _ttl_world(WORKING, {"status": "cancelled",
                                                "fills": []})
    r = c.run_once()
    assert repo.order("o1")["status"] == "expired"
    assert _item(conn) == "expired"
    assert not r["flags"]


def test_b2_unreadable_final_status_keeps_the_order_open():
    conn, repo, venue, c = _ttl_world(WORKING, {
        "status": "cancelled", "ended": "Cancelled",
        "fills": [{"fill_id": "f1", "qty": "2", "price": "161.5"}]})
    venue.status_error = "network blip"
    r = c.run_once()
    assert repo.order("o1")["status"] == "placed"   # NOT expired on a guess
    assert any("final status could not be read" in f for f in r["flags"])
    # next run the venue answers: the fill is booked, then the book closes
    venue.status_error = None
    c.run_once()
    assert repo.position("saxo-paper", "NYSE:PG")[0] == to_micro("2")
    assert repo.order("o1")["status"] in ("expired", "cancelled")
    assert _item(conn) == "done"


def test_b2_venue_still_working_after_the_cancel_is_left_open():
    conn, repo, venue, c = _ttl_world(WORKING, WORKING)
    r = c.run_once()
    assert repo.order("o1")["status"] == "placed"
    assert any("still reports it placed" in f for f in r["flags"])


# ── B3: FX rates ────────────────────────────────────────────────────────────
def _chart(rate):
    return {"chart": {"result": [{"timestamp": [1], "indicators": {"quote": [
        {"open": [1], "high": [1], "low": [1], "close": [rate],
         "volume": [0]}]}, "meta": {"currency": "USD"}}]}}


class _Sources:
    """Yahoo and the ECB, each switchable between a rate and an outage."""
    def __init__(self, yahoo=None, ecb=None):
        self.yahoo, self.ecb, self.t = yahoo, ecb, 1000

    def get(self, url, params=None):
        if self.yahoo is None:
            raise MarketError("throttled")
        return _chart(self.yahoo)

    def ecb_rate(self, a, b):
        if self.ecb is None:
            raise MarketError("ECB down")
        return self.ecb

    def market(self, primary=None):
        return Market(_get=self.get, fx_fallback=self.ecb_rate,
                      clock=lambda: self.t, primary=primary)


class _PrimaryFx:
    def __init__(self):
        self.rate = "0.7400"

    def supports(self, s):
        return True

    def fx(self, a, b):
        if self.rate is None:
            raise RuntimeError("429")
        return self.rate


def test_b3_a_fresh_rate_from_the_next_source_beats_a_stale_one():
    s, p = _Sources(yahoo=0.76), _PrimaryFx()
    m = s.market(primary=p)
    assert m.fx("SGD", "USD") == Decimal("0.7400")
    s.t += 400                              # cache expired
    p.rate = None                           # the keyed provider now fails
    # it used to hand back the provider's old 0.7400 without asking Yahoo
    assert m.fx("SGD", "USD") == Decimal("0.76")


def test_b3_every_source_down_reuses_the_last_rate_only_for_a_while():
    s = _Sources(yahoo=0.74)
    m = s.market()
    assert m.fx("SGD", "USD") == Decimal("0.74")
    s.yahoo = None                          # Yahoo and the ECB both down
    s.t += 3600
    assert m.fx("SGD", "USD") == Decimal("0.74")     # an hour old: tolerated
    assert ("fx", "SGDUSD=X") in m._stale_keys       # and shown as stale
    s.t = 1000 + Market.FX_MAX_AGE + 1
    with pytest.raises(MarketError, match="no SGD→USD rate"):
        m.fx("SGD", "USD")                  # too old to book a fill at
    s.yahoo = 0.75                          # a source comes back
    assert m.fx("SGD", "USD") == Decimal("0.75")
    assert ("fx", "SGDUSD=X") not in m._stale_keys


def test_b3_a_stale_chart_is_not_restamped_as_a_fresh_rate():
    s = _Sources(yahoo=0.74, ecb="0.78")
    m = s.market()
    assert m.fx("SGD", "USD") == Decimal("0.74")
    s.yahoo = None                          # chart() now serves its old copy
    s.t += 400
    assert m.fx("SGD", "USD") == Decimal("0.78")     # so the ECB is used
    # and the old chart never becomes a new cache entry: with everything
    # down long enough the rate runs out instead of living forever
    s.ecb = None
    for _ in range(12):
        s.t += 1800
        try:
            m.fx("SGD", "USD")
        except MarketError:
            break
    else:
        pytest.fail("a stale FX rate was still being served after 6 hours")


def test_b3_never_cached_and_all_down_still_raises():
    with pytest.raises(MarketError):
        _Sources().market().fx("JPY", "USD")
    assert _Sources().market().fx("USD", "USD") == Decimal(1)


# ── B6: the ledger check runs ───────────────────────────────────────────────
def test_b6_custodian_checks_the_ledger_every_run():
    conn, w, _ = _desk(Decimal("100"))
    c = Custodian(conn, w.repo, {}, resume_post=lambda b: (200, {}),
                  account_broker=lambda a: "alpaca",
                  fx_rate_for=lambda i, a: to_micro("1"), clock=lambda: T0)
    assert "_ledger" in Custodian.DUTIES
    assert not any("LEDGER" in f for f in c.run_once()["flags"])

    # one share vanishes from a lot without a closure to account for it
    conn.execute("UPDATE lots SET qty_remaining = qty_remaining - 1000000")
    flags = c.run_once()["flags"]
    assert any("LEDGER INCONSISTENT a" in f and "reconcile" in f
               for f in flags)
    row = conn.execute("SELECT ok FROM service_health WHERE service="
                       "'custodian' ORDER BY rowid DESC LIMIT 1").fetchone()
    assert row[0] == "degraded"


def test_b6_one_bad_account_does_not_hide_another():
    conn, w, _ = _desk(Decimal("100"))
    w.repo.create_account("b", "alpaca", "paper", "USD",
                          fee_model={"type": "flat", "per_trade": "0"}, ts=T0)
    conn.execute(
        "INSERT INTO executions (id, account_id, instrument_id, side, qty,"
        " price_native, currency, fx_rate, gross_base, net_base, executed_at,"
        " recorded_at) VALUES ('ex2','b','NASDAQ:X','buy',1000000,1000000,"
        " 'USD', 1000000, 1000000, 1000000, ?, ?)", (T0, T0))
    conn.execute(
        "INSERT INTO lots (id, open_execution_id, account_id, instrument_id,"
        " qty_opened, qty_remaining, cost_per_share_base,"
        " commission_allocated, opened_at) VALUES"
        " ('lot2','ex2','b','NASDAQ:X',1000000,1000000,1000000,-5,?)", (T0,))
    conn.execute("UPDATE lots SET qty_remaining = qty_remaining - 1000000"
                 " WHERE id='lot1'")
    c = Custodian(conn, w.repo, {}, resume_post=lambda b: (200, {}),
                  account_broker=lambda a: "alpaca",
                  fx_rate_for=lambda i, a: to_micro("1"), clock=lambda: T0)
    ledger = [f for f in c.run_once()["flags"] if "LEDGER" in f]
    assert len(ledger) == 2
    assert any(" a:" in f for f in ledger) and any(" b:" in f for f in ledger)
