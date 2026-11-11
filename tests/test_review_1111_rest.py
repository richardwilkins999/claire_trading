"""The rest of REVIEW-2026-11-11's bug list: B10 (Hong Kong tickers), B11
(the approval card's cash), B14 (2027 calendars), B15 (live caps) and the
small ones."""
import json
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from pydantic import ValidationError

from app import live_trading, portfolio, sessions
from app.accounting import db
from app.accounting.money import to_micro
from app.accounting.repo import Repo
from app.dashboards import _approval_account
from app.graph.state import Thesis
from app.tools import tradingview
from app.tools.market import (Market, canonical_symbol, desk_ticker, hk_code,
                              yahoo_symbol)
from tests.test_portfolio import T0, FakeMarket, book  # noqa: F401 — fixture

ROOT = Path(__file__).resolve().parents[1]


# ── B14: calendars reach 2027, and say when they will run out ───────────────
def test_every_exchange_has_2027_and_keeps_2026():
    assert set(sessions.coverage().values()) == {2027}
    for ex, s in sessions.DEFAULTS.items():
        assert any(d.startswith("2026-") for d in s.holidays), ex
        for d in s.holidays:                    # a weekend "closure" is a typo
            assert date.fromisoformat(d).weekday() < 5, (ex, d)
        for d, t in s.early_closes:
            assert date.fromisoformat(d).weekday() < 5, (ex, d)
            assert re.fullmatch(r"\d\d:\d\d", t)


@pytest.mark.parametrize("exchange,tz,when,expected", [
    ("NYSE", "America/New_York", (2027, 12, 24, 12, 0), False),  # Xmas obs.
    ("NYSE", "America/New_York", (2027, 11, 26, 12, 0), True),   # half day…
    ("NYSE", "America/New_York", (2027, 11, 26, 14, 0), False),  # …shut at 13
    ("NYSE", "America/New_York", (2027, 11, 24, 14, 0), True),   # ordinary Wed
    ("SGX", "Asia/Singapore", (2027, 2, 8, 10, 0), False),       # CNY obs.
    ("SGX", "Asia/Singapore", (2027, 2, 5, 14, 0), False),       # CNY eve pm
    ("SGX", "Asia/Singapore", (2027, 2, 5, 10, 0), True),
    ("HKEX", "Asia/Hong_Kong", (2027, 2, 9, 10, 0), False),      # LNY day 4
    ("LSE", "Europe/London", (2027, 12, 28, 10, 0), False),      # Boxing obs.
    ("TSE", "Asia/Tokyo", (2027, 3, 22, 10, 0), False),          # Equinox obs.
    ("NYSE", "America/New_York", (2026, 11, 26, 12, 0), False),  # 2026 intact
])
def test_2027_sessions(exchange, tz, when, expected):
    ts = datetime(*when, tzinfo=ZoneInfo(tz))
    assert sessions.is_open(exchange, ts) is expected


def test_calendar_check_warns_before_the_data_runs_out():
    assert sessions.calendar_check(date(2026, 11, 11))[0] == "ok"
    # a year known to be incomplete is flagged once its circular is due
    st, _, why = sessions.calendar_check(date(2026, 12, 20))
    assert st == "warn" and "NSE 2027" in why
    # 45 days before the tables end, with no 2028 in them
    st, value, why = sessions.calendar_check(date(2027, 11, 20))
    assert st == "warn" and "2027" in value and "2028" in why
    # and past the end it is not a warning any more
    st, _, why = sessions.calendar_check(date(2028, 1, 2))
    assert st == "bad" and "reads as an open day" in why


def test_the_maintainer_checks_the_calendars_every_day():
    from app.maintenance import Maintainer
    conn = db.connect(":memory:")
    db.init(conn)
    m = Maintainer(conn, clock=lambda: datetime(
        2028, 1, 3, tzinfo=ZoneInfo("UTC")).timestamp())
    (c,) = [c for c in m.checks() if c["key"] == "calendars"]
    assert c["status"] == "bad" and "2028" in c["value"]


