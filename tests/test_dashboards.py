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


def test_approvals_page_carries_the_whole_case(web):
    """The decision page must show the evidence, not just a headline: every
    agent's typed report, the trigger, and the cost that produced it."""
    base, conn = web
    conn.execute(
        "INSERT INTO agent_reports (work_item_id, agent_id, kind, payload,"
        " created_at) VALUES ('wi_1','technical','analyst',?,?)",
        (json.dumps({"agent": "technical", "signal": "bearish",
                     "conviction": 0.45, "summary": "Below the 50 day.",
                     "key_findings": ["RSI 38", "support 424"]}), T0))
    conn.execute(
        "INSERT INTO agent_reports (work_item_id, agent_id, kind, payload,"
        " created_at) VALUES ('wi_1','arbiter','thesis',?,?)",
        (json.dumps({"direction": "buy", "conviction": 0.6,
                     "conditions": ["thin volume"]}), T0))
    conn.execute("UPDATE work_items SET trigger='event scan: rel-vol 4.1x'"
                 " WHERE id='wi_1'")
    with paired(base) as c:
        assert c.get("/approvals").status_code == 200
        # and Mission Control no longer hosts the approval controls
        home = c.get("/").text
        assert "renderApprovals" not in home
        assert "/approvals" in home              # it links out instead

        card = c.get("/api/approvals").json()["cards"][0]
        assert card["trigger"] == "event scan: rel-vol 4.1x"
        assert card["exchange"] is None or isinstance(card["exchange"], str)
        agents = {r["agent"]: r for r in card["reports"]}
        assert agents["technical"]["data"]["key_findings"] == \
            ["RSI 38", "support 424"]
        assert agents["arbiter"]["data"]["direction"] == "buy"
        assert "llm_cost" in card and "llm_calls" in card


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


def test_account_topup_withdraw_and_approve_balance_guard(web):
    base, conn = web
    with httpx.Client(base_url=base, timeout=5) as anon:
        r = anon.post("/api/account-action",
                      json={"account_id": "acct", "action": "deposit",
                            "amount": 500})
        assert r.status_code == 403                 # money moves need pairing
    with paired(base) as c:
        r = c.post("/api/account-action",
                   json={"account_id": "acct", "action": "deposit",
                         "amount": 500})
        assert (r.status_code, r.json()["balance"]) == (200, 1500)
        r = c.post("/api/account-action",
                   json={"account_id": "acct", "action": "withdraw",
                         "amount": 99999})
        assert r.status_code == 400                 # exceeds balance
        assert c.post("/api/account-action",
                      json={"account_id": "acct", "action": "deposit",
                            "amount": -5}).status_code == 400
        # approving a buy larger than the account's cash is refused upfront
        r = c.post("/api/thesis-action",
                   json={"work_item_id": "wi_1", "action": "approve",
                         "size_base": 99999, "broker": "alpaca",
                         "token": "tok_abc"})
        assert r.status_code == 400
        assert "exceeds" in r.json()["error"]
        row = conn.execute("SELECT token_state FROM work_items"
                           " WHERE id='wi_1'").fetchone()
        assert row["token_state"] == "minted"       # nothing consumed


