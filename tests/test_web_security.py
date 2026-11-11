"""Dashboard web-security fixes from REVIEW-2026-11-03 (C1, H1, H2, H3 and
the Low items owned by dashboards.py / web / desk.py). Real HTTP against the
stdlib server, offline: claire-api is never reachable, and httpx.post inside
the server is trapped so a test fails loudly if a refused request would have
reached it."""
import json
import re
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest

from app import dashboards
from app.accounting import db
from app.accounting.money import to_micro
from app.accounting.repo import Repo
from app.api.desk import Desk, validate_symbol
from app.dashboards import create_server
from app.graph.state import Instrument
from tests.test_dashboards import DKEY, T0, FakeMarket

WEB = Path(__file__).resolve().parents[1] / "web"


def _build(**kw):
    from app.providers import seeds
    conn = db.connect(":memory:")
    db.init(conn)
    seeds.seed(conn, ts=T0)
    repo = Repo(conn)
    repo.create_account("acct", "alpaca", "paper", "USD",
                        fee_model={"type": "flat", "per_trade": "0"}, ts=T0)
    repo.deposit("acct", to_micro("1000"), ts=T0)
    opts = dict(secret="s", dash_key=DKEY, api_base="http://127.0.0.1:9",
                clock=lambda: T0, port=0, sse_interval=None)
    opts.update(kw)
    srv = create_server(conn, repo, FakeMarket(), **opts)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, conn


@pytest.fixture
def server():
    made = []

    def make(**kw):
        srv, conn = _build(**kw)
        made.append(srv)
        return f"http://127.0.0.1:{srv.server_address[1]}", conn
    yield make
    for s in made:
        s.shutdown()


@pytest.fixture
def web(server):
    return server()


@pytest.fixture
def no_upstream(monkeypatch):
    """Record (and refuse) every call the server would make to claire-api."""
    calls = []

    def trap(*a, **k):
        calls.append((a, k))
        raise httpx.ConnectError("trapped in test")
    monkeypatch.setattr(dashboards.httpx, "post", trap)
    monkeypatch.setattr(dashboards.httpx, "stream", trap)
    return calls


def paired(base):
    return httpx.Client(base_url=base, timeout=5,
                        headers={"X-Dash-Key": DKEY})


POST_ROUTES = {
    "/api/run": {"ticker": "NVDA", "exchange": "NASDAQ", "currency": "USD"},
    "/api/ask": {"text": "hi", "thread": "t"},
    "/api/agent-ask": {"agent_id": "arbiter", "text": "hi"},
    "/api/agent": {"id": "arbiter", "provider_id": "anthropic",
                   "model": "claude-opus-5"},
    "/api/provider": {"id": "evil", "kind": "openai_compatible",
                      "base_url": "http://attacker/v1",
                      "api_key_ref": "ANTHROPIC_API_KEY"},
    "/api/provider-test": {"provider_id": "anthropic"},
    "/api/schedule": {"job": "watcher", "enabled": False},
    "/api/mcp": {"id": "evil", "command": "sh -c id"},
    "/api/watchlist": {"ticker": "NVDA", "exchange": "NASDAQ"},
    "/api/thesis-action": {"work_item_id": "x", "action": "reject"},
    "/api/override": {"work_item_id": "x"},
    "/api/account-action": {"account_id": "acct", "action": "deposit",
                            "amount": 1},
}


# ── C1 / H1: every POST needs the key ───────────────────────────────────────
@pytest.mark.parametrize("route", sorted(POST_ROUTES))
def test_every_post_route_requires_the_key(web, no_upstream, route):
    base, conn = web
    before = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("providers", "mcp_servers", "watchlist")}
    sched = conn.execute("SELECT enabled FROM schedules WHERE job='watcher'"
                         ).fetchone()[0]
    for headers in ({}, {"X-Dash-Key": "wrong"},
                    {"X-Dash-Key": DKEY[:-1]},            # same prefix
                    {"X-Dash-Key": DKEY + "x"}):
        # a Client, not httpx.post: the module function is trapped above
        with httpx.Client(timeout=5) as c:
            r = c.post(base + route, json=POST_ROUTES[route],
                       headers=headers)
        assert r.status_code == 403, (route, headers, r.text)
    assert no_upstream == []                     # nothing reached claire-api
    after = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
             for t in before}
    assert after == before
    assert conn.execute("SELECT enabled FROM schedules WHERE job='watcher'"
                        ).fetchone()[0] == sched


