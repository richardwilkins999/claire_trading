"""Overturning a PASS verdict (Approvals page).

The desk declining is a decision, so overriding it goes through the SAME
money path as any approval: a gate, a minted token, a cash check. What the
human adds is the thing a pass verdict never carries — an entry and a floor.
"""
import json

import pytest

from app.accounting import db
from app.accounting.repo import Repo


T0 = 1_786_456_800
AT = "2026-08-12T10:00:00Z"


def _desk(tmp_path, thesis_direction="pass", state="done"):
    """A desk whose only run has already concluded with the given verdict."""
    from langgraph.checkpoint.memory import InMemorySaver
    from app.api.desk import Desk

    conn = db.connect(":memory:")
    db.init(conn)
    repo = Repo(conn)
    conn.execute("INSERT INTO instruments (id, ticker, exchange, currency)"
                 " VALUES ('NASDAQ:SBUX','SBUX','NASDAQ','USD')")

    from app.graph.pipeline import ANALYSTS, Deps
    # only the money half is exercised — an override never re-analyses
    deps = Deps(analysts={a: (lambda s: None) for a in ANALYSTS},
                bull=lambda s: None, bear=lambda s: None,
                arbiter=lambda s: None,
                execute=lambda s: {"order_ids": ["ord_1"]},
                record=lambda s: {})
    desk = Desk(conn, deps, InMemorySaver(), repo, clock=lambda: T0)

    repo.create_work_item("wi_pass", "pipeline", "SBUX", ts=T0)
    t = {"ticker": "SBUX", "direction": thesis_direction, "conviction": 0.28,
         "currency": "USD", "conditions": ["wait for a full-volume session"]}
    if thesis_direction != "pass":
        t.update(entry_low=100.0, entry_high=100.0, stop_loss=90.0)
    conn.execute("UPDATE work_items SET thesis_json=?, state=? WHERE id=?",
                 (json.dumps(t), state, "wi_pass"))
    conn.execute("INSERT INTO agent_reports (work_item_id, agent_id, kind,"
                 " payload, created_at) VALUES ('wi_pass','arbiter','thesis',"
                 " ?, ?)", (json.dumps(t), T0))
    return conn, repo, desk


# ── the override reaches the gate, and only the gate ────────────────────────
def test_override_sends_a_pass_to_the_approval_gate(tmp_path):
    conn, repo, desk = _desk(tmp_path)
    wi = desk.override("wi_pass", direction="buy", entry=106.0, stop=98.0,
                       target=120.0, note="turnaround is real",
                       background=False)
    row = conn.execute("SELECT * FROM work_items WHERE id=?", (wi,)).fetchone()
    assert row["state"] == "awaiting_approval"     # in front of the human
    assert row["approval_token"] and row["token_state"] == "minted"
    assert row["override_of"] == "wi_pass"
    assert row["kind"] == "override"
    t = json.loads(row["thesis_json"])
    assert t["direction"] == "buy" and t["entry_low"] == 106.0
    assert t["stop_loss"] == 98.0 and t["take_profit"] == 120.0
    # the original caveats survive and the override is stated in the thesis
    assert "wait for a full-volume session" in t["conditions"]
    assert any("HUMAN OVERRIDE" in c and "turnaround is real" in c
               for c in t["conditions"])
    # nothing was bought by overriding
    assert conn.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"] == 0
    # and it is on the record as a human act, not a system one
    ev = conn.execute("SELECT actor, to_state, payload FROM events WHERE"
                      " item_id=? AND to_state='override'", (wi,)).fetchone()
    assert ev["actor"] == "human"
    assert json.loads(ev["payload"])["origin"] == "wi_pass"


def test_the_original_verdict_is_untouched(tmp_path):
    conn, _, desk = _desk(tmp_path)
    before = dict(conn.execute("SELECT * FROM work_items WHERE id='wi_pass'"
                               ).fetchone())
    desk.override("wi_pass", direction="buy", entry=106.0, stop=98.0,
                  background=False)
    after = dict(conn.execute("SELECT * FROM work_items WHERE id='wi_pass'"
                              ).fetchone())
    assert after["state"] == before["state"] == "done"
    assert after["thesis_json"] == before["thesis_json"]


# ── the guards ──────────────────────────────────────────────────────────────
def test_a_stop_that_is_not_a_floor_is_refused(tmp_path):
    conn, _, desk = _desk(tmp_path)
    with pytest.raises(Exception) as e:            # Thesis validator
        desk.override("wi_pass", direction="buy", entry=100.0, stop=110.0,
                      background=False)
    assert "stop" in str(e.value).lower()
    with pytest.raises(Exception):
        desk.override("wi_pass", direction="buy", entry=100.0, stop=None,
                      background=False)
    assert conn.execute("SELECT COUNT(*) c FROM work_items WHERE"
                        " override_of IS NOT NULL").fetchone()["c"] == 0


