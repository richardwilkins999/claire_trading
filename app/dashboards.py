"""Dashboards server (:7787, stdlib, no build step — DESIGN.md §13) + the
price watcher loop (§15). Serves pages from web/, JSON APIs over desk.db, and
is the ONLY component that converts a human click into a resume (§10).
"""
import json
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

from . import approvals, sessions
from .providers import health as provider_health
from .providers import registry

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
PAGES = {"/": "index.html", "/portfolio": "portfolio.html",
         "/markets": "markets.html", "/trading": "trading.html",
         "/agents": "agents.html", "/providers": "providers.html"}


def create_server(conn, repo, market, *, api_base="http://127.0.0.1:7788",
                  secret="", clock=time.time, port=7787, env=None):
    resume_post = approvals.default_resume_post(api_base, secret)
    env = env or {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):               # quiet; systemd has the journal
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

        # ── GET ──────────────────────────────────────────────────────────
        def do_GET(self):                        # noqa: N802
            try:
                u = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                if u.path in PAGES:
                    return self._send(200, (WEB / PAGES[u.path]).read_bytes(),
                                      "text/html; charset=utf-8")
                if u.path == "/style.css":
                    return self._send(200, (WEB / "style.css").read_bytes(),
                                      "text/css")
                if u.path == "/nav.js":
                    return self._send(200, (WEB / "nav.js").read_bytes(),
                                      "text/javascript")
                if u.path == "/api/overview":
                    return self._send(200, self._overview())
                if u.path == "/api/approvals":
                    return self._send(200, self._approvals())
                if u.path == "/api/portfolio":
                    return self._send(200, self._portfolio())
                if u.path == "/api/runs":
                    return self._send(200, self._runs())
                if u.path == "/api/agents":
                    return self._send(200, self._agents())
                if u.path == "/api/providers":
                    return self._send(200, self._providers())
                if u.path == "/api/quote":
                    syms = q.get("symbols", "").split(",")
                    return self._send(200, market.spark([s for s in syms if s]))
                if u.path == "/api/search":
                    return self._send(200, market.search(q.get("q", "")))
                if u.path == "/api/chart":
                    return self._send(200, market.chart(
                        q.get("symbol", ""), q.get("range", "6mo")))
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
                body = self._json_body()
                if u.path == "/api/thesis-action":
                    try:
                        out = approvals.thesis_action(conn, body, resume_post,
                                                      clock=clock)
                        return self._send(200, out)
                    except approvals.AuthError as e:
                        return self._send(e.code, {"error": e.message})
                if u.path == "/api/run":
                    r = httpx.post(f"{api_base}/api/run", json=body, timeout=10)
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

        # ── views ────────────────────────────────────────────────────────
        def _overview(self):
            states = {r["state"]: r["n"] for r in conn.execute(
                "SELECT state, COUNT(*) n FROM work_items GROUP BY state")}
            healthrows = {}
            for r in conn.execute(
                    "SELECT service, ok, checked_at, MAX(checked_at) FROM"
                    " service_health GROUP BY service"):
                stale = clock() - r["checked_at"] > 1800
                healthrows[r["service"]] = {
                    "ok": "stale" if stale else r["ok"],
                    "checked_at": r["checked_at"]}
            cash = [{"account": a["id"],
                     "cash": repo.cash_balance(a["id"]) / 1e6,
                     "ccy": a["base_currency"]}
                    for a in conn.execute("SELECT * FROM broker_accounts")]
            try:
                api_ok = httpx.get(f"{api_base}/status",
                                   timeout=3).status_code == 200
            except Exception:                    # noqa: BLE001
                api_ok = False
            return {"work_items": states, "health": healthrows, "cash": cash,
                    "claire_api": api_ok,
                    "pending": states.get("awaiting_approval", 0)}

        def _approvals(self):
            out = []
            for r in conn.execute(
                    "SELECT * FROM work_items WHERE state='awaiting_approval'"
                    " ORDER BY created_at DESC"):
                out.append({
                    "id": r["id"], "kind": r["kind"], "ticker": r["ticker"],
                    "thesis": json.loads(r["thesis_json"] or "{}"),
                    # THE CARD CARRIES THE TOKEN (§10): this API is the card.
                    "token": r["approval_token"],
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

        def _agents(self):
            out = []
            for a in conn.execute("SELECT * FROM agents"):
                run = conn.execute(
                    "SELECT model, cost_usd, tokens_in, tokens_out, status,"
                    " started_at FROM agent_runs WHERE agent_id=?"
                    " ORDER BY started_at DESC LIMIT 1", (a["id"],)).fetchone()
                out.append({"id": a["id"], "display_name": a["display_name"],
                            "provider_id": a["provider_id"], "model": a["model"],
                            "temperature": a["temperature"],
                            "fallback_provider_id": a["fallback_provider_id"],
                            "enabled": a["enabled"],
                            "tools": json.loads(a["tools"]),
                            "requires": json.loads(a["requires"]),
                            "last_run": dict(run) if run else None})
            return out

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


def main():
    import os

    from .accounting import db
    from .accounting.repo import Repo
    from .api.server import ROOT as API_ROOT
    from .api.server import load_env
    from .tools.market import Market

    load_env()
    var = API_ROOT / "var"
    conn = db.connect(var / "desk.db")
    db.init(conn)
    repo = Repo(conn)
    market = Market()
    secret = os.environ.get("CLAIRE_INTERNAL_SECRET", "")
    cal = sessions.load(conn) or None
    start_watcher_thread(conn, repo, market,
                         api_base="http://127.0.0.1:7788", cal=cal)
    srv = create_server(conn, repo, market, secret=secret, env=os.environ)
    print("dashboards on http://127.0.0.1:7787")
    srv.serve_forever()


if __name__ == "__main__":
    main()
