"""Trades page, alert clean-up and clearable Mission Control warnings
(2026-11-06: "I can not see anywhere that I sold CSCO" — its stop sold it at
the broker; the alert stayed on the Portfolio page as if it watched it)."""
import httpx

from app.watcher import Watcher
from tests.test_protective_legs import T0, World, dash  # noqa: F401 — fixture


def trail_rows(conn):
    return conn.execute("SELECT * FROM price_alerts WHERE rule='trail_pct'"
                        ).fetchall()


def watcher(w):
    return Watcher(w.conn, w.repo, w.market,
                   lambda inst, trigger=None: "x", clock=w.clock)


def stop_out(w):
    stop = [l for l in w.legs() if l["leg"] == "stop"][0]
    w.venue.fill(stop["broker_order_id"], 9, "96.90")
    w.custodian.run_once()


# ── the alert goes with the shares ──────────────────────────────────────
def test_a_sold_position_loses_its_price_alert():
    w = World()
    w.buy_with_legs()
    watcher(w).tick()
    assert len(trail_rows(w.conn)) == 1
    stop_out(w)
    assert w.held() == 0
    watcher(w).tick()
    assert trail_rows(w.conn) == []


def test_the_alert_survives_while_a_buy_is_still_working():
    """execute writes the approval's own trailing % before the fill; the
    watcher must not delete it in the gap (it would come back as 8%)."""
    w = World()
    w.buy_with_legs(fill=False)                 # entry placed, not filled
    w.conn.execute("INSERT INTO price_alerts (instrument_id, account_id,"
                   " side, rule, threshold, armed) VALUES"
                   " ('NASDAQ:CSCO', ?, 1, 'trail_pct', 12, 1)", (w.acct,))
    watcher(w).tick()
    (r,) = trail_rows(w.conn)
    assert r["threshold"] == 12


# ── the Trades page ─────────────────────────────────────────────────────
def test_trades_lists_the_buy_and_the_stop_sell_with_pl_and_reason(dash):
    base, w, key = dash
    stop_out(w)
    d = httpx.get(base + "/api/trades", headers={"X-Dash-Key": key}).json()
    by = {t["side"]: t for t in d["trades"]}
    sell, buy = by["sell"], by["buy"]
    assert sell["leg"] == "stop" and sell["price"] == 96.9
    assert "Stop-loss hit" in sell["reason"] and "97" in sell["reason"]
    assert round(sell["closes"]["pl"], 2) == -54.9
    assert "recommended" in buy["reason"]       # a pipeline run, approved
    assert buy["lot"]["remaining"] == 0
    # money is totalled per account currency, never across accounts (B5)
    (usd,) = d["summary"]["by_ccy"]
    assert usd["ccy"] == "USD" and round(usd["realized"], 2) == -54.9
    assert "realized" not in d["summary"]
    assert d["summary"]["losses"] == 1 and d["summary"]["sells"] == 1
    assert httpx.get(base + "/trades").status_code == 200


def test_an_open_buy_shows_unrealized_pl(dash):
    base, w, key = dash
    d = httpx.get(base + "/api/trades", headers={"X-Dash-Key": key}).json()
    (buy,) = d["trades"]
    assert buy["lot"]["remaining"] == 9
    assert buy["lot"]["last"] == 101                # the fake market's price
    assert round(buy["lot"]["unrealized"], 2) == -18.0     # 9 × (101 − 103)
    assert round(d["summary"]["by_ccy"][0]["unrealized"], 2) == -18.0


def test_orders_carry_what_a_leg_would_make_and_the_session(dash):
    base, w, key = dash
    d = httpx.get(base + "/api/orders", headers={"X-Dash-Key": key}).json()
    assert d["legs"] and all(l["entry_price"] == 103 for l in d["legs"])
    assert all("is_open" in l and "next_open" in l for l in d["legs"])


# ── clearable Mission Control warnings ──────────────────────────────────
def fail(w, wi, ts):
    w.conn.execute("INSERT INTO work_items (id, kind, ticker, state,"
                   " thread_id, created_at, updated_at) VALUES"
                   " (?, 'pipeline', 'CSCO', 'failed', ?, ?, ?)",
                   (wi, wi, ts, ts))


def test_a_cleared_failed_runs_warning_returns_only_for_new_failures(dash):
    base, w, key = dash
    fail(w, "wi_f1", T0 - 60)
    keys = lambda: [f["key"] for f in httpx.get(              # noqa: E731
        base + "/api/overview").json()["flag_items"]]
    assert "failed_runs" in keys()
    r = httpx.post(base + "/api/flag-ack", json={"keys": ["failed_runs"]},
                   headers={"Origin": base})
    assert r.status_code == 403                   # the key is required
    r = httpx.post(base + "/api/flag-ack", json={"keys": ["failed_runs"]},
                   headers={"X-Dash-Key": key, "Origin": base})
    assert r.status_code == 200
    assert "failed_runs" not in keys()
    fail(w, "wi_f2", T0 + 60)                     # a NEW failure
    assert "failed_runs" in keys()


