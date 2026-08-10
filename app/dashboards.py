"""Dashboards server (:7787, stdlib, no build step — DESIGN.md §13) + the
price watcher loop (§15). Serves pages from web/, JSON APIs over desk.db, and
is the ONLY component that converts a human click into a resume (§10).

Pairing: approval tokens are released only to a browser that presents the
dashboard key (X-Dash-Key, pasted once from etc/claire.env) — closing the
"any localhost curl can read the card API" hole.
"""
import json
import threading
import time
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

from . import approvals, sessions
from .providers import health as provider_health
from .providers import registry
from .tools import tradingview
from .tools.brokers import MCP_BROKERS

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
PAGES = {"/": "index.html", "/portfolio": "portfolio.html",
         "/markets": "markets.html", "/trading": "trading.html",
         "/agents": "agents.html", "/providers": "providers.html",
         "/claire": "claire.html"}
STATIC = {"/style.css": ("style.css", "text/css"),
          "/nav.js": ("nav.js", "text/javascript"),
          "/charts.js": ("charts.js", "text/javascript")}

PIPELINE_EDGES = [("prepare", "fundamental"), ("prepare", "technical"),
                  ("prepare", "news"), ("fundamental", "bull"),
                  ("technical", "bull"), ("news", "bull"), ("bull", "bear"),
                  ("bear", "arbiter"), ("arbiter", "gate"),
                  ("gate", "execute"), ("execute", "record")]


