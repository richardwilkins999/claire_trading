"""Display correctness (REVIEW-2026-11-11 B5, B7, B12, B13).

B5   totals added SGD and USD accounts into one unlabelled number
B7   the Trading feed showed the oldest 300 events, for good
B12  Markets sent a search hit out under whichever exchange was selected
B13  a page function replaced a shared one of the same name
"""
import json
import re
from decimal import Decimal
from pathlib import Path

import pytest

from app import portfolio
from app.accounting.money import to_micro
from app.accounting.repo import Repo
from tests.test_dashboards import paired, web  # noqa: F401 — fixture
from tests.test_portfolio import T0, FakeMarket, book  # noqa: F401 — fixture

WEB = Path(__file__).resolve().parents[1] / "web"


# ── B5: one figure per currency ─────────────────────────────────────────────
class TwoCurrencyMarket(FakeMarket):
    PRICES = {"NVDA": ("120", "USD"), "D05.SI": ("33", "SGD")}

    def metrics(self, symbols):
        out = super().metrics(symbols)
        for s in symbols:
            px, ccy = self.PRICES.get(s, (None, None))
            if px is None:
                out.pop(s, None)
            else:
                out[s].update(price=Decimal(px), currency=ccy)
        return out


@pytest.fixture
def two_books(book):                                    # noqa: F811
    """The USD book of test_portfolio plus an SGD account holding D05."""
    r = Repo(book)
    r.create_account("sg", "saxo", "paper", "SGD",
                     fee_model={"type": "flat", "per_trade": "0"}, ts=T0)
    r.add_instrument("SGX:D05", "D05", "SGX", "SGD")
    r.deposit("sg", to_micro("5000"), ts=T0)
    r.create_order(id="o2", account_id="sg", instrument_id="SGX:D05",
                   side="buy", qty=to_micro("100"), expires_at=T0 + 999,
                   ts=T0)
    r.record_fill("o2", broker_fill_id="f2", qty=to_micro("100"),
                  price_native=to_micro("30"), fx_rate=to_micro("1"), ts=T0)
    return book


def test_totals_are_per_currency_and_never_added(two_books):
    d = portfolio.build(two_books, TwoCurrencyMarket(), clock=lambda: T0 + 60)
    sgd, usd = d["totals"]
    assert (sgd["ccy"], usd["ccy"]) == ("SGD", "USD")       # sorted
    assert usd["value"] == 1200 and usd["unrealized"] == 200
    assert usd["cash"] == 8999 and usd["equity"] == 10199   # 10000-1000-1
    assert sgd["value"] == 3300 and sgd["unrealized"] == 300
    assert sgd["cash"] == 2000 and sgd["equity"] == 5300
    assert sgd["positions"] == usd["positions"] == 1
    # there is no single number for two currencies: 4500 was the old answer
    assert d["total_value"] is None


def test_a_single_currency_desk_still_has_one_total(book):  # noqa: F811
    d = portfolio.build(book, FakeMarket(), clock=lambda: T0 + 60)
    assert d["total_value"] == 1200
    assert [t["ccy"] for t in d["totals"]] == ["USD"]


def test_weights_are_within_one_currency(two_books):
    """1200 USD against 3300 SGD is not 27% / 73% of anything."""
    d = portfolio.build(two_books, TwoCurrencyMarket(), clock=lambda: T0 + 60)
    w = {p["ticker"]: p["weight_pct"] for ps in d["exchanges"].values()
         for p in ps}
    assert w == {"NVDA": 100.0, "D05": 100.0}


def test_an_unpriced_position_is_counted_not_hidden(two_books):
    class NoSgx(TwoCurrencyMarket):
        PRICES = {"NVDA": ("120", "USD")}
    d = portfolio.build(two_books, NoSgx(), clock=lambda: T0 + 60)
    sgd = d["totals"][0]
    assert sgd["unpriced"] == 1 and sgd["value"] == 0
    assert sgd["equity"] == 2000            # cash only — and the page says so


