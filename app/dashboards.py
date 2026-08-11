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

from . import approvals, scheduler, sessions
from .providers import health as provider_health
from .providers import registry
from .tools import tradingview
from .tools.brokers import seed_mcps

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
PAGES = {"/": "index.html", "/portfolio": "portfolio.html",
         "/markets": "markets.html", "/trading": "trading.html",
         "/agents": "agents.html", "/providers": "providers.html",
         "/claire": "claire.html", "/schedule": "agents.html",  # merged
         "/approvals": "approvals.html"}
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
                  port=7787, env=None, cal=None, sse_interval=2.0):
    resume_post = approvals.default_resume_post(api_base, secret)
    env = env or {}
    status_cache = {"t": 0.0, "ok": False}
    status_lock = threading.Lock()

    # ── SSE fan-out (v1 pattern): one poller thread watches desk.db and
    #    pushes new event rows to every connected browser queue ────────────
    import queue as _queue
    sse_clients: list = []
    sse_lock = threading.Lock()

    def _push(payload):
        with sse_lock:
            clients = list(sse_clients)
        for q in clients:
            try:
                q.put_nowait(payload)
            except _queue.Full:
                pass

    def sse_poller():
        row = conn.execute("SELECT COALESCE(MAX(id),0) m FROM events").fetchone()
        last = row["m"]
        agent_sig = None
        while True:
            time.sleep(sse_interval)
            try:
                for r in [dict(r) for r in conn.execute(
                        "SELECT * FROM events WHERE id > ? ORDER BY id"
                        " LIMIT 100", (last,))]:
                    last = r["id"]
                    _push(r)
                # agent activity drives the "glowing while running" state —
                # work-item events alone are far too coarse for that
                sig = tuple(conn.execute(
                    "SELECT agent_id, status FROM agent_runs"
                    " WHERE started_at > ? ORDER BY agent_id, id",
                    (int(clock()) - 900,)).fetchall())
                if agent_sig is not None and sig != agent_sig:
                    _push({"kind": "agents"})
                agent_sig = sig
            except Exception:                    # noqa: BLE001
                pass

    if sse_interval:
        threading.Thread(target=sse_poller, daemon=True,
                         name="sse-poller").start()
    scheduler.seed(conn)
    seed_mcps(conn)

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
                    "/api/exchanges": self._exchanges,
                }.get(u.path)
                if route:
                    return self._send(200, route())
                if u.path == "/api/schedules":
                    return self._send(200, scheduler.rows_with_next(
                        conn, clock=clock))
                if u.path == "/api/mcps":
                    return self._send(200, self._mcps())
                if u.path == "/api/data-sources":
                    return self._send(200, self._data_sources())
                if u.path == "/api/position-chart":
                    from . import portfolio
                    return self._send(200, portfolio.chart_for(
                        conn, market, q.get("instrument", ""),
                        range_=q.get("range", "5d"),
                        interval=q.get("interval", "60m")))
                if u.path == "/api/watchlist":
                    return self._send(200, [dict(r) for r in conn.execute(
                        "SELECT * FROM watchlist ORDER BY exchange, ticker")])
                if u.path == "/api/events":
                    return self._sse()
                if u.path == "/api/fx":
                    return self._send(200, {"rate": str(market.fx(
                        q.get("from", "USD"), q.get("to", "USD")))})
                if u.path == "/api/agent-activity":
                    return self._send(200, self._agent_activity(
                        q.get("id", "")))
                if u.path == "/api/agent-narrative":
                    return self._send(200, self._narrative(
                        q.get("agent", ""), q.get("work_item", "")) or
                        {"error": "no narrative for that run"})
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
            except (BrokenPipeError, ConnectionResetError):
                return                           # client went away; not news
            except Exception as e:               # noqa: BLE001
                traceback.print_exc()
                try:
                    self._send(500, {"error": str(e)})
                except (BrokenPipeError, ConnectionResetError):
                    pass

        # ── POST ─────────────────────────────────────────────────────────
        def do_POST(self):                       # noqa: N802
            try:
                u = urlparse(self.path)
                if u.path == "/api/ask":
                    return self._ask_proxy(self._json_body(), "/ask")
                if u.path == "/api/agent-ask":
                    return self._ask_proxy(self._json_body(), "/agent/ask")
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
                if u.path == "/api/schedule":
                    try:
                        scheduler.update(conn, body["job"],
                                         enabled=body.get("enabled"),
                                         spec_patch=body.get("spec"))
                        return self._send(200, {"ok": True})
                    except (KeyError, ValueError) as e:
                        return self._send(400, {"error": str(e)})
                if u.path == "/api/provider":
                    return self._upsert_provider(body)
                if u.path == "/api/mcp":
                    return self._upsert_mcp(body)
                if u.path == "/api/account-action":
                    return self._account_action(body)
                if u.path == "/api/watchlist":
                    t = str(body.get("ticker", "")).upper().strip()
                    ex = str(body.get("exchange", "")).upper().strip()
                    if not t or not ex:
                        return self._send(400, {"error":
                                                "ticker and exchange required"})
                    if body.get("action") == "remove":
                        conn.execute("DELETE FROM watchlist WHERE ticker=?"
                                     " AND exchange=?", (t, ex))
                    else:
                        conn.execute(
                            "INSERT OR REPLACE INTO watchlist (ticker,"
                            " exchange, note, added_at) VALUES (?,?,?,?)",
                            (t, ex, body.get("note"), int(clock())))
                    return self._send(200, {"ok": True})
                self._send(404, {"error": "not found"})
            except (BrokenPipeError, ConnectionResetError):
                return                           # client went away; not news
            except Exception as e:               # noqa: BLE001
                traceback.print_exc()
                try:
                    self._send(500, {"error": str(e)})
                except (BrokenPipeError, ConnectionResetError):
                    pass

        def _ask_proxy(self, body, upstream="/ask"):
            """Stream claire-api NDJSON straight through to the browser."""
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

            def line(obj):
                self.wfile.write(json.dumps(obj).encode() + b"\n")
                self.wfile.flush()
            try:
                with httpx.stream("POST", f"{api_base}{upstream}", json=body,
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

        def _sse(self):
            q = _queue.Queue(maxsize=500)
            with sse_lock:
                sse_clients.append(q)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            try:
                while True:
                    try:
                        ev = q.get(timeout=20)
                        self.wfile.write(
                            f"data: {json.dumps(ev, default=str)}\n\n".encode())
                    except _queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                with sse_lock:
                    if q in sse_clients:
                        sse_clients.remove(q)

        def _narrative(self, agent_id, work_item):
            """One run's output, split the way a reader wants it: the REPORT
            (the conclusion, and the only thing passed downstream) separate
            from the TRANSCRIPT (how it got there)."""
            from .narrative_format import format_narrative
            if not narratives or not work_item:
                return None

            def read(name):
                try:
                    return narratives.read(work_item, name)
                except OSError:
                    return None

            report, transcript = read(f"{agent_id}.report"), \
                read(f"{agent_id}.transcript")
            if report is None and transcript is None:
                return None
            return {"work_item": work_item, "agent": agent_id,
                    "report": format_narrative(report)[:8000]
                    if report else None,
                    "transcript": format_narrative(transcript)[:12000]
                    if transcript else None}

        def _exchanges(self):
            """The world-markets strip: pure session-calendar data — no Yahoo,
            immune to rate limits, always instant."""
            cal = cal_or_default()
            now = datetime.fromtimestamp(int(clock()), tz=timezone.utc)
            out = []
            for ex in ("SGX", "HKEX", "TSE", "ASX", "NSE", "LSE", "XETRA",
                       "PARIS", "NYSE", "NASDAQ"):
                s = cal.get(ex)
                if s is None:
                    continue
                is_open = sessions.is_open(ex, now, cal)
                lunch = False
                if s.lunch_break and not is_open:
                    nxt = sessions.next_open(ex, now, cal)
                    lunch = nxt.date() == now.astimezone(nxt.tzinfo).date() \
                        and nxt.time().isoformat()[:5] == \
                        s.lunch_break.split("-")[1]
                out.append({"exchange": ex, "tz": s.tz, "is_open": is_open,
                            "at_lunch": lunch,
                            "open_time": s.open_time,
                            "close_time": s.close_time,
                            "lunch_break": s.lunch_break,
                            "next_open": sessions.next_open(ex, now,
                                                            cal).isoformat(),
                            "session_close": sessions.close_of(
                                ex, now, cal).isoformat()})
            return out

        def _agent_activity(self, agent_id):
            """What is this agent doing? Recent LLM runs, the work items they
            belong to, and which of them left a narrative to read."""
            runs = [dict(r) for r in conn.execute(
                "SELECT r.*, w.ticker, w.state AS wi_state, w.trigger"
                " FROM agent_runs r"
                " LEFT JOIN work_items w ON w.id = r.work_item_id"
                " WHERE r.agent_id=? ORDER BY r.started_at DESC LIMIT 5",
                (agent_id,))]
            current = next((r for r in runs if r["status"] == "running"), None)
            for r in runs:
                files = (narratives.list(r["work_item_id"])
                         if r["work_item_id"] and narratives else [])
                r["has_narrative"] = any(
                    f in files for f in (f"{agent_id}.report.md",
                                         f"{agent_id}.transcript.md"))
            narrative = None
            for r in runs:
                if r.get("has_narrative"):
                    narrative = self._narrative(agent_id, r["work_item_id"])
                    break
            (cost_today,) = conn.execute(
                "SELECT COALESCE(SUM(cost_usd),0) FROM agent_runs"
                " WHERE agent_id=? AND started_at > ?",
                (agent_id, int(clock()) - 86400)).fetchone()
            reports = {}
            for rep in conn.execute(
                    "SELECT work_item_id, payload FROM agent_reports"
                    " WHERE agent_id=?", (agent_id,)):
                try:
                    reports[rep["work_item_id"]] = json.loads(rep["payload"])
                except ValueError:
                    pass
            for r in runs:
                r["report"] = reports.get(r["work_item_id"])
            return {"agent_id": agent_id, "current": current, "runs": runs,
                    "narrative": narrative, "cost_24h": cost_today}

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
            cal = cal_or_default()
            nowdt = datetime.fromtimestamp(int(clock()), tz=timezone.utc)
            accounts = [{
                "id": a["id"], "broker": a["broker"],
                "ccy": a["base_currency"],
                "cash": repo.cash_balance(a["id"]) / 1e6,
                "fee_model": json.loads(a["fee_model"])}
                for a in conn.execute("SELECT * FROM broker_accounts")]
            cards = []
            now = int(clock())
            for r in conn.execute(
                    "SELECT * FROM work_items WHERE state='awaiting_approval'"
                    " ORDER BY created_at DESC"):
                if r["expires_at"] and now >= r["expires_at"]:
                    continue                     # expiry on every read path §12
                inst = conn.execute(
                    "SELECT exchange FROM instruments WHERE ticker=? LIMIT 1",
                    (r["ticker"],)).fetchone()
                exchange = inst["exchange"] if inst else None
                session = {}
                if exchange and exchange in cal:
                    session = {"exchange": exchange,
                               "is_open": sessions.is_open(exchange, nowdt,
                                                           cal),
                               "next_open": sessions.next_open(
                                   exchange, nowdt, cal).isoformat()}
                reports = []
                for rep in conn.execute(
                        "SELECT agent_id, kind, payload FROM agent_reports"
                        " WHERE work_item_id=? ORDER BY id", (r["id"],)):
                    try:
                        reports.append({"agent": rep["agent_id"],
                                        "kind": rep["kind"],
                                        "data": json.loads(rep["payload"])})
                    except ValueError:
                        pass
                costs = conn.execute(
                    "SELECT COALESCE(SUM(cost_usd),0) c, COUNT(*) n FROM"
                    " agent_runs WHERE work_item_id=?", (r["id"],)).fetchone()
                cards.append({
                    "id": r["id"], "kind": r["kind"], "ticker": r["ticker"],
                    "exchange": exchange, "trigger": r["trigger"],
                    "reports": reports,
                    "llm_cost": costs["c"], "llm_calls": costs["n"],
                    "created_at": r["created_at"],
                    "thesis": json.loads(r["thesis_json"] or "{}"),
                    # tokens ONLY for a paired browser (§10 rev2)
                    "token": r["approval_token"] if paired else None,
                    "locked": not paired,
                    "session": session,
                    "expires_at": r["expires_at"]})
            return {"cards": cards, "accounts": accounts}

        def _portfolio(self):
            from . import portfolio
            data = portfolio.build(conn, market, clock=clock, cal=cal)
            data["alerts"] = [dict(r) for r in conn.execute(
                "SELECT * FROM price_alerts ORDER BY instrument_id")]
            for a in data["accounts"]:
                a["realized_pl"] = repo.realized_pl(a["id"]) / 1e6
            return data

        def _runs(self):
            rows = []
            for r in conn.execute(
                    "SELECT * FROM work_items WHERE archived_at IS NULL"
                    " ORDER BY created_at DESC LIMIT 50"):
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
            """Every node carries a status the UI renders identically:
            active (running now) · ok (configured and proven) · warn
            (degraded) · error (cannot run / failing) · idle (configured,
            never run) · disabled."""
            now = int(clock())
            provs = {p["id"]: p for p in self._providers()}
            agents = {}
            for a in self._agents():
                lr = a["last_run"] or {}
                p = provs.get(a["provider_id"], {})
                active = (lr.get("status") == "running" and
                          now - (lr.get("started_at") or 0) < 600)
                if not a["enabled"]:
                    state, detail = "disabled", "disabled"
                elif active:
                    state, detail = "active", "running now"
                elif p.get("key_ref") and not p.get("key_present"):
                    # configured on paper but cannot actually run
                    state, detail = "error", f"{p['key_ref']} not set"
                elif not p.get("enabled", 1):
                    state, detail = "error", f"provider {a['provider_id']} off"
                elif lr.get("status") == "error":
                    state, detail = "error", (lr.get("error") or
                                              "last run failed")[:60]
                elif lr:
                    state, detail = "ok", "ready"
                else:
                    state, detail = "idle", "never run"
                agents[a["id"]] = {**a, "state": state, "detail": detail}
            brokers = []
            for acct in conn.execute("SELECT * FROM broker_accounts"):
                (open_orders,) = conn.execute(
                    "SELECT COUNT(*) FROM orders WHERE account_id=? AND"
                    " status IN ('pending_session','placed',"
                    "'partially_filled')", (acct["id"],)).fetchone()
                mcp = conn.execute(
                    "SELECT * FROM mcp_servers WHERE broker=? AND enabled=1",
                    (acct["broker"],)).fetchone()
                refs = json.loads(mcp["env_refs"]) if mcp else []
                creds = bool(refs) and all(env.get(r) for r in refs)
                if mcp and refs and not creds:
                    bstate, bdetail = "warn", "MCP configured, creds missing"
                elif creds:
                    bstate, bdetail = "ok", "MCP live"
                else:
                    bstate, bdetail = "idle", "paper sim (by design)"
                brokers.append({
                    "broker": acct["broker"], "account": acct["id"],
                    "environment": acct["environment"],
                    "cash": repo.cash_balance(acct["id"]) / 1e6,
                    "ccy": acct["base_currency"],
                    "open_orders": open_orders,
                    "state": bstate, "detail": bdetail,
                    "adapter": "mcp" if creds else "paper-sim",
                    "mcp": {"command": " ".join(json.loads(mcp["command"])),
                            "env_refs": refs,
                            "creds_present": creds,
                            "note": mcp["note"]} if mcp else None})
            # a guard that stopped reporting is an ERROR, not a shrug: a dead
            # safety net is worse than no safety net (v1 lesson §19.10)
            state_of = {"ok": "ok", "degraded": "warn", "stale": "error"}
            health = {}
            for svc in ("watcher", "custodian"):
                r = conn.execute(
                    "SELECT ok, MAX(checked_at) AS checked_at FROM"
                    " service_health WHERE service=?", (svc,)).fetchone()
                if r is None or r["checked_at"] is None:
                    health[svc] = {"ok": "never ran", "state": "idle",
                                   "checked_at": None}
                    continue
                ok = "stale" if clock() - r["checked_at"] > 1800 else r["ok"]
                health[svc] = {"ok": ok, "state": state_of.get(ok, "warn"),
                               "checked_at": r["checked_at"]}
            (open_orders,) = conn.execute(
                "SELECT COUNT(*) FROM orders WHERE status IN"
                " ('pending_session','placed','partially_filled')").fetchone()
            (positions,) = conn.execute(
                "SELECT COUNT(DISTINCT instrument_id) FROM lots"
                " WHERE qty_remaining > 0").fetchone()
            (pending,) = conn.execute(
                "SELECT COUNT(*) FROM work_items WHERE"
                " state='awaiting_approval'").fetchone()
            primary = market.primary_name() if hasattr(market, "primary_name") \
                else None
            stale = len(market._stale_keys) if hasattr(market, "_stale_keys") \
                else 0
            return {"agents": agents, "edges": PIPELINE_EDGES,
                    "brokers": brokers, "claire_api": api_ok(),
                    "mcps": self._mcps(), "health": health,
                    "book": {"open_orders": open_orders,
                             "positions": positions, "pending": pending},
                    "market_data": {
                        "source": primary or "yahoo",
                        "fallbacks": "tradingview · ecb",
                        "stale_keys": stale,
                        "state": "warn" if stale else "ok",
                        "detail": (f"{stale} symbol(s) served stale"
                                   if stale else
                                   ("keyed provider" if primary
                                    else "free source"))}}

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
                            "cost_per_1k_in": p["cost_per_1k_in"],
                            "cost_per_1k_out": p["cost_per_1k_out"],
                            "enabled": p["enabled"],
                            "health": dict(h) if h else None})
            return out

        def _data_sources(self):
            """Market-data source chain status — configure by adding the env
            vars to etc/claire.env and restarting both services."""
            primary = market.primary_name() if hasattr(market,
                                                       "primary_name") else None
            return [
                {"id": "ibkr", "name": "Interactive Brokers",
                 "role": "primary when enabled (licensed exchange data)",
                 "configured": bool((env or {}).get("IBKR_ENABLED")),
                 "active": primary == "ibkr",
                 "how": "IBKR_ENABLED=1 + IBKR_HOST/IBKR_PORT in claire.env; "
                        "IB Gateway running (paper ok) + pip install "
                        "ib-insync + per-exchange data subscriptions"},
                {"id": "twelvedata", "name": "Twelve Data",
                 "role": "primary when keyed (global quotes/charts/FX)",
                 "configured": bool((env or {}).get("TWELVEDATA_API_KEY")),
                 "active": primary == "twelvedata",
                 "how": "TWELVEDATA_API_KEY in claire.env (free tier "
                        "~800 credits/day)"},
                {"id": "yahoo", "name": "Yahoo Finance",
                 "role": "free default (paced + host-rotated; throttles)",
                 "configured": True, "active": primary is None,
                 "how": "always on"},
                {"id": "tradingview", "name": "TradingView scanner",
                 "role": "quote fallback + recommendations",
                 "configured": True, "active": True, "how": "always on"},
                {"id": "ecb", "name": "ECB / Frankfurter",
                 "role": "FX fallback (daily rates)",
                 "configured": True, "active": True, "how": "always on"},
            ]

        def _mcps(self):
            out = []
            for m in conn.execute("SELECT * FROM mcp_servers ORDER BY id"):
                refs = json.loads(m["env_refs"])
                out.append({"id": m["id"], "broker": m["broker"],
                            "command": json.loads(m["command"]),
                            "env_refs": refs,
                            "creds_present": bool(refs) and
                            all((env or {}).get(r) for r in refs),
                            "missing_refs": [r for r in refs
                                             if not (env or {}).get(r)],
                            "note": m["note"],
                            "enabled": bool(m["enabled"])})
            return out

        def _upsert_provider(self, body):
            import re
            pid = str(body.get("id", "")).strip()
            if not re.fullmatch(r"[a-z0-9_-]{2,30}", pid):
                return self._send(400, {"error":
                                        "id must be 2-30 chars [a-z0-9_-]"})
            kind = body.get("kind")
            if kind not in ("anthropic", "openai_compatible", "custom"):
                return self._send(400, {"error": "kind must be anthropic |"
                                        " openai_compatible | custom"})
            caps = body.get("capabilities") or {}
            if not isinstance(caps, dict):
                return self._send(400, {"error": "capabilities must be an"
                                        " object of booleans"})
            conn.execute(
                "INSERT INTO providers (id, display_name, kind, base_url,"
                " api_key_ref, capabilities, cost_per_1k_in, cost_per_1k_out,"
                " enabled) VALUES (?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET display_name=excluded"
                ".display_name, kind=excluded.kind, base_url=excluded.base_url,"
                " api_key_ref=excluded.api_key_ref, capabilities=excluded"
                ".capabilities, cost_per_1k_in=excluded.cost_per_1k_in,"
                " cost_per_1k_out=excluded.cost_per_1k_out,"
                " enabled=excluded.enabled",
                (pid, body.get("display_name") or pid, kind,
                 body.get("base_url"), body.get("api_key_ref"),
                 json.dumps(caps), body.get("cost_per_1k_in"),
                 body.get("cost_per_1k_out"),
                 1 if body.get("enabled", True) else 0))
            return self._send(200, {"ok": True, "id": pid})

        def _upsert_mcp(self, body):
            import re
            import shlex
            mid = str(body.get("id", "")).strip()
            if not re.fullmatch(r"[a-z0-9_-]{2,30}", mid):
                return self._send(400, {"error":
                                        "id must be 2-30 chars [a-z0-9_-]"})
            if body.get("action") == "delete":
                conn.execute("DELETE FROM mcp_servers WHERE id=?", (mid,))
                return self._send(200, {"ok": True, "deleted": mid})
            command = body.get("command")
            if isinstance(command, str):
                command = shlex.split(command)
            if not command:
                return self._send(400, {"error": "command required"})
            refs = body.get("env_refs") or []
            if isinstance(refs, str):
                refs = [r.strip() for r in refs.split(",") if r.strip()]
            if any(not re.fullmatch(r"[A-Z][A-Z0-9_]*", r) for r in refs):
                return self._send(400, {"error": "env_refs must be UPPER_CASE"
                                        " env var NAMES (values stay in"
                                        " claire.env)"})
            conn.execute(
                "INSERT INTO mcp_servers (id, broker, command, env_refs,"
                " note, enabled) VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET broker=excluded.broker,"
                " command=excluded.command, env_refs=excluded.env_refs,"
                " note=excluded.note, enabled=excluded.enabled",
                (mid, body.get("broker"), json.dumps(command),
                 json.dumps(refs), body.get("note"),
                 1 if body.get("enabled", True) else 0))
            return self._send(200, {"ok": True, "id": mid})

        def _account_action(self, body):
            """Top-up / withdraw paper cash — paired browsers only (it moves
            the ledger), every movement is a cash_transactions row."""
            from .accounting.money import to_micro
            if dash_key and not self._paired():
                return self._send(403, {"error": "dashboard not paired"})
            account = body.get("account_id", "")
            action = body.get("action")
            try:
                amount = to_micro(str(body.get("amount", 0)))
            except Exception:                    # noqa: BLE001
                return self._send(400, {"error": "amount must be a number"})
            if amount <= 0:
                return self._send(400, {"error": "amount must be positive"})
            from .accounting.repo import InsufficientCash, LedgerError
            try:
                if action == "deposit":
                    repo.deposit(account, amount, ts=int(clock()),
                                 note="dashboard top-up")
                elif action == "withdraw":
                    repo.withdraw(account, amount, ts=int(clock()),
                                  note="dashboard withdrawal")
                else:
                    return self._send(400, {"error":
                                            "action must be deposit|withdraw"})
            except InsufficientCash as e:
                return self._send(400, {"error": str(e)})
            except LedgerError as e:
                return self._send(400, {"error": str(e)})
            return self._send(200, {"ok": True,
                                    "balance": repo.cash_balance(account) / 1e6})

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

    class Server(ThreadingHTTPServer):
        def handle_error(self, request, client_address):
            """A browser closing an SSE stream is normal, not an error — it
            was filling the log with tracebacks and hiding the real ones."""
            import sys
            if isinstance(sys.exc_info()[1], (BrokenPipeError,
                                              ConnectionResetError)):
                return
            super().handle_error(request, client_address)

    return Server(("127.0.0.1", port), Handler)