def test_narrative_formatting_and_per_run_fetch(web, tmp_path):
    """Runs list caps at 5 and flags which have a write-up; narratives are
    formatted for humans (the file on disk stays the raw record)."""
    from app.narrative_format import format_narrative
    base, conn = web
    # a debater's JSON dump becomes prose
    out = format_narrative('# arbiter — AMD\n\n' + json.dumps({
        "direction": "pass", "conviction": 0.3, "stop_loss": 92,
        "conditions": ["feeds disagree", "no forward EPS"]}))
    assert "**Verdict: PASS**" in out
    assert "- feeds disagree" in out
    assert "stop loss 92" in out
    assert "{" not in out                        # no raw JSON left
    # an analyst transcript becomes numbered steps + prose
    out = format_narrative('# news — AMD\n\n'
                           '[human] Research AMD now.\n'
                           '[ai] [{"type": "text", "text": "Looking now."},'
                           ' {"type": "tool_use", "name": "web_search",'
                           ' "input": {"query": "AMD news"}}]\n'
                           '[tool] {"title": "AMD beats"}')
    assert "**Task** — Research AMD now." in out
    assert "Looking now." in out
    assert "called `web_search`" in out
    assert "query=AMD news" in out
    assert '"type": "tool_use"' not in out       # blocks rendered, not dumped

    with paired(base) as c:
        for i in range(7):                       # more runs than the cap
            conn.execute(
                "INSERT INTO agent_runs (id, agent_id, work_item_id,"
                " provider_id, model, started_at, status) VALUES"
                " (?,'news','wi_1','anthropic','m',?,'ok')",
                (f"r{i}", T0 - i * 60))
        act = c.get("/api/agent-activity?id=news").json()
        assert len(act["runs"]) == 5             # scrollable list of last 5
        assert all("has_narrative" in r for r in act["runs"])
        assert act["runs"][0]["has_narrative"] is False   # none on disk here
        miss = c.get("/api/agent-narrative?agent=news&work_item=nope").json()
        assert "error" in miss


def test_node_status_vocabulary(web):
    """Every node type reports the same status vocabulary: active (glowing),
    ok, warn, error, idle, disabled — computed from real conditions, not
    just from an agent's last run."""
    base, conn = web
    with paired(base) as c:
        g = c.get("/api/agent-graph").json()
        # not signed in to Claude (conftest) → SDK agents cannot run: error
        tech = g["agents"]["technical"]
        assert tech["state"] == "error"
        assert "not signed in" in tech["detail"]

        # a run in flight outranks everything → active (this is the glow)
        conn.execute(
            "INSERT INTO agent_runs (id, agent_id, provider_id, model,"
            " started_at, status) VALUES ('r_live','technical','anthropic',"
            "'m', ?, 'running')", (T0 - 5,))
        g = c.get("/api/agent-graph").json()
        assert g["agents"]["technical"]["state"] == "active"
        assert g["agents"]["technical"]["detail"] == "running now"

        # a disabled agent reads as disabled, not as an error
        conn.execute("UPDATE agents SET enabled=0 WHERE id='news'")
        assert c.get("/api/agent-graph").json()["agents"]["news"]["state"] \
            == "disabled"

        # venues: MCP rows exist but creds are missing → warn (not error:
        # the paper sim is a working fallback by design)
        byb = {b["broker"]: b for b in g["brokers"]}
        assert byb["alpaca"]["state"] == "warn"
        assert "creds missing" in byb["alpaca"]["detail"]

        # services: a guard that stopped reporting is an ERROR, not a shrug
        conn.execute("INSERT INTO service_health (service, checked_at, ok)"
                     " VALUES ('watcher', ?, 'ok')", (T0 - 7200,))
        assert c.get("/api/agent-graph").json()["health"]["watcher"]["state"] \
            == "error"                                  # stale → error
        conn.execute("INSERT INTO service_health (service, checked_at, ok)"
                     " VALUES ('custodian', ?, 'degraded')", (T0 - 60,))
        assert c.get("/api/agent-graph").json()["health"]["custodian"][
            "state"] == "warn"


def test_mission_control_map_is_one_svg(web):
    """The map must be a single SVG (nodes AND edges in one coordinate
    space) — the earlier overlay version had panels painting over the
    connectors, which is what made it unreadable."""
    base, conn = web
    with paired(base) as c:
        page = c.get("/").text
        assert 'id="map"' in page and 'viewBox="0 0 1240 530"' in page
        assert "class=\"mapwrap\"" in page
        # no overlay layer: nothing on the page is positioned over the map.
        # (The Today tab's small price bar, .pbar, is the one positioned
        # thing here, and it sits in a table cell of the other tab.)
        placed = [ln.strip() for ln in page.splitlines()
                  if "position:absolute" in ln]
        assert all(ln.startswith(".pbar ") for ln in placed), placed
        for piece in ("renderMap", "SCREEN · scans only",
                      "OTHER SYSTEMS", "BACK OFFICE", "claire_chat",
                      "VENUES", "mcp:", "id === 'book'"):
            assert piece in page, piece
        css = c.get("/style.css").text
        assert ".mnode" in css and ".medge" in css
        assert ".deskmap" not in css                # old overlay styles gone


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
            "api_key_ref": "OPENAI_API_KEY",
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