def test_pages_do_not_sum_across_currencies():
    index = (WEB / "index.html").read_text()
    port = (WEB / "portfolio.html").read_text()
    trades = (WEB / "trades.html").read_text()
    assert "o.cash.reduce" not in index and "perCcy(sumByCcy(o.cash" in index
    assert "fmt(d.total_value)" not in port and "a.equity, 0" not in port
    assert "perCcy(t, 'equity')" in port
    assert "s.realized" not in trades and "s.fees" not in trades
    assert "perCcy(by, 'realized'" in trades


# ── B7: the feed shows what happened last ───────────────────────────────────
def _events(conn, n):
    for i in range(n):
        conn.execute(
            "INSERT INTO events (item_id, ts, actor, from_state, to_state,"
            " payload) VALUES ('wi_1', ?, 'test', 'a', ?, '{}')",
            (T0 + i, f"step_{i}"))
    return [r[0] for r in conn.execute("SELECT id FROM events ORDER BY id")]


def test_feed_returns_the_latest_events_oldest_first(web):  # noqa: F811
    base, conn = web
    ids = _events(conn, 350)
    with paired(base) as c:
        d = c.get("/api/feed").json()
        got = [e["id"] for e in d["events"]]
        assert len(got) == 300 and got == ids[-300:]        # not ids[:300]
        assert d["last"] == ids[-1]
        # a poll asks only for what is new
        assert c.get(f"/api/feed?since={ids[-1]}").json() == {
            "events": [], "last": ids[-1]}
        tail = c.get(f"/api/feed?since={ids[-11]}").json()
        assert [e["id"] for e in tail["events"]] == ids[-10:]
        assert c.get("/api/feed?since=abc").status_code == 400


def test_trading_page_polls_incrementally():
    src = (WEB / "trading.html").read_text()
    assert "'?since=' + feedLast" in src
    assert "await api('/api/feed');" not in src


# ── B12 / B13: page scripts ─────────────────────────────────────────────────
def _names(src):
    return set(re.findall(
        r"^\s*(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(", src, re.M)) \
        | set(re.findall(r"^(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=", src,
                         re.M))


def test_no_page_redefines_a_shared_script_function():
    """A top-level function in a page REPLACES the same-named one from a
    script loaded earlier, for every script on that page. saxo.html's
    money() did that to orderform.js; orders.html's ago() to nav.js."""
    shared = {p.name: _names(p.read_text()) for p in WEB.glob("*.js")}
    clashes = []
    for page in sorted(WEB.glob("*.html")):
        src = page.read_text()
        inline = "\n".join(re.findall(r"<script>(.*?)</script>", src, re.S))
        for js in re.findall(r'<script src="/([\w.]+\.js)"', src):
            both = _names(inline) & shared.get(js, set())
            if both:
                clashes.append(f"{page.name} redefines {sorted(both)} "
                               f"from {js}")
    assert not clashes, clashes
    for a in shared:                          # nor two shared scripts
        for b in shared:
            assert a >= b or not (shared[a] & shared[b]), (a, b)


def test_markets_never_guesses_an_exchange():
    src = (WEB / "markets.html").read_text()
    assert "exchangeOf" not in src                  # fell back to NASDAQ
    assert "r.exchange || ex" not in src            # kept the venue's name
    assert "deskExchange(r)" in src
    assert "not one of the desk's markets" in src   # says why, offers nothing
    # one resolver, in the shared script; Watchlist's own copy is gone
    nav = (WEB / "nav.js").read_text()
    assert "function deskExchange(r)" in nav
    wl = (WEB / "watchlist.html").read_text()
    assert "function deskExchange" not in wl and "EX_ALIAS" not in wl
    assert "deskExchange(r)" in wl


def test_trades_api_shape_is_json_serialisable(web):        # noqa: F811
    base, _ = web
    with paired(base) as c:
        d = c.get("/api/trades").json()
    assert d["summary"]["by_ccy"] == [] and json.dumps(d)