def create_server(conn, repo, market, *, api_base="http://127.0.0.1:7788",
                  secret="", dash_key="", narratives=None, clock=time.time,
                  port=7787, env=None, cal=None):
    resume_post = approvals.default_resume_post(api_base, secret)
    env = env or {}
    status_cache = {"t": 0.0, "ok": False}
    status_lock = threading.Lock()

    def api_ok():
        with status_lock:
            now = clock()
            if now - status_cache["t"] > 10:        # pressure-test finding #1:
                try:                                # never probe per-request
                    status_cache["ok"] = httpx.get(
                        f"{api_base}/status", timeout=2).status_code == 200
                except Exception:                   # noqa: BLE001
                    status_cache["ok"] = False
                status_cache["t"] = now
            return status_cache["ok"]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        # ── plumbing ─────────────────────────────────────────────────────
        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(
                body, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _json_body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def _paired(self):
            return bool(dash_key) and \
                self.headers.get("X-Dash-Key") == dash_key

        # ── GET ──────────────────────────────────────────────────────────
        def do_GET(self):                        # noqa: N802
            try:
                u = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                if u.path in PAGES:
                    return self._send(200, (WEB / PAGES[u.path]).read_bytes(),
                                      "text/html; charset=utf-8")
                if u.path in STATIC:
                    name, ctype = STATIC[u.path]
                    return self._send(200, (WEB / name).read_bytes(), ctype)
                route = {
                    "/api/overview": self._overview,
                    "/api/approvals": self._approvals,
                    "/api/portfolio": self._portfolio,
                    "/api/runs": self._runs,
                    "/api/agents": self._agents,
                    "/api/providers": self._providers,
                    "/api/agent-graph": self._agent_graph,
                }.get(u.path)
                if route:
                    return self._send(200, route())
                if u.path == "/api/exchange-info":
                    return self._send(200, self._exchange_info(
                        q.get("exchange", "NASDAQ")))
                if u.path == "/api/screener":
                    return self._send(200, market.screener(
                        q.get("exchange", "NASDAQ"),
                        q.get("sort", "intradaymarketcap"),
                        int(q.get("start", 0)), int(q.get("count", 100))))
                if u.path == "/api/tv-recs":
                    return self._send(200, tradingview.recommendations(
                        q.get("exchange", "NASDAQ")))
                if u.path == "/api/run-detail":
                    return self._send(200, self._run_detail(q.get("id", "")))
                if u.path == "/api/quote":
                    syms = [s for s in q.get("symbols", "").split(",") if s]
                    return self._send(200, market.spark(syms))
                if u.path == "/api/search":
                    return self._send(200, market.search(q.get("q", "")))
                if u.path == "/api/chart":
                    return self._send(200, market.chart(
                        q.get("symbol", ""), q.get("range", "6mo"),
                        q.get("interval", "1d")))
                if u.path == "/api/feed":
                    rows = [dict(r) for r in conn.execute(
                        "SELECT * FROM events WHERE id > ? ORDER BY id"
                        " LIMIT 300", (int(q.get("since", 0)),))]
                    return self._send(200, {"events": rows})
                self._send(404, {"error": "not found"})
            except Exception as e:               # noqa: BLE001
                traceback.print_exc()
                self._send(500, {"error": str(e)})

        # ── POST ─────────────────────────────────────────────────────────
        def do_POST(self):                       # noqa: N802
            try:
                u = urlparse(self.path)
                if u.path == "/api/ask":
                    return self._ask_proxy(self._json_body())
                body = self._json_body()
                if u.path == "/api/thesis-action":
                    if dash_key and not self._paired():
                        return self._send(403, {"error":
                                                "dashboard not paired — enter "
                                                "the dashboard key first"})
                    try:
                        out = approvals.thesis_action(conn, body, resume_post,
                                                      clock=clock)
                        return self._send(200, out)
                    except approvals.AuthError as e:
                        return self._send(e.code, {"error": e.message})
                if u.path == "/api/run":
                    r = httpx.post(f"{api_base}/api/run", json=body,
                                   timeout=10)
                    return self._send(r.status_code, r.json())
                if u.path == "/api/agent":
                    return self._assign_agent(body)
                if u.path == "/api/provider-test":
                    ok = provider_health.probe(conn, body["provider_id"])
                    latest = provider_health.latest(conn).get(
                        body["provider_id"])
                    return self._send(200, dict(latest) if latest else
                                      {"ok": ok})
                self._send(404, {"error": "not found"})
            except Exception as e:               # noqa: BLE001
                traceback.print_exc()
                self._send(500, {"error": str(e)})

        def _ask_proxy(self, body):
            """Stream Claire's NDJSON straight through to the browser."""
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

            def line(obj):
                self.wfile.write(json.dumps(obj).encode() + b"\n")
                self.wfile.flush()
            try:
                with httpx.stream("POST", f"{api_base}/ask", json=body,
                                  timeout=180) as r:
                    if r.status_code != 200:
                        detail = r.read().decode()[:300]
                        line({"kind": "error",
                              "text": f"claire-api {r.status_code}: {detail}"})
                        line({"kind": "done"})
                        return
                    for chunk in r.iter_raw():
                        self.wfile.write(chunk)
                        self.wfile.flush()
            except Exception as e:               # noqa: BLE001
                try:
                    line({"kind": "error", "text": str(e)[:300]})
                    line({"kind": "done"})
                except Exception:                # noqa: BLE001
                    pass

        # ── views ────────────────────────────────────────────────────────
        def _overview(self):
            states = {r["state"]: r["n"] for r in conn.execute(
                "SELECT state, COUNT(*) n FROM work_items GROUP BY state")}
            healthrows = {}
            for r in conn.execute(
                    "SELECT service, ok, MAX(checked_at) AS checked_at"
                    " FROM service_health GROUP BY service"):
                stale = clock() - r["checked_at"] > 1800
                healthrows[r["service"]] = {
                    "ok": "stale" if stale else r["ok"],
                    "checked_at": r["checked_at"]}
            cash = [{"account": a["id"],
                     "cash": repo.cash_balance(a["id"]) / 1e6,
                     "ccy": a["base_currency"]}
                    for a in conn.execute("SELECT * FROM broker_accounts")]
            up = api_ok()
            flags = []
            if not up:
                flags.append("claire-api :7788 is down")
            if not env.get("ANTHROPIC_API_KEY"):
                flags.append("ANTHROPIC_API_KEY missing — LLM runs will fail"
                             " (add to etc/claire.env, restart claire-api)")
            for pid, h in provider_health.latest(conn).items():
                if h and not h["ok"]:
                    flags.append(f"provider {pid} failing its health probe")
            for svc, h in healthrows.items():
                if h["ok"] != "ok":
                    flags.append(f"{svc} is {h['ok']}")
            day_ago = int(clock()) - 86400
            (nfail,) = conn.execute(
                "SELECT COUNT(*) FROM work_items WHERE state='failed'"
                " AND updated_at > ?", (day_ago,)).fetchone()
            if nfail:
                flags.append(f"{nfail} failed run(s) in the last 24h —"
                             " see Trading")
            return {"work_items": states, "health": healthrows, "cash": cash,
                    "claire_api": up, "flags": flags,
                    "pending": states.get("awaiting_approval", 0)}

        def _approvals(self):
            paired = self._paired() or not dash_key
            out = []
            now = int(clock())
            for r in conn.execute(
                    "SELECT * FROM work_items WHERE state='awaiting_approval'"
                    " ORDER BY created_at DESC"):
                if r["expires_at"] and now >= r["expires_at"]:
                    continue                     # expiry on every read path §12
                out.append({
                    "id": r["id"], "kind": r["kind"], "ticker": r["ticker"],
                    "thesis": json.loads(r["thesis_json"] or "{}"),
                    # tokens ONLY for a paired browser (§10 rev2)
                    "token": r["approval_token"] if paired else None,
                    "locked": not paired,
                    "expires_at": r["expires_at"]})
            return out

        def _portfolio(self):
            accounts = []
            for a in conn.execute("SELECT * FROM broker_accounts"):
                positions = []
                for p in conn.execute(
                        "SELECT l.instrument_id, i.ticker, i.exchange,"
                        " SUM(CASE WHEN l.qty_opened<0 THEN -l.qty_remaining"
                        "     ELSE l.qty_remaining END)/1e6 qty,"
                        " SUM(l.qty_remaining*l.cost_per_share_base/1e12)"
                        "  cost,"
                        " SUM(l.commission_allocated)/1e6 comm"
                        " FROM lots l JOIN instruments i ON"
                        "  i.id=l.instrument_id"
                        " WHERE l.account_id=? AND l.qty_remaining>0"
                        " GROUP BY l.instrument_id", (a["id"],)):
                    positions.append(dict(p))
                accounts.append({
                    "id": a["id"], "broker": a["broker"],
                    "ccy": a["base_currency"],
                    "cash": repo.cash_balance(a["id"]) / 1e6,
                    "realized_pl": repo.realized_pl(a["id"]) / 1e6,
                    "positions": positions})
            alerts = [dict(r) for r in conn.execute(
                "SELECT * FROM price_alerts ORDER BY instrument_id")]
            return {"accounts": accounts, "alerts": alerts}

        def _runs(self):
            rows = []
            for r in conn.execute(
                    "SELECT * FROM work_items ORDER BY created_at DESC"
                    " LIMIT 50"):
                d = dict(r)
                d.pop("approval_token", None)    # tokens live on cards only
                rows.append(d)
            return rows

        def _run_detail(self, wi):
            row = conn.execute("SELECT * FROM work_items WHERE id=?",
                               (wi,)).fetchone()
            if row is None:
                return {"error": "unknown work item"}
            d = dict(row)
            d.pop("approval_token", None)
            d["thesis"] = json.loads(d.pop("thesis_json") or "null")
            d["events"] = [dict(e) for e in conn.execute(
                "SELECT ts, actor, from_state, to_state, payload FROM events"
                " WHERE item_id=? ORDER BY id", (wi,))]
            d["agent_runs"] = [dict(e) for e in conn.execute(
                "SELECT agent_id, model, status, cost_usd, tokens_in,"
                " tokens_out, started_at FROM agent_runs WHERE work_item_id=?"
                " ORDER BY started_at", (wi,))]
            d["orders"] = [dict(o) for o in conn.execute(
                "SELECT id, side, qty/1e6 qty, status, broker_order_id,"
                " expires_at FROM orders WHERE work_item_id=?", (wi,))]
            d["narratives"] = narratives.list(wi) if narratives else []
            if narratives and "summary.md" in d["narratives"]:
                d["summary"] = narratives.read(wi, "summary")
            return d

        def _agents(self):
            out = []
            for a in conn.execute("SELECT * FROM agents"):
                run = conn.execute(
                    "SELECT model, cost_usd, tokens_in, tokens_out, status,"
                    " started_at, ended_at FROM agent_runs WHERE agent_id=?"
                    " ORDER BY started_at DESC LIMIT 1", (a["id"],)).fetchone()
                out.append({"id": a["id"], "display_name": a["display_name"],
                            "provider_id": a["provider_id"], "model": a["model"],
                            "temperature": a["temperature"],
                            "fallback_provider_id": a["fallback_provider_id"],
                            "enabled": a["enabled"],
                            "tools": json.loads(a["tools"]),
                            "requires": json.loads(a["requires"]),
                            "system_prompt": a["system_prompt"],
                            "last_run": dict(run) if run else None})
            return out

        def _agent_graph(self):
            now = int(clock())
            agents = {}
            for a in self._agents():
                lr = a["last_run"] or {}
                active = (lr.get("status") == "running" and
                          now - (lr.get("started_at") or 0) < 600)
                state = ("disabled" if not a["enabled"] else
                         "active" if active else
                         "error" if lr.get("status") == "error" else
                         "ok" if lr else "idle")
                agents[a["id"]] = {**a, "state": state}
            brokers = []
            for acct in conn.execute("SELECT * FROM broker_accounts"):
                (open_orders,) = conn.execute(
                    "SELECT COUNT(*) FROM orders WHERE account_id=? AND"
                    " status IN ('pending_session','placed',"
                    "'partially_filled')", (acct["id"],)).fetchone()
                mcp = MCP_BROKERS.get(acct["broker"])
                creds = all(env.get(ref) for ref in mcp.env_refs) \
                    if mcp and mcp.env_refs else False
                brokers.append({
                    "broker": acct["broker"], "account": acct["id"],
                    "environment": acct["environment"],
                    "cash": repo.cash_balance(acct["id"]) / 1e6,
                    "ccy": acct["base_currency"],
                    "open_orders": open_orders,
                    "adapter": "mcp" if creds else "paper-sim",
                    "mcp": {"command": " ".join(mcp.command),
                            "env_refs": mcp.env_refs,
                            "creds_present": creds,
                            "note": mcp.note} if mcp else None})
            return {"agents": agents, "edges": PIPELINE_EDGES,
                    "brokers": brokers, "claire_api": api_ok(),
                    "market_data": {"source": "Yahoo Finance",
                                    "stale_keys": len(market._stale_keys)
                                    if hasattr(market, "_stale_keys") else 0}}

        def _exchange_info(self, exchange):
            cal = cal_or_default()
            s = cal.get(exchange)
            if s is None:
                return {"error": f"unknown exchange {exchange}"}
            now = datetime.fromtimestamp(int(clock()), tz=timezone.utc)
            info = {"exchange": exchange, "tz": s.tz,
                    "open_time": s.open_time, "close_time": s.close_time,
                    "lunch_break": s.lunch_break,
                    "holidays": sorted(s.holidays),
                    "is_open": sessions.is_open(exchange, now, cal),
                    "next_open": sessions.next_open(exchange, now,
                                                    cal).isoformat(),
                    "session_close": sessions.close_of(exchange, now,
                                                       cal).isoformat()}
            info.update(market.exchange_metrics(exchange))
            return info

        def _providers(self):
            latest = provider_health.latest(conn)
            out = []
            for p in conn.execute("SELECT * FROM providers"):
                h = latest.get(p["id"])
                out.append({"id": p["id"], "display_name": p["display_name"],
                            "kind": p["kind"], "base_url": p["base_url"],
                            "key_ref": p["api_key_ref"],
                            "key_present": bool(
                                p["api_key_ref"] and
                                (env or {}).get(p["api_key_ref"])),
                            "capabilities": json.loads(p["capabilities"]),
                            "enabled": p["enabled"],
                            "health": dict(h) if h else None})
            return out

        def _assign_agent(self, body):
            try:
                registry.assign(conn, body["id"], body["provider_id"],
                                body["model"],
                                fallback_provider_id=body.get(
                                    "fallback_provider_id"),
                                fallback_model=body.get("fallback_model"),
                                ts=int(clock()))
                return self._send(200, {"ok": True})
            except registry.CapabilityError as e:
                return self._send(400, {"error": str(e)})

    def cal_or_default():
        return cal or sessions.DEFAULTS

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def start_watcher_thread(conn, repo, market, *, api_base, interval=600,
                         clock=time.time, cal=None):
    """The §15 loop lives in the dashboards process; sell-review launches go
    through claire-api's HTTP seam with a SHORT timeout (§15 lesson 8)."""
    from .watcher import Watcher

    def start_sell_review(inst):
        try:
            r = httpx.post(f"{api_base}/api/run",
                           json={"ticker": inst["ticker"],
                                 "exchange": inst["exchange"],
                                 "currency": inst["currency"],
                                 "instrument_id": inst["id"],
                                 "kind": "sell_review"}, timeout=10)
            return r.json().get("work_item_id")
        except Exception:                        # noqa: BLE001
            return None

    watcher = Watcher(conn, repo, market, start_sell_review, clock=clock,
                      cal=cal)

    def loop():
        while True:
            try:
                watcher.tick()
            except Exception:                    # noqa: BLE001
                traceback.print_exc()
            time.sleep(interval)

    t = threading.Thread(target=loop, daemon=True, name="watcher")
    t.start()
    return t


def ensure_dash_key(env_path):
    """CLAIRE_DASHBOARD_KEY: generated once, lives only in claire.env and the
    user's browser (pasted on first visit)."""
    import os
    import secrets
    key = os.environ.get("CLAIRE_DASHBOARD_KEY", "")
    if key:
        return key
    key = secrets.token_hex(8)
    with open(env_path, "a") as f:
        f.write(f"\nCLAIRE_DASHBOARD_KEY={key}\n")
    os.environ["CLAIRE_DASHBOARD_KEY"] = key
    print(f"generated CLAIRE_DASHBOARD_KEY (see {env_path})")
    return key


def main():
    import os

    from .accounting import db
    from .accounting.repo import Repo
    from .api.server import ROOT as API_ROOT
    from .api.server import load_env
    from .tools.files import Narratives
    from .tools.market import Market

    load_env()
    var = API_ROOT / "var"
    conn = db.connect(var / "desk.db")
    db.init(conn)
    repo = Repo(conn)
    market = Market()
    secret = os.environ.get("CLAIRE_INTERNAL_SECRET", "")
    dash_key = ensure_dash_key(API_ROOT / "etc" / "claire.env")
    cal = sessions.load(conn) or None
    start_watcher_thread(conn, repo, market,
                         api_base="http://127.0.0.1:7788", cal=cal)
    srv = create_server(conn, repo, market, secret=secret, dash_key=dash_key,
                        narratives=Narratives(var / "narratives"),
                        env=os.environ, cal=cal)
    print("dashboards on http://127.0.0.1:7787")
    srv.serve_forever()


if __name__ == "__main__":
    main()