def test_only_a_pass_can_be_overridden(tmp_path):
    _, _, desk = _desk(tmp_path, thesis_direction="buy")
    with pytest.raises(ValueError, match="only a pass"):
        desk.override("wi_pass", direction="buy", entry=106.0, stop=98.0,
                      background=False)


def test_a_running_item_cannot_be_overridden(tmp_path):
    _, _, desk = _desk(tmp_path, state="running")
    with pytest.raises(ValueError, match="still running"):
        desk.override("wi_pass", direction="buy", entry=106.0, stop=98.0,
                      background=False)


def test_one_live_override_per_verdict(tmp_path):
    _, _, desk = _desk(tmp_path)
    desk.override("wi_pass", direction="buy", entry=106.0, stop=98.0,
                  background=False)
    with pytest.raises(ValueError, match="already overridden"):
        desk.override("wi_pass", direction="buy", entry=107.0, stop=99.0,
                      background=False)


def test_unknown_item_is_refused(tmp_path):
    _, _, desk = _desk(tmp_path)
    with pytest.raises(ValueError, match="unknown"):
        desk.override("nope", direction="buy", entry=1.0, stop=0.5,
                      background=False)


# ── the override still has to be approved to become a position ─────────────
def test_approving_an_override_executes_through_the_normal_path(tmp_path):
    conn, _, desk = _desk(tmp_path)
    wi = desk.override("wi_pass", direction="buy", entry=106.0, stop=98.0,
                       background=False)
    row = conn.execute("SELECT * FROM work_items WHERE id=?", (wi,)).fetchone()
    desk.resume(wi, {"status": "approved", "size_base": 1000.0, "qty": None,
                     "trail_pct": 8.0, "broker": "alpaca", "actor": "human",
                     "token": row["approval_token"], "at": AT})
    after = conn.execute("SELECT state FROM work_items WHERE id=?",
                         (wi,)).fetchone()
    assert after["state"] == "executing"        # the gate-only graph executed
    # rejecting instead leaves nothing behind
    wi2_conn, _, desk2 = _desk(tmp_path)
    wi2 = desk2.override("wi_pass", direction="buy", entry=106.0, stop=98.0,
                         background=False)
    r2 = wi2_conn.execute("SELECT approval_token FROM work_items WHERE id=?",
                          (wi2,)).fetchone()
    desk2.resume(wi2, {"status": "rejected", "actor": "human",
                       "token": r2["approval_token"], "at": AT})
    assert wi2_conn.execute("SELECT state FROM work_items WHERE id=?",
                            (wi2,)).fetchone()["state"] == "rejected"


# ── the page shows the evidence on both halves ─────────────────────────────
def test_decided_runs_carry_full_agent_evidence(tmp_path):
    import threading
    import httpx
    from app.dashboards import create_server
    conn, repo, desk = _desk(tmp_path)
    repo.create_account("a", "alpaca", "paper", "USD",
                        fee_model={"type": "flat", "per_trade": "0"}, ts=T0)
    conn.execute("UPDATE work_items SET updated_at=? WHERE id='wi_pass'",
                 (T0,))

    class M:
        _stale_keys = set()

        def metrics(self, s):
            return {}
    srv = create_server(conn, repo, M(), clock=lambda: T0, port=0,
                        sse_interval=None, dash_key="k")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        d = httpx.get(f"http://127.0.0.1:{srv.server_address[1]}/api/approvals",
                      headers={"X-Dash-Key": "k"}, timeout=5).json()
        row = d["decided"][0]
        assert row["direction"] == "pass"
        assert row["reports"][0]["agent"] == "arbiter"   # evidence is there
        assert row["overridable"] is True
        assert row["overridden_by"] is None

        # once overridden, the offer is withdrawn and the card inherits the
        # evidence of the verdict it overturned
        wi = desk.override("wi_pass", direction="buy", entry=106.0, stop=98.0,
                           background=False)
        d = httpx.get(f"http://127.0.0.1:{srv.server_address[1]}/api/approvals",
                      headers={"X-Dash-Key": "k"}, timeout=5).json()
        assert d["decided"][0]["overridable"] is False
        assert d["decided"][0]["overridden_by"] == wi
        card = [c for c in d["cards"] if c["id"] == wi][0]
        assert card["override_of"] == "wi_pass"
        assert card["reports"][0]["agent"] == "arbiter"
    finally:
        srv.shutdown()