# ── B10: one Hong Kong code ─────────────────────────────────────────────────
def test_hong_kong_codes_have_one_form():
    assert [hk_code(x) for x in ("861", "0861", "00861", "5", "0700")] == \
        ["0861", "0861", "0861", "0005", "0700"]
    assert hk_code("9988") == "9988" and hk_code("80700") == "80700"
    assert hk_code("HSBA") == "HSBA"                # not a number: untouched
    for t in ("861", "0861", "00861", "861.HK", "00861.hk"):
        assert yahoo_symbol(t, "HKEX") == "0861.HK", t
        assert desk_ticker(t, "HKEX") == "0861", t
    assert canonical_symbol("00861.HK") == "0861.HK"
    assert canonical_symbol("D05.SI") == "D05.SI"
    assert canonical_symbol("NVDA") == "NVDA"
    # other exchanges are exactly as before
    assert yahoo_symbol("APC.DE", "XETRA") == "APC.DE"
    assert yahoo_symbol("D05", "SGX") == "D05.SI"
    assert desk_ticker("APC.DE", "XETRA") == "APC"
    assert yahoo_symbol("NVDA", "NASDAQ") == "NVDA"


def test_market_answers_whatever_form_an_agent_improvises():
    asked = []

    def tv(symbols):
        asked.append(list(symbols))
        return {s: {"symbol": s, "price": Decimal("3.1"), "currency": "HKD",
                    "stale": False, "src": "tv"} for s in symbols}
    m = Market(_get=lambda *a, **k: 1 / 0, tv_quotes=tv, clock=lambda: 1000)
    got = m.spark(["00861.HK", "861.HK", "NVDA"])
    assert asked == [["0861.HK", "NVDA"]]           # one canonical lookup
    assert set(got) == {"00861.HK", "861.HK", "NVDA"}       # caller's names
    assert got["00861.HK"]["price"] == Decimal("3.1")
    assert m.quote("861.HK")["symbol"] == "0861.HK"


def test_scans_emit_padded_hong_kong_tickers():
    class C:
        def post(self, url, json=None):
            vals = {"name": "861", "description": "Digital China",
                    "close": 3.1}
            return httpx.Response(
                200, request=httpx.Request("POST", url),
                json={"totalCount": 1, "data": [{"s": "HKEX:861", "d": [
                    vals.get(c) for c in tradingview.LISTING_COLS]}]})
    row = tradingview.listing("HKEX", _client=C())["rows"][0]
    assert row["ticker"] == "0861" and row["symbol"] == "0861.HK"


# ── B11: the card shows what the gate will allow ────────────────────────────
def test_approval_card_cash_is_what_is_free_to_spend(book):  # noqa: F811
    repo = Repo(book)
    acct = book.execute("SELECT * FROM broker_accounts").fetchone()
    live = {"armed": False, "summary": "off"}
    before = _approval_account(acct, repo, {}, live)
    assert before["cash"] == 8999 and before["cash_reserved"] == 0
    # a buy is approved and working: its cash is promised, not spent
    repo.create_order(id="o9", account_id="acct", instrument_id="NASDAQ:NVDA",
                      side="buy", qty=to_micro("10"), expires_at=T0 + 999,
                      ts=T0, limit_price=to_micro("100"),
                      reserved_base=to_micro("1000"))
    after = _approval_account(acct, repo, {}, live)
    assert after["cash"] == 7999 and after["cash_reserved"] == 1000
    assert after["cash"] == repo.available_cash("acct") / 1e6   # the gate's
    html = (ROOT / "web" / "approvals.html").read_text()
    assert "held for open buy" in html