def test_saxo_accounts_page_needs_the_key_and_weights(web, monkeypatch):
    """/saxo shows real-account data, so even its GET needs the key; each
    position carries its share of gross exposure for the allocation bars."""
    from app.tools import saxo_live
    base, conn = web
    calls = []

    def fake_summary(which, *, with_orders=False, **kw):
        calls.append(which)
        return {"env": which, "account": "x", "account_id": "99999999",
                "balance": {"Currency": "SGD", "TotalValue": 10000},
                "positions": [{"symbol": "D05:xses", "exposure_base": 3000},
                              {"symbol": "AAPL:xnas", "exposure_base": -1000}],
                "orders": []}
    monkeypatch.setattr(saxo_live, "summary", fake_summary)
    assert httpx.get(base + "/api/saxo-account?env=live").status_code == 403
    with paired(base) as c:
        assert c.get("/saxo").status_code == 200
        d = c.get("/api/saxo-account?env=live").json()
        assert [p["weight_pct"] for p in d["positions"]] == [75.0, 25.0]
        assert c.get("/api/saxo-account?env=sim").json()["env"] == "sim"
        assert c.get("/api/saxo-account?env=prod").status_code == 400
    assert calls == ["live", "sim"]


def test_approval_card_labels_both_saxo_accounts():
    """The real account is listed by number — greyed out with the reason
    until live trading is armed — and SIM is named by its account."""
    from app.dashboards import _approval_account
    conn = db.connect(":memory:")
    db.init(conn)
    repo = Repo(conn)
    repo.create_account("saxo-paper", "saxo", "paper", "USD",
                        fee_model={"type": "flat", "per_trade": "0"}, ts=T0)
    repo.create_account("saxo-live", "saxo_live", "live", "SGD",
                        fee_model={"type": "flat", "per_trade": "0"}, ts=T0,
                        allow_live=True, external_ref="K")
    env = {"SAXO_SIM_EXECUTION": "enabled", "SAXO_SIM_ACCOUNT_ID": "11111111",
           "SAXO_LIVE_ACCOUNT_ID": "99999999"}
    rows = {r["broker"]: r for r in conn.execute(
        "SELECT * FROM broker_accounts")}
    off = {"armed": False, "summary": "live trading OFF"}
    sim = _approval_account(rows["saxo"], repo, env, off)
    live = _approval_account(rows["saxo_live"], repo, env, off)
    assert sim["label"] == "Saxo SIM 11111111 — paper" and not sim["disabled"]
    assert live["label"] == "Saxo LIVE 99999999 — real money"
    assert live["disabled"] == "live trading OFF"
    armed = _approval_account(rows["saxo_live"], repo, env,
                              {"armed": True, "summary": "ARMED"})
    assert armed["disabled"] is None


def test_saxo_sign_in_button_flow(web, monkeypatch):
    """Start needs the key and hands back Saxo's URL with a callback on THIS
    server; a callback the dashboard did not start is refused."""
    from app.tools import saxo_live
    base, conn = web
    monkeypatch.setattr(saxo_live, "app_config",
                        lambda env, which=None: {
                            "which": which, "app_key": "k", "secret": None,
                            "redirect_uri": "http://localhost/claire-saxo",
                            "auth_base": "https://sim.logonvalidation.net"})
    port = base.rsplit(":", 1)[1]
    r = httpx.post(base + "/api/saxo-login-start", json={"env": "sim"})
    assert r.status_code == 403                      # no key, no sign-in
    with paired(base) as c:
        url = c.post("/api/saxo-login-start", json={"env": "sim"}).json()["url"]
        assert url.startswith("https://sim.logonvalidation.net/authorize?")
        assert f"localhost%3A{port}%2Fclaire-saxo" in url
        bad = c.get("/claire-saxo?state=forged&code=x")
        assert bad.status_code == 400 and "sign-in failed" in bad.text.lower()