def test_a_cleared_warning_can_be_shown_again(dash):
    base, w, key = dash
    w.conn.execute("INSERT INTO service_health (service, checked_at, ok,"
                   " detail) VALUES ('watcher', ?, 'degraded', '{}')", (T0,))
    ov = httpx.get(base + "/api/overview").json()
    (f,) = [f for f in ov["flag_items"] if "watcher" in f["text"]]
    hdr = {"X-Dash-Key": key, "Origin": base}
    httpx.post(base + "/api/flag-ack", json={"keys": [f["key"]]}, headers=hdr)
    ov = httpx.get(base + "/api/overview").json()
    assert f["key"] not in [x["key"] for x in ov["flag_items"]]
    assert f["key"] in [x["key"] for x in ov["cleared"]]
    httpx.post(base + "/api/flag-ack", json={"keys": [f["key"]],
                                             "restore": True}, headers=hdr)
    ov = httpx.get(base + "/api/overview").json()
    assert f["key"] in [x["key"] for x in ov["flag_items"]]


# ── a slow Watchlist (2026-11-06: ~20 s per load) ───────────────────────
def test_a_suffixed_ticker_is_not_suffixed_twice():
    from app.tools.market import desk_ticker, yahoo_symbol
    assert yahoo_symbol("APC.DE", "XETRA") == "APC.DE"
    assert yahoo_symbol("APC", "XETRA") == "APC.DE"
    assert desk_ticker("0R37.L", "LSE") == "0R37"
    assert desk_ticker("IBM", "NYSE") == "IBM"


def test_yahoo_refusing_us_pauses_further_calls():
    import httpx as hx
    import pytest
    from app.tools.market import Market, MarketError
    calls = []
    m = Market()

    def once(url, params=None):
        calls.append(url)
        req = hx.Request("GET", url)
        raise hx.HTTPStatusError("429", request=req,
                                 response=hx.Response(429, request=req))
    m._http_get_once = once
    with pytest.raises(hx.HTTPStatusError):
        m._http_get("https://query1.finance.yahoo.com/x")
    with pytest.raises(MarketError, match="rate-limiting"):
        m._http_get("https://query1.finance.yahoo.com/y")
    assert len(calls) == 1                      # the second never went out


def test_recent_saxo_bars_skip_the_yahoo_trip():
    from app.tools.market import Market
    yahoo, saxo = [], []

    def get(url, params=None):
        yahoo.append(url)
        raise RuntimeError("429")

    def fb(sym, r, i):
        saxo.append(sym)
        return {"symbol": sym, "close": [1, 2]}
    m = Market(_get=get, chart_fallback=fb)
    assert m.chart("DDOG")["close"] == [1, 2]
    n = len(yahoo)
    assert m.chart("DDOG")["close"] == [1, 2]
    assert len(yahoo) == n and len(saxo) == 1   # served from the Saxo cache


def test_watchlist_rows_stored_with_a_suffix_are_repaired(dash):
    base, w, key = dash
    w.conn.execute("INSERT INTO watchlist (ticker, exchange, added_at)"
                   " VALUES ('APC.DE', 'XETRA', 1)")
    from app.watchlist import _fix_suffixed
    _fix_suffixed(w.conn)
    assert [tuple(r) for r in w.conn.execute(
        "SELECT ticker, exchange FROM watchlist")] == [("APC", "XETRA")]


# ── buy at the current price (a limit 1% through the live price) ───────
def at_market_state(w, wi, **kw):
    st = w.state(wi, "buy", size=1000.0, **kw)
    return st.model_copy(update={"approval": st.approval.model_copy(
        update={"order_type": "market"})})


def test_a_market_buy_is_sent_as_a_limit_just_above_the_live_price():
    w = World()                                     # live price 103
    out = w.execute(at_market_state(w, "wi_m", entry=95.0, stop=90.0,
                                    target=115.0))
    o = w.repo.order(out["order_ids"][0])
    assert o["limit_price"] == 104_030_000          # 103 × 1.01
    assert o["qty"] == 9_000_000                    # 1000 / 103 → 9 shares
    assert out["thesis"].entry_high == 104.03       # the plan follows
    assert o["stop_loss"] == 90_000_000 and o["take_profit"] == 115_000_000


def test_a_market_buy_whose_price_passed_the_target_is_refused():
    import pytest
    w = World()
    with pytest.raises(ValueError, match="no longer makes sense"):
        w.execute(at_market_state(w, "wi_m2", entry=95.0, stop=90.0,
                                  target=100.0))    # 104.03 > target 100
    assert w.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


def test_order_type_is_kept_through_the_approval_event():
    from app.api.desk import clean_suggested
    assert clean_suggested({"order_type": "market"}) == {"order_type": "market"}
    import pytest
    with pytest.raises(ValueError):
        clean_suggested({"order_type": "stop"})
