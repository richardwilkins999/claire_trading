"""Dashboards server tests — real HTTP against the stdlib server, offline
(fake market, no claire-api: the approve path 502s but authorization rules
still enforce). Includes the pairing-key rules: tokens only for a paired
browser."""
import json
import threading
from decimal import Decimal

import httpx
import pytest

from app.accounting import db
from app.accounting.money import to_micro
from app.accounting.repo import Repo
from app.dashboards import create_server
from app.tools import tradingview

T0 = 1_786_456_800          # Tue 2026-08-11 14:00 UTC — NASDAQ open
DKEY = "dash-key-123"


class FakeMarket:
    _stale_keys = set()

    def spark(self, symbols):
        return {s: {"symbol": s, "price": Decimal("101"), "currency": "USD",
                    "stale": False, "series": [99, 100, 101],
                    "previous_close": 100} for s in symbols}

    def fx(self, a, b):
        return Decimal("0.74") if a != b else Decimal(1)

    def search(self, q):
        return [{"symbol": "NVDA", "name": "NVIDIA", "exchange": "NASDAQ"}]

    def chart(self, symbol, range_="6mo", interval="1d"):
        return {"symbol": symbol, "timestamps": [1, 2, 3],
                "open": [1, 2, 3], "high": [2, 3, 4], "low": [1, 1, 2],
                "close": [1.5, 2.5, 3.5], "volume": [10, 20, 30],
                "currency": "USD"}

    def screener(self, exchange, sort="intradaymarketcap", start=0, count=100):
        return {"total": 3456, "rows": [
            {"symbol": "NVDA", "name": "NVIDIA", "price": 181.5,
             "change_pct": 1.2, "volume": 9e7, "mcap": 4.4e12, "pe": 55.1}]}

    def exchange_metrics(self, exchange):
        return {"index": {"symbol": "^IXIC", "level": 24000.5,
                          "change_pct": 0.8, "day_volume": 5.2e9},
                "listings": 3456}


@pytest.fixture
def web():
    from app.providers import seeds
    conn = db.connect(":memory:")
    db.init(conn)
    seeds.seed(conn, ts=T0)
    repo = Repo(conn)
    repo.create_account("acct", "alpaca", "paper", "USD",
                        fee_model={"type": "flat", "per_trade": "0"}, ts=T0)
    repo.deposit("acct", to_micro("1000"), ts=T0)
    conn.execute(
        "INSERT INTO work_items (id, kind, ticker, state, thread_id,"
        " approval_token, token_state, expires_at, thesis_json, created_at,"
        " updated_at) VALUES ('wi_1', 'pipeline', 'NVDA', 'awaiting_approval',"
        " 'wi_1', 'tok_abc', 'minted', ?, ?, ?, ?)",
        (T0 + 3600, json.dumps({"ticker": "NVDA", "direction": "buy",
                                "conviction": 0.7, "entry_low": 99,
                                "entry_high": 101, "stop_loss": 95,
                                "currency": "USD"}), T0, T0))
    srv = create_server(conn, repo, FakeMarket(), secret="s", dash_key=DKEY,
                        api_base="http://127.0.0.1:9",  # discard port: always down
                        clock=lambda: T0, port=0, sse_interval=None)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}", conn
    srv.shutdown()


def paired(base):
    return httpx.Client(base_url=base, timeout=5,
                        headers={"X-Dash-Key": DKEY})


def test_pages_and_apis_serve(web):
    base, conn = web
    with paired(base) as c:
        for page in ("/", "/portfolio", "/markets", "/trading", "/agents",
                     "/providers", "/claire"):
            assert c.get(page).status_code == 200, page
        for asset in ("/style.css", "/nav.js", "/charts.js"):
            assert c.get(asset).status_code == 200, asset
        ov = c.get("/api/overview").json()
        assert ov["pending"] == 1
        assert ov["cash"][0]["cash"] == 1000
        assert any("claire-api" in f for f in ov["flags"])   # api_base is down
        runs = c.get("/api/runs").json()
        assert "approval_token" not in runs[0]
        assert c.get("/api/quote?symbols=NVDA").json()["NVDA"]["price"] == "101"