def test_no_configured_key_means_nobody_is_paired(server, no_upstream):
    """dash_key='' used to skip the checks entirely (fail open)."""
    base, _ = server(dash_key="")
    for headers in ({}, {"X-Dash-Key": ""}):
        with httpx.Client(timeout=5) as c:
            r = c.post(base + "/api/watchlist", headers=headers,
                       json={"ticker": "NVDA", "exchange": "NASDAQ"})
        assert r.status_code == 403


def test_pairing_uses_constant_time_compare():
    from app.dash import handler
    src = Path(handler.__file__).read_text()
    body = src[src.index("def _paired"):src.index("# ── GET")]
    assert "hmac.compare_digest" in body and "== dash_key" not in body
    assert "== self.ctx.dash_key" not in body


def test_provider_key_ref_allowlist(web):
    base, conn = web
    with paired(base) as c:
        for bad in ("CLAIRE_DASHBOARD_KEY", "CLAIRE_INTERNAL_SECRET",
                    "ALPACA_PAPER_SECRET", "SAXO_SIM_TOKEN", "GROQ_API_KEY",
                    "anthropic_api_key", "PATH"):
            r = c.post("/api/provider", json={
                "id": "exfil", "kind": "openai_compatible",
                "base_url": "http://attacker/v1", "api_key_ref": bad})
            assert r.status_code == 400, bad
        assert conn.execute("SELECT 1 FROM providers WHERE id='exfil'"
                            ).fetchone() is None
        for ok in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY",
                   "XAI_API_KEY", None, ""):
            r = c.post("/api/provider", json={
                "id": "p1", "kind": "openai_compatible",
                "base_url": "https://api.example.com/v1", "api_key_ref": ok})
            assert r.status_code == 200, ok
        # the keyless Claude Agent SDK kind is still accepted
        r = c.post("/api/provider", json={"id": "sdk2", "kind": "claude_sdk",
                                          "api_key_ref": "ANTHROPIC_API_KEY"})
        assert r.status_code == 200
        assert tuple(conn.execute("SELECT kind, api_key_ref FROM providers"
                                  " WHERE id='sdk2'").fetchone()) == \
            ("claude_sdk", None)
        # base_url must be http(s); display_name length bounded
        assert c.post("/api/provider", json={
            "id": "p2", "kind": "openai_compatible",
            "base_url": "javascript:alert(1)"}).status_code == 400
        assert c.post("/api/provider", json={
            "id": "p2", "kind": "anthropic",
            "display_name": "x" * 500}).status_code == 400


def test_probe_refuses_a_legacy_row_with_a_secret_key_ref(web, monkeypatch):
    base, conn = web
    probed = []
    monkeypatch.setattr(dashboards.provider_health, "probe",
                        lambda *a, **k: probed.append(a) or True)
    conn.execute("INSERT INTO providers (id, display_name, kind, base_url,"
                 " api_key_ref, capabilities, enabled) VALUES ('legacy',"
                 " 'L', 'openai_compatible', 'http://attacker/v1',"
                 " 'CLAIRE_DASHBOARD_KEY', '{}', 1)")
    with paired(base) as c:
        assert c.post("/api/provider-test",
                      json={"provider_id": "legacy"}).status_code == 400
        assert c.post("/api/provider-test",
                      json={"provider_id": "nope"}).status_code == 400
        assert c.post("/api/provider-test",
                      json={"provider_id": "anthropic"}).status_code == 200
    assert [a[1] for a in probed] == ["anthropic"]


def test_mcp_env_refs_cannot_name_desk_secrets(web):
    base, conn = web
    with paired(base) as c:
        for refs in ("CLAIRE_INTERNAL_SECRET", "ANTHROPIC_API_KEY",
                     "OK_TOKEN, CLAIRE_DASHBOARD_KEY"):
            assert c.post("/api/mcp", json={"id": "m1", "command": "x",
                                            "env_refs": refs}
                          ).status_code == 400, refs
        assert c.post("/api/mcp", json={"id": "m1", "command": 5}
                      ).status_code == 400