def test_trade_retry_and_cancel_routes_need_the_key(web):
    base, conn = web
    for route in ("/api/trade-retry", "/api/trade-cancel"):
        assert httpx.post(base + route, json={"work_item_id": "x"}
                          ).status_code == 403
    with paired(base) as c:
        assert c.post("/api/trade-cancel", json={"work_item_id": "nope"}
                      ).status_code == 404
        assert "failed" in c.get("/api/approvals").json()


def test_trade_check_needs_the_key_and_explains_the_built_in_sim(web):
    base, conn = web
    assert httpx.post(base + "/api/trade-check",
                      json={"work_item_id": "wi_1"}).status_code == 403
    with paired(base) as c:
        r = c.post("/api/trade-check", json={"work_item_id": "wi_1",
                                              "broker": "alpaca",
                                              "entry": 100, "size_base": 500})
        assert r.status_code == 200 and r.json()["ok"] is None
        assert "built-in paper sim" in r.json()["note"]


# ── /usage: LLM token usage per agent / source / model ─────────────────────
def test_usage_source_attribution_rules():
    from app.dashboards import usage_source as src
    # no work item: decided by the agent
    assert src("claire_chat", linked=None) == "chat"
    assert src("screener", linked=None) == "screener"
    assert src("recorder", linked=None) == "journal"
    assert src("fundamental", linked=None) == "agent_qa"
    # a dangling work_item_id
    assert src("bull", linked=False) == "other"
    # research runs: kind, then trigger text
    assert src("bull", "override", "human override of wi_x") == "override"
    assert src("bull", "pipeline", "human override of wi_x") == "override"
    assert src("news", "sell_review", None) == "exit_review"
    assert src("news", "pipeline", "watcher: stop breached") == "exit_review"
    assert src("news", "pipeline", "event scan: relative volume 5.2x") == \
        "volume_scan"
    assert src("news", "pipeline", "screener agent: momentum",
               "NASDAQ") == "daily_us"
    assert src("news", "pipeline", "screener agent: x", "SGX") == "daily_asia"
    assert src("news", "pipeline", "deterministic fallback (down): top",
               "LSE") == "daily_eu"
    assert src("news", "pipeline", "screener agent: x", None) == "daily"
    assert src("news", "pipeline", None) == "manual"
    assert src("news", "pipeline", "something else") == "manual"