def test_token_release_requires_pairing(web):
    base, conn = web
    with httpx.Client(base_url=base, timeout=5) as anon:
        cards = anon.get("/api/approvals").json()["cards"]
        assert cards[0]["locked"] is True
        assert cards[0]["token"] is None            # curl gets NO token
        r = anon.post("/api/thesis-action",
                      json={"work_item_id": "wi_1", "action": "approve",
                            "size_base": 500, "broker": "alpaca",
                            "token": "tok_abc"})
        assert r.status_code == 403                 # even WITH the token
    with paired(base) as c:
        data = c.get("/api/approvals").json()
        assert data["cards"][0]["token"] == "tok_abc"   # paired card carries it
        assert data["cards"][0]["locked"] is False
        # the card ships estimate context: accounts with cash + fee models
        alpaca = next(a for a in data["accounts"] if a["broker"] == "alpaca")
        assert alpaca["cash"] == 1000
        assert alpaca["fee_model"]["type"] == "flat"
        # NVDA instrument isn't registered in this fixture → no session block
        assert data["cards"][0]["session"] == {}


def test_thesis_action_authorization_over_http(web):
    base, conn = web
    with paired(base) as c:
        r = c.post("/api/thesis-action",
                   json={"work_item_id": "wi_1", "action": "approve",
                         "size_base": 500, "broker": "alpaca"})
        assert r.status_code == 403                 # no token → no approval
        r = c.post("/api/thesis-action",
                   json={"work_item_id": "wi_1", "action": "approve",
                         "size_base": 500, "broker": "alpaca",
                         "token": "wrong"})
        assert r.status_code == 403
        # right token but claire-api is down: authorized, saved, 502 surfaced
        r = c.post("/api/thesis-action",
                   json={"work_item_id": "wi_1", "action": "approve",
                         "size_base": 500, "broker": "alpaca",
                         "token": "tok_abc"})
        assert r.status_code == 502
        row = conn.execute("SELECT token_state FROM work_items"
                           " WHERE id='wi_1'").fetchone()
        assert row["token_state"] == "pending_resume"


def test_exchange_info(web):
    base, conn = web
    with paired(base) as c:
        i = c.get("/api/exchange-info?exchange=SGX").json()
        assert i["tz"] == "Asia/Singapore"
        assert i["open_time"] == "09:00"
        # T0 is 14:00 UTC = 22:00 SGT → closed, next open Wed 09:00 SGT
        assert i["is_open"] is False
        assert i["next_open"].startswith("2026-08-12T09:00")
        assert i["index"]["level"] == 24000.5
        assert i["listings"] == 3456
        # NASDAQ at the same instant is open
        assert c.get("/api/exchange-info?exchange=NASDAQ").json()["is_open"] \
            is True


def test_screener_and_run_detail_and_graph(web):
    base, conn = web
    with paired(base) as c:
        s = c.get("/api/screener?exchange=NASDAQ").json()
        assert s["rows"][0]["pe"] == 55.1
        d = c.get("/api/run-detail?id=wi_1").json()
        assert d["thesis"]["direction"] == "buy"
        assert "approval_token" not in d
        g = c.get("/api/agent-graph").json()
        assert "arbiter" in g["agents"]
        assert g["brokers"][0]["broker"] == "alpaca"
        assert g["brokers"][0]["adapter"] == "paper-sim"
        assert ["gate", "execute"] in [list(e) for e in g["edges"]]


def test_world_markets_strip(web):
    base, conn = web
    with paired(base) as c:
        rows = c.get("/api/exchanges").json()
        assert len(rows) == 10
        byex = {r["exchange"]: r for r in rows}
        # T0 = Tue 14:00 UTC: NASDAQ open, SGX (22:00 SGT) closed
        assert byex["NASDAQ"]["is_open"] is True
        assert byex["SGX"]["is_open"] is False
        assert byex["SGX"]["next_open"].startswith("2026-08-12T09:00")
        assert byex["HKEX"]["lunch_break"] == "12:00-13:00"
        assert rows[0]["exchange"] == "SGX"         # geographic order, Asia first


def test_fx_and_agent_activity(web):
    base, conn = web
    with paired(base) as c:
        assert c.get("/api/fx?from=SGD&to=USD").json()["rate"] == "0.74"
        conn.execute(
            "INSERT INTO agent_runs (id, agent_id, work_item_id, provider_id,"
            " model, started_at, status, tokens_in, tokens_out, cost_usd)"
            " VALUES ('r1', 'technical', 'wi_1', 'anthropic', 'm', ?,"
            " 'running', 10, 0, 0.01)", (T0 - 30,))
        act = c.get("/api/agent-activity?id=technical").json()
        assert act["current"]["work_item_id"] == "wi_1"
        assert act["runs"][0]["ticker"] == "NVDA"
        assert act["cost_24h"] == 0.01