# ── H1: schedule spec bounds ────────────────────────────────────────────────
def test_schedule_spec_is_bounded(web):
    base, conn = web
    with paired(base) as c:
        bad = [
            {"job": "event_scan", "spec": {"max_runs": 10000}},
            {"job": "event_scan", "spec": {"max_runs": -1}},
            {"job": "event_scan", "spec": {"max_runs": True}},
            {"job": "event_scan", "spec": {"threshold": 0}},
            {"job": "event_scan", "spec": {"type": "daily"}},
            {"job": "watcher", "spec": {"command": "rm -rf"}},
            {"job": "watcher", "spec": {"minutes": 99999}},
            {"job": "watcher", "spec": {"minutes": "5"}},
            {"job": "watcher", "spec": {"time": "09:00"}},   # not this type
            {"job": "watcher", "spec": "minutes=5"},
            {"job": "watcher", "enabled": "yes"},
            {"job": "reconcile", "spec": {"days": [0]}},
            {"job": "reconcile", "spec": {"days": [1, 1]}},
            {"job": "reconcile", "spec": {"tz": "<script>"}},
            {"job": ["watcher"]},
        ]
        for b in bad:
            assert c.post("/api/schedule", json=b).status_code == 400, b
        spec = json.loads(conn.execute("SELECT spec FROM schedules WHERE"
                                       " job='event_scan'").fetchone()[0])
        assert spec["max_runs"] == 1 and spec["type"] == "interval"
        # what the UI actually sends still works
        assert c.post("/api/schedule", json={
            "job": "event_scan", "enabled": True,
            "spec": {"minutes": 30, "require_open": False, "max_runs": 3}
        }).status_code == 200
        assert c.post("/api/schedule", json={
            "job": "reconcile", "enabled": False,
            "spec": {"time": "08:30", "tz": "Asia/Singapore",
                     "days": [1, 2, 3, 4, 5]}}).status_code == 200
        spec = json.loads(conn.execute("SELECT spec FROM schedules WHERE"
                                       " job='event_scan'").fetchone()[0])
        assert spec["max_runs"] == 3 and spec["minutes"] == 30


def test_agent_assignment_rejects_markup_models(web):
    base, conn = web
    with paired(base) as c:
        for body in ({"id": "arbiter", "provider_id": "anthropic",
                      "model": "<img src=x onerror=alert(1)>"},
                     {"id": "arbiter", "provider_id": "anthropic",
                      "model": 'x" autofocus onfocus="alert(1)'},
                     {"id": "arbiter", "provider_id": "../x", "model": "m"},
                     {"id": "ghost", "provider_id": "anthropic", "model": "m"},
                     {"id": "arbiter", "provider_id": "anthropic"}):
            assert c.post("/api/agent", json=body).status_code == 400, body
    assert "<" not in conn.execute("SELECT model FROM agents WHERE"
                                   " id='arbiter'").fetchone()[0]


# ── H2: content type, Origin, Host ─────────────────────────────────────────
def test_post_requires_json_content_type(web):
    base, conn = web
    body = json.dumps({"ticker": "AAA", "exchange": "NASDAQ"})
    for ctype in ("text/plain", "application/x-www-form-urlencoded",
                  "multipart/form-data; boundary=x", None):
        h = {"X-Dash-Key": DKEY}
        if ctype:
            h["Content-Type"] = ctype
        r = httpx.post(base + "/api/watchlist", content=body, headers=h,
                       timeout=5)
        assert r.status_code == 415, ctype
    assert conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] == 0
    r = httpx.post(base + "/api/watchlist", content=body, timeout=5,
                   headers={"X-Dash-Key": DKEY,
                            "Content-Type": "application/json; charset=utf-8"})
    assert r.status_code == 200


def test_origin_must_match_host(web):
    base, _ = web
    host = base.split("//")[1]
    with paired(base) as c:
        for origin in ("http://evil.example", "null",
                       f"http://{host}.evil.example", "file://"):
            assert c.post("/api/watchlist", headers={"Origin": origin},
                          json={"ticker": "AAA", "exchange": "NASDAQ"}
                          ).status_code == 403, origin
            assert c.get("/api/overview", headers={"Origin": origin}
                         ).status_code == 403, origin
        assert c.post("/api/watchlist", headers={"Origin": f"http://{host}"},
                      json={"ticker": "AAA", "exchange": "NASDAQ"}
                      ).status_code == 200
        assert c.get("/api/overview").status_code == 200    # no Origin: ok


def test_host_allowlist_blocks_dns_rebinding(web):
    base, _ = web
    port = base.rsplit(":", 1)[1]
    for host in ("evil.example", f"evil.example:{port}", "127.0.0.1.nip.io",
                 ""):
        for path in ("/", "/api/overview", "/api/approvals", "/nav.js"):
            r = httpx.get(base + path, headers={"Host": host}, timeout=5)
            assert r.status_code == 421, (host, path)
    for host in (f"127.0.0.1:{port}", f"localhost:{port}", "localhost"):
        assert httpx.get(base + "/api/overview", headers={"Host": host},
                         timeout=5).status_code == 200, host