def _usage_rows(conn):
    conn.execute(
        "INSERT INTO work_items (id, kind, ticker, state, thread_id,"
        " created_at, updated_at, trigger) VALUES ('wi_scan', 'pipeline',"
        " 'NVDA', 'done', 'wi_scan', ?, ?, 'event scan: volume 4x')",
        (T0 - 60, T0 - 60))
    rows = [  # agent, wi, provider, model, at, in, out, cw, cr, cost, status
        ("claire_chat", None, "claude_max", "claude-opus-5", T0, 10_000, 500,
         3_000, 6_000, 0, "ok"),
        ("news", "wi_scan", "claude_max", "claude-haiku-4-5", T0 - 30, 4_000,
         1_000, 0, 0, 0, "ok"),
        ("fundamental", "wi_scan", "claude_max", "claude-sonnet-5", T0 - 30,
         2_000, 2_000, 0, 0, 0, "error"),
        ("arbiter", "wi_1", "claude_max", "claude-opus-5", T0 - 20, 1_000,
         100, 0, 0, 0, "ok"),
        # 3 days ago: inside 7d, not today
        ("claire_chat", None, "claude_max", "claude-opus-5", T0 - 3 * 86400,
         1_000, 100, 0, 0, 0, "ok"),
        # 20 days ago: inside 30d only
        ("bull", "wi_1", "anthropic", "claude-sonnet-5", T0 - 20 * 86400,
         1_000, 1_000, 0, 0, 0.018, "ok"),
        # 40 days ago: outside every window
        ("bear", "wi_1", "anthropic", "claude-sonnet-5", T0 - 40 * 86400,
         9_999, 9_999, 0, 0, 0.5, "ok"),
    ]
    for i, r in enumerate(rows):
        conn.execute(
            "INSERT INTO agent_runs (id, agent_id, work_item_id, provider_id,"
            " model, started_at, ended_at, tokens_in, tokens_out,"
            " cache_write_tokens, cache_read_tokens, cost_usd, status)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"run_u{i}", *r[:5], r[4] + 5, *r[5:]))


def test_usage_api_totals_breakdowns_and_value(web, tmp_path, monkeypatch):
    base, conn = web
    _usage_rows(conn)
    bob = tmp_path / "usage.json"
    from datetime import datetime
    day = datetime.fromtimestamp(T0).date().isoformat()
    bob.write_text(json.dumps({"lifetime": {"requests": 2}, "daily": {
        day: {"requests": 2, "input_tokens": 10, "output_tokens": 20,
              "cache_read_tokens": 1000, "cache_creation_tokens": 100,
              "cost_usd": 0.5}}}))
    from app.dash import usage          # where the report now lives
    monkeypatch.setattr(usage, "BOB_USAGE_FILE", bob)
    d = httpx.get(base + "/api/usage?days=7").json()
    t = d["totals"]
    # tokens_in already includes the cache: input is NOT fresh + cache again
    assert t["today"]["calls"] == 4
    assert t["today"]["input"] == 17_000
    assert t["today"]["fresh"] == 8_000
    assert t["today"]["tokens"] == 17_000 + 3_600
    assert t["today"]["errors"] == 1
    assert t["d7"]["calls"] == 5 and t["d30"]["calls"] == 6
    assert t["d30"]["billed"] == pytest.approx(0.018)
    # API-equivalent: opus 5 chat call = fresh 1k + 3k write + 6k read + 500 out
    chat = 1 * .005 + 3 * .005 * 1.25 + 6 * .005 * .1 + .5 * .025
    agent = {b["key"]: b for b in d["by_agent"]}
    assert agent["claire_chat"]["label"] == "Claire chat"
    assert agent["fundamental"]["label"] == "Fundamental analyst"
    assert agent["claire_chat"]["calls"] == 2
    assert agent["claire_chat"]["api_value"] == pytest.approx(
        chat + 1 * .005 + .1 * .025)
    src = {b["key"]: b for b in d["by_source"]}
    assert src["chat"]["label"] == "Claire chat"
    assert src["volume_scan"]["calls"] == 2 and \
        src["volume_scan"]["errors"] == 1
    assert src["manual"]["calls"] == 1             # wi_1 has no trigger
    models = {b["model"]: b for b in d["by_model"]}
    assert models["claude-haiku-4-5"]["label"] == "Claude Haiku 4.5"
    assert len(d["series"]) == 7 and d["series"][-1]["calls"] == 4
    assert d["series"][-1]["by_source"]["volume_scan"] == 9_000
    top = d["top_runs"][0]
    assert top["work_item_id"] == "wi_scan" and top["ticker"] == "NVDA"
    assert top["source_label"] == "Volume scan" and top["tokens"] == 9_000
    assert d["errors"]["count"] == 1
    assert "NOT billed" in d["pricing_note"]
    b = d["bob"]
    assert b["available"] and b["today"]["calls"] == 2
    assert b["today"]["input"] == 1_110 and b["today"]["tokens"] == 1_130
    # a bad days value falls back; 30 days widens the window
    assert httpx.get(base + "/api/usage?days=x").json()["days"] == 7
    assert len(httpx.get(base + "/api/usage?days=30").json()["series"]) == 30


def test_usage_bob_missing_file_is_reported(tmp_path):
    from app.dashboards import bob_usage
    out = bob_usage(tmp_path / "nope.json", now=T0)
    assert out["available"] is False and "no usage file" in out["reason"]
    (tmp_path / "bad.json").write_text("{not json")
    assert bob_usage(tmp_path / "bad.json", now=T0)["available"] is False


def test_usage_page_serves_and_is_in_the_nav(web):
    base, _ = web
    r = httpx.get(base + "/usage")
    assert r.status_code == 200 and "/api/usage" in r.text
    assert "['/usage', 'Usage']" in httpx.get(base + "/nav.js").text


def test_trading_page_runs_read_as_stories(web, tmp_path):
    """/api/runs and /api/run-detail carry a plain-language layer: who
    started the run, the verdict, what happened to the order — and a retry
    loop's refused order rows collapse into one counted line."""
    from app.dashboards import create_server
    from app.tools.files import Narratives
    base, conn = web
    Repo(conn).create_account("saxo-paper", "saxo", "paper", "USD",
                              fee_model={"type": "flat", "per_trade": "0"},
                              ts=T0)
    conn.execute("INSERT OR IGNORE INTO instruments VALUES"
                 " ('NYSE:STVN','STVN','NYSE','USD','Stevanato Group')")
    thesis = {"ticker": "STVN", "direction": "pass", "conviction": 0.3,
              "currency": "USD", "conditions": ["catalyst unconfirmed",
                                                "volume premise wrong",
                                                "no stop placeable",
                                                "fourth"]}
    for wi, kind, state, trig, ovr, th in (
            ("wi_p", "pipeline", "done",
             "event scan: relative volume 5.2x its 10-day average today",
             None, thesis),
            ("wi_o", "override", "done", "human override of wi_p", "wi_p",
             {**thesis, "direction": "buy", "entry_low": 14.62,
              "entry_high": 14.62, "stop_loss": 11.57, "take_profit": 15,
              "conditions": thesis["conditions"] +
              ["HUMAN OVERRIDE of a PASS verdict"]}),
            ("wi_f", "pipeline", "failed", None, None, None)):
        conn.execute(
            "INSERT INTO work_items (id, kind, ticker, state, thread_id,"
            " thesis_json, trigger, override_of, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (wi, kind, "STVN", state, wi, json.dumps(th) if th else None,
             trig, ovr, T0, T0))
    ev = [("wi_o", "human", "awaiting_approval", "approved",
           {"size_base": 1000.0, "broker": "saxo"})]
    for i in range(30):
        ev += [("wi_o", "human", "approved", "approve_retry",
                {"size_base": 1000.0, "broker": "saxo"}),
               ("wi_o", "system", "approved", "resume_failed",
                {"error": "broker rejected order: quantity 66.6 is not a"
                          " whole multiple of the Saxo lot size 1"})]
    ev += [("wi_o", "system", "approved", "executing", None),
           ("wi_o", "custodian", "executing", "done", None),
           ("wi_f", "system", "running", "failed",
            {"errors": ["fundamental: Error code: 400 - {'message': 'Your"
                        " credit balance is too low to access the API.'}"]})]
    for i, (wi, actor, a, b, p) in enumerate(ev):
        conn.execute("INSERT INTO events (item_id, ts, actor, from_state,"
                     " to_state, payload) VALUES (?,?,?,?,?,?)",
                     (wi, T0 + i, actor, a, b, json.dumps(p) if p else None))
    for i in range(30):                          # the retry loop's debris
        conn.execute(
            "INSERT INTO orders (id, work_item_id, account_id, instrument_id,"
            " side, qty, status, expires_at, updated_at) VALUES"
            " (?,?,?,?,?,?,?,?,?)", (f"ord_r{i}", "wi_o", "saxo-paper",
                                     "NYSE:STVN", "buy", 66_600_000,
                                     "rejected", T0 + 9999, T0))
    conn.execute(
        "INSERT INTO orders (id, work_item_id, account_id, instrument_id,"
        " side, qty, limit_price, status, broker_order_id, expires_at,"
        " updated_at) VALUES ('ord_ok','wi_o','saxo-paper','NYSE:STVN',"
        " 'buy', 68000000, 14620000, 'filled', 'B1', ?, ?)",
        (T0 + 9999, T0))
    conn.execute(
        "INSERT INTO executions (id, order_id, account_id, instrument_id,"
        " work_item_id, side, qty, price_native, currency, fx_rate,"
        " gross_base, net_base, executed_at, recorded_at) VALUES"
        " ('x1','ord_ok','saxo-paper','NYSE:STVN','wi_o','buy',68000000,"
        " 14620000,'USD',1000000,994160000,994160000,?,?)", (T0, T0))
    with paired(base) as c:
        runs = {r["id"]: r for r in c.get("/api/runs").json()}
    p, o, f = runs["wi_p"]["story"], runs["wi_o"]["story"], \
        runs["wi_f"]["story"]
    assert "thesis_json" in runs["wi_p"]           # raw fields stay
    assert p["origin"] == "volume_scan"
    assert p["origin_detail"].startswith("relative volume 5.2x")
    assert p["name"] == "Stevanato Group"
    assert p["verdict"] == "pass" and p["conviction_word"] == "low"
    assert p["reasons"] == ["catalyst unconfirmed", "volume premise wrong",
                            "no stop placeable"]
    assert [x["id"] for x in p["overridden_by"]] == ["wi_o"]
    assert o["origin"] == "override" and o["override_of"] == "wi_p"
    assert "HUMAN OVERRIDE of a PASS verdict" not in o["reasons"]
    assert o["approval"]["account"] == "Saxo SIM (paper)"
    assert o["approval"]["size"] == 1000.0
    assert o["fills"][0]["qty"] == 68 and o["fills"][0]["avg_price"] == \
        pytest.approx(14.62)
    assert [x["id"] for x in o["orders"]] == ["ord_ok"]   # debris hidden
    fa = o["failed_attempts"]
    assert fa["rows"] == 30 and fa["tries"] == 30
    assert fa["reasons"][0]["count"] == 30
    assert "whole shares" in fa["reasons"][0]["reason"]
    assert f["origin"] == "unrecorded"      # no trigger: source not recorded
    assert f["failure"]["reason"] == "the AI provider account ran out of credit"

    # the detail adds a short timeline and readable pipeline steps
    nar = Narratives(tmp_path)
    nar.write("wi_p", "technical.report",
              "# technical — STVN\n\n**Signal: BEARISH** · conviction 0.55"
              "\n\nCrashed 24% on thin volume.\n\n**Key findings**\n- a\n- b"
              "\n- c\n- d")
    nar.write("wi_p", "bull.report",
              "# bull — STVN\n\n**Bull case** · conviction 0.3\n\n"
              "**Key points**\n- oversold")
    srv = create_server(conn, Repo(conn), FakeMarket(), secret="s",
                        dash_key=DKEY, api_base="http://127.0.0.1:9",
                        narratives=nar, clock=lambda: T0, port=0,
                        sse_interval=None)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with paired(f"http://127.0.0.1:{srv.server_address[1]}") as c:
            d = c.get("/api/run-detail?id=wi_p").json()
            steps = {s["agent"]: s for s in d["steps"]}
            assert steps["technical"]["signal"] == "bearish"
            assert steps["technical"]["conviction"] == 0.55
            assert steps["technical"]["summary"] == \
                "Crashed 24% on thin volume."
            assert steps["technical"]["points"] == ["a", "b", "c"]
            assert steps["bull"]["signal"] is None
            assert steps["bull"]["points"] == ["oversold"]
            o = c.get("/api/run-detail?id=wi_o").json()
            texts = [t["text"] for t in o["timeline"]]
            assert "You approved 1,000 USD on Saxo SIM (paper)" in texts
            fails = [t for t in texts if t.startswith("Sending the order")]
            assert len(fails) == 1 and "30 times" in fails[0]
            assert texts[-1] == "Finished — bought 68 shares at 14.62 USD"
            assert len(o["events"]) == 63            # raw trail kept
    finally:
        srv.shutdown()


def test_trading_page_escapes_and_keeps_its_links():
    """The rebuilt Trading page still launches runs, links Approvals and
    run narratives, and never drops a server string in unescaped."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "web" /
           "trading.html").read_text()
    assert "api('/api/run'," in src
    assert 'href="/approvals"' in src
    assert "/api/agent-narrative?agent=" in src
    assert "poll(refresh" in src
    for raw in ("${r.id}", "${r.ticker}", "${s.name}", "${st.summary}",
                "${x.reason}", "${o.account}"):
        assert raw not in src, raw