# ── stops: required for a buy or a short, not for an exit ────────────────
def _hold(conn, repo, shares="10"):
    from app.accounting.money import to_micro
    repo.create_account("a", "alpaca", "paper", "USD",
                        fee_model={"type": "flat", "per_trade": "0"}, ts=T0)
    repo.deposit("a", to_micro("10000"), ts=T0)
    repo.create_order(id="o", account_id="a", instrument_id="NASDAQ:SBUX",
                      side="buy", qty=to_micro(shares),
                      limit_price=to_micro("100"), status="placed",
                      expires_at=T0 + 3600, ts=T0)
    repo.record_fill("o", broker_fill_id="f", qty=to_micro(shares),
                     price_native=to_micro("100"), fx_rate=to_micro("1"),
                     ts=T0)


def test_a_buy_without_a_stop_is_refused(tmp_path):
    _, _, desk = _desk(tmp_path)
    with pytest.raises(ValueError, match="stop below"):
        desk.override("wi_pass", direction="buy", entry=100.0, stop=None,
                      background=False)


def test_a_sell_with_nothing_held_is_a_short_and_needs_a_stop(tmp_path):
    _, _, desk = _desk(tmp_path)
    with pytest.raises(ValueError, match="SHORT"):
        desk.override("wi_pass", direction="sell", entry=100.0, stop=None,
                      background=False)
    # with a stop above the entry it is accepted
    assert desk.override("wi_pass", direction="sell", entry=100.0,
                         stop=106.0, target=90.0, background=False)


def test_an_exit_of_held_shares_needs_no_stop(tmp_path):
    conn, repo, desk = _desk(tmp_path)
    _hold(conn, repo)
    assert desk.desk_held("SBUX") == 10
    wi = desk.override("wi_pass", direction="sell", entry=104.0, stop=None,
                       background=False)
    t = json.loads(conn.execute("SELECT thesis_json FROM work_items WHERE"
                                " id=?", (wi,)).fetchone()[0])
    assert t["direction"] == "sell" and t["stop_loss"] is None


def test_reference_levels_for_the_override_form():
    from app.dashboards import _levels
    # 30 bars: high = close + 1, low = close - 1, close rising by 1 a day
    closes = [100 + i for i in range(30)]
    c = {"close": closes, "high": [x + 1 for x in closes],
         "low": [x - 1 for x in closes]}
    L = _levels(c)
    assert L["sma20"] == sum(closes[-20:]) / 20
    assert L["low20"] == closes[-20] - 1 and L["high20"] == closes[-1] + 1
    # true range each day = max(2, |h-pc|=2, |l-pc|=0) = 2 → ATR 2
    assert L["atr14"] == 2.0
    assert L["sma50"] is None                       # not enough bars
    assert _levels({"close": [1, 2], "high": [1, 2], "low": [1, 2]}) == {}


def test_tradingview_levels_fallback_when_yahoo_bars_are_rate_limited():
    """C6L, 2026-11-04: Yahoo answered 429 for the daily bars, so there was
    no ATR and "use suggestion" silently did nothing. TradingView's scanner
    supplies the same levels."""
    from app.tools import tradingview

    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": [{"s": "SGX:C6L", "d": [
                "C6L", 0.0945, 6.6155, 6.6546, 6.92, 6.46, 6.92, 6.81]}]}

    class C:
        def post(self, *a, **k):
            return R()

    L = tradingview.levels("C6L.SI", _client=C())
    assert L["atr14"] == 0.0945 and L["low20"] == 6.46
    assert L["sma20"] == 6.6155 and L["src"] == "tradingview"


def test_hong_kong_tickers_map_between_yahoo_and_saxo():
    from app.tools.saxo_broker import desk_symbol
    assert desk_symbol("00700:xhkg") == "0700.HK"
    assert desk_symbol("09988:xhkg") == "9988.HK"
    assert desk_symbol("C6L:xses") == "C6L.SI"


def test_chart_falls_back_to_saxo_when_yahoo_refuses():
    """C6L.SI, 2026-11-04: Yahoo 429 on the daily bars; Twelve Data's free
    tier does not cover SGX. The chart must still draw, from Saxo."""
    from app.tools.market import Market

    def yahoo(url, params=None):
        raise RuntimeError("429 Too Many Requests")

    calls = []

    def saxo(sym, rng, iv):
        calls.append(sym)
        return {"symbol": sym, "timestamps": [1, 2], "open": [1, 2],
                "high": [1, 2], "low": [1, 2], "close": [6.79, 6.86],
                "volume": [5, 6], "currency": "SGD", "src": "saxo-cfd",
                "stale": False}

    m = Market(_get=yahoo, chart_fallback=saxo)
    c = m.chart("C6L.SI", "6mo", "1d")
    assert c["src"] == "saxo-cfd" and c["close"][-1] == 6.86
    m.chart("C6L.SI", "6mo", "1d")
    assert calls == ["C6L.SI"]                  # cached, not re-fetched

    import pytest
    with pytest.raises(RuntimeError):           # no fallback: error as before
        Market(_get=yahoo).chart("C6L.SI")