def test_allowed_hosts_follow_the_bind():
    assert dashboards.default_allowed_hosts("127.0.0.1") == \
        {"127.0.0.1", "localhost", "::1"}
    lan = dashboards.default_allowed_hosts("0.0.0.0")
    assert {"127.0.0.1", "localhost"} <= lan
    assert socket.gethostname().lower() in lan
    assert "0.0.0.0" not in lan
    assert dashboards.host_only("[::1]:7787") == "::1"
    assert dashboards.host_only("Box.Local.:7787") == "box.local"
    assert dashboards.host_only("127.0.0.1") == "127.0.0.1"


def test_explicit_allowed_hosts(server):
    base, _ = server(allowed_hosts={"desk.lan"})
    port = base.rsplit(":", 1)[1]
    assert httpx.get(base + "/api/overview", headers={"Host": "desk.lan"},
                     timeout=5).status_code == 200
    assert httpx.get(base + "/api/overview",
                     headers={"Host": f"127.0.0.1:{port}"},
                     timeout=5).status_code == 421


# ── Low: body limits ───────────────────────────────────────────────────────
def _raw(base, request: bytes, wait=5.0):
    host, port = base.split("//")[1].split(":")
    with socket.create_connection((host, int(port)), timeout=wait) as s:
        s.sendall(request)
        data = b""
        try:
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
        except socket.timeout:
            pass
    return data


def _post_head(base, length, extra=""):
    host = base.split("//")[1]
    return (f"POST /api/watchlist HTTP/1.1\r\nHost: {host}\r\n"
            f"X-Dash-Key: {DKEY}\r\nContent-Type: application/json\r\n"
            f"Content-Length: {length}\r\n{extra}\r\n").encode()


def test_bad_content_length_is_refused_not_blocking(web):
    base, _ = web
    t = time.monotonic()
    for bad in ("-1", "abc", "1_0", " ", "99999999999999"):
        out = _raw(base, _post_head(base, bad))
        assert out.split(b"\r\n")[0].split()[1] in (b"400", b"413"), bad
    assert time.monotonic() - t < 5            # nothing waited for a body


def test_oversized_body_is_refused(web):
    base, conn = web
    out = _raw(base, _post_head(base, dashboards.MAX_BODY + 1))
    assert b" 413 " in out.split(b"\r\n")[0]
    with paired(base) as c:
        r = c.post("/api/watchlist", json={"ticker": "AAA",
                                           "exchange": "NASDAQ",
                                           "note": "x" * 300_000})
        assert r.status_code == 413
        assert c.post("/api/watchlist", json=["not", "an", "object"]
                      ).status_code == 400
        assert c.post("/api/watchlist", content=b"{bad json",
                      headers={"Content-Type": "application/json"}
                      ).status_code == 400


# ── Low: SSE streams end ───────────────────────────────────────────────────
def test_sse_stream_has_a_max_lifetime(server):
    base, _ = server(sse_max_age=1)
    t = time.monotonic()
    with httpx.stream("GET", base + "/api/events", timeout=10) as r:
        assert r.status_code == 200
        for _ in r.iter_raw():
            pass                                  # returns when server ends
    assert time.monotonic() - t < 6


def test_sse_thread_exits_on_client_disconnect(server, monkeypatch):
    from app.dash import common          # where the handler reads it
    monkeypatch.setattr(common, "SSE_MAX_CLIENTS", 1)
    base, _ = server()
    host, port = base.split("//")[1].split(":")
    req = (f"GET /api/events HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n"
           ).encode()
    s = socket.create_connection((host, int(port)), timeout=5)
    s.sendall(req)
    assert b"200" in s.recv(200)
    # the one slot is taken
    assert httpx.get(base + "/api/events", timeout=5).status_code == 503
    s.close()                                     # browser tab closed
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            with httpx.stream("GET", base + "/api/events", timeout=2) as r:
                if r.status_code == 200:
                    break
        except httpx.ReadTimeout:
            break                                 # 200 + streaming = freed
        time.sleep(0.3)
    else:
        pytest.fail("SSE slot never freed after the client went away")


# ── Low: honest venue display ──────────────────────────────────────────────
def test_venue_never_claims_mcp_live(server):
    base, _ = server(env={"ALPACA_PAPER_KEY": "k", "ALPACA_PAPER_SECRET": "s"})
    with paired(base) as c:
        g = c.get("/api/agent-graph").json()
    b = next(x for x in g["brokers"] if x["broker"] == "alpaca")
    assert b["adapter"] == "paper-sim"
    assert "live" not in b["detail"].lower()
    assert "paper sim" in b["detail"]
    assert b["mcp"]["used_for_execution"] is False
    src = (WEB / "index.html").read_text()
    assert "mcp ●" not in src


# ── H3: tickers and escaping ───────────────────────────────────────────────
@pytest.mark.parametrize("ticker", ["NVDA", "BRK.B", "RDS-A", "C07", "0700",
                                    "7203", "M&M", "vod"])