def test_schedule_endpoints(web):
    base, conn = web
    with paired(base) as c:
        jobs = {j["job"]: j for j in c.get("/api/schedules").json()}
        assert jobs["custodian"]["spec"]["minutes"] == 5
        assert jobs["analysis_asia"]["spec"]["require_open"] is True
        assert jobs["watcher"]["runs_in"] == "dashboards"
        # analysis_asia last ran Tue 10:00 SGT? never — next is computed
        assert jobs["custodian"]["next_run_at"] == T0    # due immediately
        r = c.post("/api/schedule", json={"job": "watcher",
                                          "spec": {"minutes": 30}})
        assert r.status_code == 200
        jobs = {j["job"]: j for j in c.get("/api/schedules").json()}
        assert jobs["watcher"]["spec"]["minutes"] == 30
        assert c.post("/api/schedule", json={
            "job": "watcher", "spec": {"minutes": 0}}).status_code == 400
        assert c.post("/api/schedule", json={
            "job": "ghost"}).status_code == 400


def test_merged_agents_schedule_page(web):
    base, conn = web
    with paired(base) as c:
        for path in ("/agents", "/schedule"):        # /schedule → merged page
            body = c.get(path).text
            assert "Agents &amp; Schedule" in body, path


def test_mcp_crud_and_provider_upsert(web):
    base, conn = web
    with paired(base) as c:
        mcps = {m["id"]: m for m in c.get("/api/mcps").json()}
        assert mcps["alpaca"]["creds_present"] is False
        assert "ALPACA_PAPER_KEY" in mcps["alpaca"]["missing_refs"]
        # add a custom MCP with a string command and comma refs
        r = c.post("/api/mcp", json={"id": "ibkr", "broker": "alpaca",
                                     "command": "uvx ibkr-mcp --paper",
                                     "env_refs": "IBKR_TOKEN, IBKR_ACCT",
                                     "note": "test"})
        assert r.status_code == 200
        m = {x["id"]: x for x in c.get("/api/mcps").json()}["ibkr"]
        assert m["command"] == ["uvx", "ibkr-mcp", "--paper"]
        assert m["env_refs"] == ["IBKR_TOKEN", "IBKR_ACCT"]
        assert c.post("/api/mcp", json={
            "id": "bad", "command": "x", "env_refs": "lower_case"
        }).status_code == 400
        r = c.post("/api/mcp", json={"id": "ibkr", "action": "delete"})
        assert r.status_code == 200
        assert "ibkr" not in {x["id"] for x in c.get("/api/mcps").json()}
        # provider upsert: add, appears in list with costs; invalid kind 400
        r = c.post("/api/provider", json={
            "id": "groq", "display_name": "Groq", "kind": "openai_compatible",
            "base_url": "https://api.groq.com/openai/v1",
            "api_key_ref": "GROQ_API_KEY",
            "capabilities": {"tool_calling": True, "structured_output": True},
            "cost_per_1k_in": 0.0001, "cost_per_1k_out": 0.0002})
        assert r.status_code == 200
        provs = {p["id"]: p for p in c.get("/api/providers").json()}
        assert provs["groq"]["cost_per_1k_out"] == 0.0002
        assert provs["groq"]["key_present"] is False
        assert c.post("/api/provider", json={
            "id": "x!", "kind": "openai_compatible"}).status_code == 400
        assert c.post("/api/provider", json={
            "id": "ok", "kind": "carrier-pigeon"}).status_code == 400


def test_tv_recommendations_offline():
    class FakeResp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"totalCount": 2, "data": [
                {"s": "NASDAQ:AAA", "d": ["AAA", "Alpha", 10.0, 1.5, 2e6,
                                          0.62, 5e9, 61.0]},
                {"s": "NASDAQ:BBB", "d": ["BBB", "Beta", 5.0, -0.5, 1e6,
                                          0.31, 1e9, 48.2]}]}

    class FakeClient:
        def post(self, url, json=None):
            assert "scanner.tradingview.com/america/scan" in url
            return FakeResp()

    out = tradingview.recommendations("NASDAQ", _client=FakeClient())
    assert out["rows"][0]["ticker"] == "AAA"
    assert out["rows"][0]["rating_label"] == "strong buy"
    assert out["rows"][1]["rating_label"] == "buy"