def test_override_carries_the_trailing_floor_to_the_card(tmp_path):
    """The Override form sets the trailing floor too (2026-11-07: it had
    only entry/stop/target, so the floor could not be chosen there)."""
    conn, _, desk = _desk(tmp_path)
    wi = desk.override("wi_pass", direction="buy", entry=106.0, stop=98.0,
                       trail_pct=12, background=False)
    row = conn.execute("SELECT suggested_json FROM work_items WHERE id=?",
                       (wi,)).fetchone()
    assert json.loads(row["suggested_json"]) == {"trail_pct": 12.0}
    ev = conn.execute("SELECT payload FROM events WHERE item_id=? AND"
                      " to_state='override'", (wi,)).fetchone()
    assert json.loads(ev["payload"])["trail_pct"] == 12.0


def test_override_without_a_floor_leaves_the_default(tmp_path):
    conn, _, desk = _desk(tmp_path)
    wi = desk.override("wi_pass", direction="buy", entry=106.0, stop=98.0,
                       background=False)
    assert conn.execute("SELECT suggested_json FROM work_items WHERE id=?",
                        (wi,)).fetchone()[0] is None


def test_override_api_rejects_a_silly_floor():
    from fastapi.testclient import TestClient

    from app.api.main import create_app

    class D:
        def override(self, *a, **k):
            raise AssertionError("must not be reached")
    client = TestClient(create_app(D(), None, "sec"))
    r = client.post("/internal/override", headers={"x-claire-secret": "sec"},
                    json={"work_item_id": "w", "direction": "buy",
                          "entry": 10, "stop": 9, "trail_pct": 95,
                          "actor": "human"})
    assert r.status_code == 422


def test_two_year_range_from_weekly_bars():
    from app.dashboards import _range
    c = {"timestamps": list(range(100, 112)), "high": [10 + i for i in range(12)],
         "low": [5 + i for i in range(12)]}
    c["low"][3] = 2.5
    r = _range(c)
    assert (r["high"], r["high_at"]) == (21, 111)
    assert (r["low"], r["low_at"]) == (2.5, 103)
    assert _range({"high": [1] * 3, "low": [1] * 3}) is None   # too little data


def test_override_prefills_shares_account_and_order_type(tmp_path):
    # the Approvals override form sends what the card should start with
    conn, repo, desk = _desk(tmp_path)
    wi = desk.override("wi_pass", direction="buy", entry=106.0, stop=98.0,
                       trail_pct=10, qty=9, broker="saxo",
                       order_type="market", background=False)
    sug = json.loads(conn.execute("SELECT suggested_json FROM work_items"
                                  " WHERE id=?", (wi,)).fetchone()[0])
    assert sug == {"qty": 9.0, "broker": "saxo", "trail_pct": 10.0,
                   "order_type": "market"}


def test_london_levels_in_pence_are_brought_to_the_quotes_pounds():
    # BNC 2026-11-10: quote 9.48 GBP, bars (Yahoo GBp / Saxo "GBP") in pence
    from app.dashboards import quote_units
    lv = {"sma20": 934.6, "sma50": 1014.54, "atr14": 31.4025, "low20": 841.0,
          "high20": 1034.0, "day_high": 951.0, "day_low": 938.0, "bars": 130}
    out = quote_units(lv, 9.48)
    assert out["sma20"] == 9.346 and out["atr14"] == 0.314025
    assert out["day_low"] == 9.38 and out["bars"] == 130
    r = quote_units({"high": 1120.0, "low": 351.0, "high_at": 1}, 9.48,
                    ("high", "low"))
    assert (r["high"], r["low"], r["high_at"]) == (11.2, 3.51, 1)
    # same units already (a US stock, or a stock that fell a lot): untouched
    assert quote_units({"sma20": 250.0, "atr14": 4.0}, 246.1) == \
        {"sma20": 250.0, "atr14": 4.0}
    assert quote_units({"sma20": 30.0}, 9.48) == {"sma20": 30.0}
    assert quote_units({}, 9.48) == {} and quote_units(None, 9.48) is None