def start_watcher_thread(conn, repo, market, *, api_base, interval=600,
                         clock=time.time, cal=None):
    """The §15 loop lives in the dashboards process; sell-review launches go
    through claire-api's HTTP seam with a SHORT timeout (§15 lesson 8)."""
    from .watcher import Watcher

    def start_sell_review(inst, trigger=None):
        try:
            r = httpx.post(f"{api_base}/api/run",
                           json={"ticker": inst["ticker"],
                                 "exchange": inst["exchange"],
                                 "currency": inst["currency"],
                                 "instrument_id": inst["id"],
                                 "trigger": trigger,
                                 "kind": "sell_review"}, timeout=10)
            return r.json().get("work_item_id")
        except Exception:                        # noqa: BLE001
            return None

    watcher = Watcher(conn, repo, market, start_sell_review, clock=clock,
                      cal=cal)

    def loop():
        while True:
            minutes, enabled = interval / 60, True
            try:                                 # cadence is UI-configurable
                row = conn.execute("SELECT spec, enabled FROM schedules"
                                   " WHERE job='watcher'").fetchone()
                if row:
                    minutes = json.loads(row["spec"]).get("minutes",
                                                          interval / 60)
                    enabled = bool(row["enabled"])
            except Exception:                    # noqa: BLE001
                pass
            try:
                if enabled:
                    watcher.tick()
            except Exception:                    # noqa: BLE001
                traceback.print_exc()
            time.sleep(max(60, minutes * 60))

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
    from .tools.market import build_market

    load_env()
    var = API_ROOT / "var"
    conn = db.connect(var / "desk.db")
    db.init(conn)
    repo = Repo(conn)
    market = build_market()
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