# ── B15: one copy of the live caps ──────────────────────────────────────────
def test_raising_a_live_cap_reaches_the_ledgers_own_check():
    conn = db.connect(":memory:")
    db.init(conn)
    repo = Repo(conn)
    repo.create_account(
        "saxo-live", "saxo_live", "live", "SGD",
        fee_model={"type": "flat", "per_trade": "0"},
        risk_limits={"max_order_base": 1000, "max_trades_per_day": 2,
                     "max_position_pct": 25},
        ts=T0, allow_live=True, external_ref="key")
    live_trading.set_limits(conn, {"max_order_base": 5000,
                                   "max_orders_per_day": 4})
    risk = json.loads(repo.account("saxo-live")["risk_limits"])
    assert risk["max_order_base"] == 5000       # was still 1000: silent cap
    assert risk["max_trades_per_day"] == 4
    assert risk["max_position_pct"] == 25       # other limits untouched
    # a paper account is never touched by the live caps
    repo.create_account("p", "alpaca", "paper", "USD",
                        fee_model={"type": "flat", "per_trade": "0"}, ts=T0)
    live_trading.set_limits(conn, {"max_order_base": 6000})
    assert json.loads(repo.account("p")["risk_limits"]) == {}


# ── the small ones ──────────────────────────────────────────────────────────
def test_average_cost_is_of_the_shares_still_held(book):  # noqa: F811
    """10 @ 100 are held. Buy 10 more @ 140, sell 10 (FIFO takes the 100s):
    what is left cost 140 a share, not the all-time average of 120."""
    r = Repo(book)
    for oid, side, px in (("o2", "buy", "140"), ("o3", "sell", "150")):
        r.create_order(id=oid, account_id="acct", instrument_id="NASDAQ:NVDA",
                       side=side, qty=to_micro("10"), expires_at=T0 + 999,
                       ts=T0 + 10)
        r.record_fill(oid, broker_fill_id="f" + oid, qty=to_micro("10"),
                      price_native=to_micro(px), fx_rate=to_micro("1"),
                      ts=T0 + 10)
    d = portfolio.build(book, FakeMarket(), clock=lambda: T0 + 60)
    p = d["exchanges"]["NASDAQ"][0]
    assert p["qty"] == 10
    assert p["levels"]["avg_cost_native"] == pytest.approx(140)


def test_thesis_levels_are_enforced_without_assert():
    src = (ROOT / "app" / "graph" / "state.py").read_text()
    assert not re.search(r"^\s+assert ", src, re.M)     # -O would drop them
    ok = dict(ticker="X", conviction=0.5, currency="USD", direction="buy",
              entry_low=99, entry_high=101, stop_loss=95, take_profit=120)
    Thesis(**ok)
    for bad, why in (({"stop_loss": 105}, "stop must be below entry"),
                     ({"take_profit": 90}, "ABOVE entry"),
                     ({"entry_low": 110}, "entry_low must not exceed")):
        with pytest.raises(ValidationError, match=why):
            Thesis(**{**ok, **bad})


def test_reset_clears_the_tables_that_hang_off_the_book():
    src = (ROOT / "bin" / "claire-reset").read_text()
    listed = set(re.findall(r'"([a-z_]+)"', re.search(
        r"TABLES = \[(.*?)\]", src, re.S).group(1)))
    assert {"sim_orders", "price_triggers", "telegram_notices",
            "arbiter_notes", "broker_sync", "dashboard_acks"} <= listed
    # and every one of them is a table one db.init() creates
    conn = db.connect(":memory:")
    db.init(conn)
    have = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert listed <= have


def test_the_approval_toast_can_spell():
    html = (ROOT / "web" / "approvals.html").read_text()
    assert "action + 'd ✓'" not in html and "reject: 'rejected'" in html


def test_indexes_cover_the_polled_queries():
    conn = db.connect(":memory:")
    db.init(conn)

    def plan(sql):
        return " ".join(r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql))
    assert "USING INDEX" in plan(
        "SELECT * FROM orders WHERE status IN ('placed','partially_filled')")
    assert "USING INDEX" in plan(
        "SELECT * FROM service_health WHERE service='watcher'"
        " ORDER BY checked_at DESC LIMIT 1")
    assert "USING INDEX" in plan(
        "SELECT * FROM work_items WHERE state='awaiting_approval'")
    assert "USING INDEX" in plan(
        "SELECT * FROM agent_runs WHERE started_at > 5")