def test_real_tickers_pass(ticker):
    validate_symbol(ticker, "NASDAQ")


@pytest.mark.parametrize("ticker", [
    "<img src=x onerror=alert(1)>", "A'B", 'A"B', "A B", "", "../etc",
    "A..B", ".NVDA", "x" * 21, "NV\nDA", "NVDA')", None, 5])
def test_markup_tickers_are_refused(ticker):
    with pytest.raises(ValueError):
        validate_symbol(ticker, "NASDAQ")


@pytest.mark.parametrize("ex", ["", "nasdaq", "<b>", "NAS DAQ", "X", None])
def test_bad_exchanges_are_refused(ex):
    with pytest.raises(ValueError):
        validate_symbol("NVDA", ex)


def test_desk_refuses_before_creating_a_work_item():
    """The row persists even when a run fails, so validation must come
    before create_work_item — checked without building a real graph."""
    class NoRepo:
        def create_work_item(self, *a, **k):
            raise AssertionError("work item created for a bad ticker")

    class FakeDesk:
        repo, clock, conn = NoRepo(), staticmethod(lambda: T0), None

    inst = Instrument(id="NASDAQ:x", ticker="<svg/onload=alert(1)>",
                      exchange="NASDAQ", currency="USD")
    with pytest.raises(ValueError):
        Desk.start_run(FakeDesk(), inst, background=False)


def test_dashboard_run_route_validates_before_forwarding(web, no_upstream):
    base, conn = web
    with paired(base) as c:
        for body in ({"ticker": "<b>x</b>", "exchange": "NASDAQ",
                      "currency": "USD"},
                     {"ticker": "NVDA", "exchange": "NASDAQ'",
                      "currency": "USD"},
                     {"ticker": "NVDA", "exchange": "NASDAQ",
                      "currency": "<x>"},
                     {"ticker": "NVDA", "exchange": "NASDAQ",
                      "currency": "USD", "kind": "override"},
                     {"ticker": "NVDA", "exchange": "NASDAQ",
                      "currency": "USD", "lot_size": -1}):
            assert c.post("/api/run", json=body).status_code == 400, body
        assert c.post("/api/watchlist", json={
            "ticker": "<b>", "exchange": "NASDAQ"}).status_code == 400
        assert no_upstream == []
        # a valid request is forwarded (and claire-api being down is a 502)
        assert c.post("/api/run", json={
            "ticker": "NVDA", "exchange": "NASDAQ", "currency": "USD"}
        ).status_code == 502
        assert len(no_upstream) == 1


def test_esc_helper_escapes_attribute_context():
    js = (WEB / "nav.js").read_text()
    m = re.search(r"const esc = s => (.+?);\n", js, re.S)
    assert m, "esc helper missing"
    for ch, ent in (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"),
                    ('"', "&quot;"), ("'", "&#39;")):
        assert ent in m.group(1), ch
    assert "const jsArg" in js and "JSON.stringify" in js


def test_pages_do_not_interpolate_raw_server_strings():
    """The concrete H3 sinks, plus the pattern behind them: a server value
    dropped into a quoted inline-handler argument."""
    pages = {p.name: p.read_text() for p in WEB.glob("*.html")}
    raw_sinks = {
        "agents.html": ["${p.display_name}", "${a.model}", "'${a.id}'",
                        "${a.display_name || a.id}"],
        "index.html": ['value="${a.model}"', "${pr.display_name}", "'${id}'",
                       "'${r.work_item_id"],
        "trading.html": ["'${r.id}'", 'id="d-${r.id}"', 'id="dc-${r.id}"'],
        "approvals.html": ["'${a.id}'", "'${a.token}'", "'${r.id}'"],
        "providers.html": ["'${p.id}'", "'${m.id}'", "${p.kind}"],
        "portfolio.html": ["'${id}'", "'${a.id}'"],
    }
    for page, sinks in raw_sinks.items():
        for s in sinks:
            assert s not in pages[page], f"{page} still renders {s} raw"
    handler_arg = re.compile(r"""on\w+="[^"]*\('\$\{(?!k\})""")
    for name, src in pages.items():
        assert not handler_arg.search(src), name


def test_streaming_posts_send_the_dash_key():
    """/api/ask and /api/agent-ask bypass api() (they stream), so they must
    add the header themselves or the paired UI breaks."""
    for page, route in (("claire.html", "/api/ask"),
                        ("index.html", "/api/agent-ask")):
        src = (WEB / page).read_text()
        i = src.index(f"fetch('{route}'")
        assert "'X-Dash-Key': dashKey()" in src[i:i + 300], page
