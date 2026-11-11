"""Dashboards server (:7787, stdlib, no build step — DESIGN.md §13) + the
price watcher loop (§15). Serves pages from web/, JSON APIs over desk.db, and
is the ONLY component that converts a human click into a resume (§10).

Pairing: approval tokens are released only to a browser that presents the
dashboard key (X-Dash-Key, pasted once from etc/claire.env) — closing the
"any localhost curl can read the card API" hole. EVERY POST needs the key
too (review 2026-11-03 C1/H1): starting paid runs, repointing agents and
editing providers are as dangerous as approving.

Request hygiene (H2): the Host header must name this machine (stops DNS
rebinding from reading GET routes), a present Origin must match the Host, and
POST bodies must be application/json — a text/plain no-cors form post from
another website is exactly what CORS does not preflight.
"""
import hmac
import json
from html import escape as html_escape
import re
import select
import socket
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

from . import approvals, scheduler, sessions
# moved out of this file (REVIEW-2026-11-11 §3.2); re-exported because tests
# and older call sites still import them from here
from .accounts import (_approval_account, account_rank,  # noqa: F401
                       ordered_accounts, sim_label, venue_adapter)
from .dash.usage import (BOB_USAGE_FILE, USAGE_AGENT_LABELS,  # noqa: F401
                         USAGE_SOURCE_LABELS, bob_usage, usage_report,
                         usage_source)
from .levels import LEVEL_KEYS, _levels, _range, quote_units  # noqa: F401
from . import exchanges as desk_exchanges
from .tools.files import JailError
from .providers import health as provider_health
from .providers import registry
from .tools import tradingview
from .tools.brokers import seed_mcps
import logging
from .logsetup import swallowed

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
SAXO_CALLBACK = "/claire-saxo"   # the path registered on both Saxo apps
PAGES = {"/": "index.html", "/portfolio": "portfolio.html",
         "/markets": "markets.html", "/trading": "trading.html",
         "/agents": "agents.html", "/providers": "providers.html",
         "/claire": "claire.html", "/schedule": "agents.html",  # merged
         "/approvals": "approvals.html", "/saxo": "saxo.html",
         "/usage": "usage.html", "/watchlist": "watchlist.html",
         "/orders": "orders.html", "/trades": "trades.html",
         "/maintenance": "maintenance.html"}
STATIC = {"/style.css": ("style.css", "text/css"),
          "/nav.js": ("nav.js", "text/javascript"),
          "/charts.js": ("charts.js", "text/javascript"),
          "/sell.js": ("sell.js", "text/javascript"),
          "/orderinfo.js": ("orderinfo.js", "text/javascript"),
          "/live.js": ("live.js", "text/javascript"),
          "/orderform.js": ("orderform.js", "text/javascript"),
          "/orderform.css": ("orderform.css", "text/css")}

PIPELINE_EDGES = [("prepare", "fundamental"), ("prepare", "technical"),
                  ("prepare", "news"), ("fundamental", "bull"),
                  ("technical", "bull"), ("news", "bull"), ("bull", "bear"),
                  ("bear", "arbiter"), ("arbiter", "gate"),
                  ("gate", "execute"), ("execute", "record")]

# The only env var NAMES a provider row may point at. build_llm sends
# env[api_key_ref] as a bearer token to the row's base_url, so a free-form
# name let anyone who could write a provider exfiltrate CLAIRE_DASHBOARD_KEY,
# CLAIRE_INTERNAL_SECRET or a broker secret to a URL of their choosing (C1).
# claude_sdk needs no key at all; None/"" means "no key".
from .providers.registry import PROVIDER_KEY_REFS  # noqa: E402 — one list
PROVIDER_KINDS = ("anthropic", "claude_sdk", "openai_compatible", "custom")

MAX_BODY = 256 * 1024          # largest real body is a chat message; 256 KiB
SSE_MAX_AGE = 15 * 60          # EventSource reconnects by itself, so a stream
SSE_MAX_CLIENTS = 32           # costs nothing to recycle — a thread per tab
                               # held forever does
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_ID_RE = re.compile(r"[a-z0-9_-]{2,30}")
_MODEL_RE = re.compile(r"[A-Za-z0-9._:/@\[\]-]{1,120}")
_CCY_RE = re.compile(r"[A-Z]{3}")


class HTTPError(Exception):
    """Raised by request plumbing; do_GET/do_POST turn it into a response."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def background_loops(env) -> list:
    """Recurring work that is not in the schedules table (fixed loops) —
    shown on the system map so nothing that runs on its own is invisible."""
    out = []
    try:
        from .tools import saxo_live
        st = {w: saxo_live.status(w) for w in ("sim", "live")}
        signed = [w for w, v in st.items() if v.get("signed_in")]
        out.append({"id": "loop:saxo", "name": "Saxo sign-in keepalive",
                    "sub": ("keeps " + " + ".join(signed) + " signed in")
                    if signed else "nothing signed in",
                    "state": "ok" if signed else "idle",
                    "detail": "refreshes the Saxo sign-in every 10 min"
                              " and warns you (Telegram + desktop) before or"
                              " when it lapses · runs in claire-api"})
    except Exception as e:                       # noqa: BLE001 — display only
        swallowed(log, "background-loops panel: Saxo keepalive block failed",
                  e, every=600)
        pass
    try:
        from . import alerts
        tg = alerts.telegram_settings(alerts.TELEGRAM_ENV)
        ready = bool(tg.get("token") and tg.get("approver_id"))
    except Exception:                            # noqa: BLE001
        ready = False
    out.append({"id": "loop:telegram", "name": "Telegram approvals",
                "sub": "every 20 s" if ready else "not configured",
                "state": "ok" if ready else "idle",
                "detail": "sends each approval card (with chart) to your "
                          "Telegram and closes it when it is decided · runs "
                          "in the dashboards; Bob's bot relays your taps"})
    return out


def session_facts(exchange, cal, now):
    """Is the order's market open, and when does it next open (epoch s) —
    what the Orders page needs to say what has to happen for it to fill."""
    nowdt = datetime.fromtimestamp(now, tz=timezone.utc)
    try:
        return {"is_open": sessions.is_open(exchange, nowdt, cal),
                "next_open": int(sessions.next_open(exchange, nowdt,
                                                    cal).timestamp())}
    except KeyError:
        return {"is_open": None, "next_open": None}


def default_allowed_hosts(bind):
    """Host names this server answers to. Loopback always; when bound beyond
    loopback, also this machine's hostname and LAN addresses — the names a
    browser on the network would actually type. Anything else (an attacker's
    domain rebound to our IP) is refused, GETs included."""
    hosts = set(LOOPBACK_HOSTS)
    if bind in LOOPBACK_HOSTS:
        return hosts
    if bind not in ("0.0.0.0", "::", ""):
        hosts.add(bind.lower())
    try:
        name = socket.gethostname().lower()
        hosts |= {name, f"{name}.local", socket.getfqdn().lower()}
        hosts |= set(socket.gethostbyname_ex(name)[2])
    except OSError:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))              # no packet sent; reads the route
        hosts.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return hosts


def host_only(value):
    """'127.0.0.1:7787' → '127.0.0.1', '[::1]:7787' → '::1'."""
    h = (value or "").strip().lower()
    if h.startswith("["):
        end = h.find("]")
        return h[1:end] if end > 0 else ""
    if h.count(":") == 1:
        h = h.split(":", 1)[0]
    return h.rstrip(".")


def _validate_schedule_patch(current: dict, patch) -> dict:
    """Only the knobs the UI exposes, with sane bounds. scheduler.validate_spec
    checks the shape of a schedule; this checks that a patch cannot smuggle in
    a new type, unknown keys, or e.g. max_runs=10_000 (one tick of event_scan
    would then start that many paid pipelines)."""
    if patch is None:
        return {}
    if not isinstance(patch, dict):
        raise ValueError("spec must be an object")

    def is_int(v):
        return isinstance(v, int) and not isinstance(v, bool)

    checks = {
        "minutes": lambda v: is_int(v) and 1 <= v <= 1440,
        "time": lambda v: isinstance(v, str) and
        re.fullmatch(r"\d{1,2}:\d{2}", v) is not None,
        "tz": lambda v: isinstance(v, str) and
        re.fullmatch(r"[A-Za-z0-9_+/-]{1,64}", v) is not None,
        "days": lambda v: isinstance(v, list) and 1 <= len(v) <= 7 and
        len(set(v)) == len(v) and all(is_int(d) and 1 <= d <= 7 for d in v),
        "require_open": lambda v: isinstance(v, bool),
        "threshold": lambda v: isinstance(v, (int, float)) and
        not isinstance(v, bool) and 1.0 <= v <= 50.0,
        "max_runs": lambda v: is_int(v) and 0 <= v <= 5,
    }
    out = {}
    for k, v in patch.items():
        if v is None:
            continue
        if k not in checks or k not in current:
            raise ValueError(f"spec key {k!r} is not editable for this job")
        if not checks[k](v):
            raise ValueError(f"spec {k}={v!r} is out of range")
        out[k] = v
    return out


def create_server(conn, repo, market, *, api_base="http://127.0.0.1:7788",
                  secret="", dash_key="", narratives=None, clock=time.time,
                  port=7787, env=None, cal=None, sse_interval=2.0,
                  bind="127.0.0.1", allowed_hosts=None,
                  sse_max_age=SSE_MAX_AGE):
    """`bind` is the dashboards' listen address. 0.0.0.0 puts the desk on the
    LAN so another machine can use it; the approval key still gates every
    action, but READ routes are open to anyone who can reach the port — see
    CLAIRE_DASHBOARD_BIND in etc/claire.env. claire-api stays on loopback
    regardless: it holds /internal/resume, and nothing off-box may call it.

    `allowed_hosts`: Host-header names to answer to; None derives them from
    `bind` (loopback, plus hostname/LAN IPs when bound wider). A derived set
    is re-read at most once a minute on a miss, so a DHCP address change does
    not lock the owner out until a restart."""
    resume_post = approvals.default_resume_post(api_base, secret)
    env = env or {}
    hosts = {"set": set(allowed_hosts) if allowed_hosts is not None
             else default_allowed_hosts(bind), "t": time.monotonic(),
             "fixed": allowed_hosts is not None}
    hosts_lock = threading.Lock()

    def host_allowed(name):
        if name in hosts["set"]:
            return True
        with hosts_lock:
            if hosts["fixed"] or time.monotonic() - hosts["t"] < 60:
                return False
            hosts["set"], hosts["t"] = default_allowed_hosts(bind), \
                time.monotonic()
            return name in hosts["set"]
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
            except Exception as e:               # noqa: BLE001
                swallowed(log, "live-update poller failed",
                          e, trace=True, every=600)
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
        # a socket timeout so a client that announces a body and never sends
        # it (or stalls mid-headers) cannot pin a thread indefinitely
        timeout = 60

        def log_message(self, *a):
            pass

        # ── plumbing ─────────────────────────────────────────────────────
        def _failed(self, e):
            """How every verb ends when its handler raises. Was pasted three
            times (GET, POST, DELETE): a client that went away is not news;
            an HTTPError is the answer the handler chose; anything else is a
            500 with its traceback in the log."""
            if isinstance(e, (BrokenPipeError, ConnectionResetError)):
                return
            if isinstance(e, HTTPError):
                code, body = e.code, {"error": e.message}
            else:
                log.exception("HTTP 500 on %s %s", self.command,
                              self.path.split("?", 1)[0])
                code, body = 500, {"error": str(e)}
            try:
                self._send(code, body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(
                body, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _check_request(self):
            """Host allowlist + Origin match, for every method. Without the
            Host check a DNS-rebinding page (evil.example → our IP) is
            'same-origin' to the browser and can read every GET route."""
            host_hdr = self.headers.get("Host")
            if not host_hdr or not host_allowed(host_only(host_hdr)):
                raise HTTPError(421, "unrecognised Host header")
            origin = self.headers.get("Origin")
            if origin is not None:
                o = urlparse(origin)
                if origin == "null" or o.scheme not in ("http", "https") or \
                        o.netloc.lower() != host_hdr.strip().lower():
                    raise HTTPError(403, "cross-origin request refused")

        def _json_body(self):
            """application/json only, bounded, and an object. The old version
            parsed any Content-Type (CSRF via text/plain) and trusted
            Content-Length (a negative one made rfile.read block forever)."""
            ctype = (self.headers.get("Content-Type") or "").split(";")[0]
            if ctype.strip().lower() != "application/json":
                raise HTTPError(415, "Content-Type must be application/json")
            if self.headers.get("Transfer-Encoding"):
                raise HTTPError(411, "Content-Length required")
            raw = (self.headers.get("Content-Length") or "0").strip()
            if not re.fullmatch(r"[0-9]{1,12}", raw):
                raise HTTPError(400, "invalid Content-Length")
            n = int(raw)
            if n > MAX_BODY:
                raise HTTPError(413, "request body too large")
            data = self.rfile.read(n) if n else b""
            if len(data) != n:
                raise HTTPError(400, "truncated request body")
            try:
                body = json.loads(data or b"{}")
            except ValueError:
                raise HTTPError(400, "body is not valid JSON") from None
            if not isinstance(body, dict):
                raise HTTPError(400, "body must be a JSON object")
            return body

        def _paired(self):
            # constant-time: a == compare leaks how many leading characters
            # matched, which over a LAN is enough to walk the key out
            given = self.headers.get("X-Dash-Key") or ""
            return bool(dash_key) and hmac.compare_digest(
                given.encode("utf-8", "replace"), dash_key.encode())

        # ── GET ──────────────────────────────────────────────────────────
        def do_GET(self):                        # noqa: N802
            try:
                self._check_request()
            except HTTPError as e:
                return self._send(e.code, {"error": e.message})
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
                    "/api/live-status": self._live_status,
                    "/api/nav-state": self._nav_state,
                    "/api/maintenance": self._maintenance,
                    "/api/exchanges": self._exchanges,
                }.get(u.path)
                if route:
                    return self._send(200, route())
                if u.path == "/api/usage":
                    return self._send(200, self._usage(q.get("days", "7")))
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
                if u.path == "/api/watchlist-data":
                    # the Watchlist page in one call (app/watchlist.py)
                    from . import watchlist
                    return self._send(200, watchlist.view(
                        conn, repo, market, env=env or {}, clock=clock,
                        cal=cal_or_default()))
                if u.path == "/api/price-triggers":
                    from . import price_triggers
                    return self._send(200, price_triggers.list_triggers(
                        conn, status=q.get("status"),
                        ticker=q.get("ticker")))
                if u.path == "/api/events":
                    return self._sse()
                if u.path == "/api/fx":
                    return self._send(200, {"rate": str(market.fx(
                        q.get("from", "USD"), q.get("to", "USD")))})
                if u.path == SAXO_CALLBACK:
                    return self._saxo_login_callback(q)
                if u.path == "/api/saxo-account":
                    # real-account balances and positions: key required even
                    # though this is a GET — the dashboard is on the LAN
                    if not self._paired():
                        return self._send(403, {"error":
                                                "dashboard not paired"})
                    which = q.get("env", "live")
                    if which not in ("sim", "live"):
                        return self._send(400, {"error":
                                                "env must be sim or live"})
                    return self._send(200, self._saxo_account(which))
                if u.path == "/api/orders":
                    return self._send(200, self._orders())
                if u.path == "/api/trades":
                    return self._send(200, self._trades(
                        int(q.get("days", 90) or 90)))
                if u.path == "/api/approvals-archive":
                    return self._send(200, self._approvals_archive(
                        int(q.get("days", 30) or 30)))
                if u.path == "/api/range2y":
                    # the 2-year high / low for a symbol (weekly bars, cached
                    # by the market layer) — the Watchlist order summary
                    sym = q.get("symbol", "")
                    try:
                        rng = _range(market.chart(sym, "2y", "1wk"))
                    except Exception as e:      # noqa: BLE001 — optional
                        return self._send(200, {"range2y": None,
                                                "error": str(e)[:200]})
                    try:                        # bars may be in pence
                        px = market.quote(sym).get("price")
                    except Exception:           # noqa: BLE001
                        px = None
                    return self._send(200, {"range2y": quote_units(
                        rng, px, ("high", "low"))})
                if u.path == "/api/arbiter-notes":
                    # the arbiter's notes in plain English for an approval
                    # card. Reading a stored explanation is free; writing
                    # one is an LLM call, so it needs the dashboard key.
                    from . import arbiter_notes
                    wi = q.get("id", "")
                    row = conn.execute("SELECT thesis_json FROM work_items"
                                       " WHERE id=?", (wi,)).fetchone()
                    if row is None:
                        return self._send(404, {"error": "unknown work item"})
                    notes = arbiter_notes.notes_of(row["thesis_json"])
                    hit = arbiter_notes.cached(conn, wi, notes) if notes \
                        else {"summary": None, "notes": []}
                    if hit is not None:
                        return self._send(200, hit)
                    if not self._paired():
                        raise HTTPError(403, "dashboard not paired")
                    try:
                        return self._send(200, arbiter_notes.explain(conn, wi))
                    except Exception as e:      # noqa: BLE001 — LLM/parse
                        return self._send(502, {"error": "could not write the"
                                                " plain-English notes: "
                                                f"{str(e)[:200]}"})
                if u.path == "/api/levels-for-override":
                    return self._send(200, self._override_context(
                        q.get("id", "")))
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
                    # no `since`: the LATEST 300, oldest first. It used to
                    # be the first 300 ever written, so once the desk had
                    # more the Trading page showed August for good and
                    # re-downloaded it every 8 s (B7). With `since`: only
                    # what is newer, which is all a poll needs.
                    try:
                        since = max(0, int(q.get("since") or 0))
                    except ValueError:
                        raise HTTPError(400, "since must be an event id")
                    if since:
                        rows = [dict(r) for r in conn.execute(
                            "SELECT * FROM events WHERE id > ? ORDER BY id"
                            " LIMIT 300", (since,))]
                    else:
                        rows = [dict(r) for r in conn.execute(
                            "SELECT * FROM (SELECT * FROM events ORDER BY"
                            " id DESC LIMIT 300) ORDER BY id")]
                    return self._send(200, {
                        "events": rows,
                        "last": rows[-1]["id"] if rows else since})
                self._send(404, {"error": "not found"})
            except Exception as e:               # noqa: BLE001
                self._failed(e)

        # ── POST ─────────────────────────────────────────────────────────
        def do_POST(self):                       # noqa: N802
            try:
                self._check_request()
                # Every POST changes state or spends money (runs, chat, agent
                # and provider edits, schedules, MCP, watchlist, approvals),
                # so all of them need the paired key — checked once, here,
                # BEFORE the body is read, rather than route by route where
                # one forgotten check was the whole of C1/H1. No key
                # configured means nobody is paired: fail closed.
                if not self._paired():
                    raise HTTPError(403, "dashboard not paired — enter the "
                                    "dashboard key first")
                u = urlparse(self.path)
                body = self._json_body()
                if u.path == "/api/ask":
                    return self._ask_proxy(body, "/ask")
                if u.path == "/api/agent-ask":
                    return self._ask_proxy(body, "/agent/ask")
                if u.path == "/api/flag-ack":
                    # clear (or bring back) a Mission Control warning
                    keys = body.get("keys") or [body.get("key")]
                    if not all(isinstance(k, str) and 0 < len(k) <= 120
                               for k in keys) or len(keys) > 50:
                        raise HTTPError(400, "keys must be short strings")
                    for k in keys:
                        if body.get("restore"):
                            conn.execute("DELETE FROM dashboard_acks"
                                         " WHERE key=?", (k,))
                        else:
                            conn.execute(
                                "INSERT INTO dashboard_acks (key, acked_at)"
                                " VALUES (?,?) ON CONFLICT(key) DO UPDATE"
                                " SET acked_at=excluded.acked_at",
                                (k, int(clock())))
                    return self._send(200, {"ok": True})
                if u.path == "/api/order-control":
                    # cancel / edit an APPROVED order before it fills; the
                    # adapters live in claire-api, which does the work
                    if not self._paired():
                        raise HTTPError(403, "dashboard not paired")
                    req = {"work_item_id": str(body.get("work_item_id") or ""),
                           "actor": "human",
                           "action": body.get("action")}
                    for k in ("entry", "stop", "target", "qty", "trail_pct"):
                        if body.get(k) not in (None, ""):
                            req[k] = float(body[k])
                    try:
                        r = httpx.post(f"{api_base}/internal/order", json=req,
                                       headers={"x-claire-secret": secret},
                                       timeout=60)
                    except Exception as e:          # noqa: BLE001
                        return self._send(502, {"error":
                                                f"claire-api unreachable ({e})"})
                    try:
                        data = r.json()
                    except ValueError:
                        data = {}
                    if r.status_code != 200:
                        return self._send(r.status_code, {"error": str(
                            data.get("detail") or f"HTTP {r.status_code}")})
                    return self._send(200, data)
                if u.path in ("/api/trade-retry", "/api/trade-cancel"):
                    # money path: re-checked locally, like thesis-action
                    if not self._paired():
                        raise HTTPError(403, "dashboard not paired")
                    wi = str(body.get("work_item_id") or "")
                    try:
                        if u.path == "/api/trade-retry":
                            if body.get("entry"):
                                # the human edited the levels: amend the
                                # trade in claire-api (the graph owns it)
                                self._amend(wi, body)
                            out = approvals.retry_failed(
                                conn, wi, resume_post,
                                confirm_live=body.get("confirm_live"),
                                changes={k: body.get(k) for k in (
                                    "size_base", "qty", "trail_pct", "order_type",
                                    "broker")})
                        else:
                            out = approvals.cancel_failed(conn, wi,
                                                          clock=clock)
                        return self._send(200, out)
                    except approvals.AuthError as e:
                        return self._send(e.code, {"error": e.message})
                if u.path == "/api/trade-check":
                    return self._send(200, self._trade_check(body))
                if u.path == "/api/saxo-login-start":
                    return self._saxo_login_start(body)
                if u.path == "/api/maintenance-run":
                    # starts a run in claire-api (it owns the maintainer and
                    # the AI agent); the page polls /api/maintenance
                    if not self._paired():
                        raise HTTPError(403, "dashboard not paired")
                    try:
                        r = httpx.post(f"{api_base}/internal/maintenance",
                                       headers={"x-claire-secret": secret},
                                       timeout=10)
                    except Exception as e:          # noqa: BLE001
                        return self._send(502, {"error":
                                                f"claire-api unreachable ({e})"})
                    try:
                        data = r.json()
                    except ValueError:
                        data = {}
                    if r.status_code != 200:
                        return self._send(r.status_code, {"error": str(
                            data.get("detail") or f"HTTP {r.status_code}")})
                    return self._send(200, data)
                if u.path == "/api/live-arm":
                    # ARMING from the dashboard (§22.1): paired key + the
                    # typed phrase + a browser on THIS PC (loopback). A LAN
                    # device can disarm but never arm. Locks 1 (env flag) and
                    # 3 (per-trade LIVE approval) are untouched by this.
                    if not self._paired():
                        raise HTTPError(403, "dashboard not paired")
                    if not self._from_this_pc():
                        raise HTTPError(403, "live trading can only be armed "
                                        "from a browser on the desk's own PC")
                    from . import live_trading
                    try:
                        live_trading.arm(
                            conn, env or {}, actor="dashboard:this-pc",
                            phrase=str(body.get("phrase") or "").strip(),
                            clock=clock)
                    except live_trading.LiveTradingBlocked as e:
                        return self._send(409, {"error": str(e)})
                    return self._send(200, {"ok": True,
                                            **self._live_status()})
                if u.path == "/api/live-limits":
                    if not self._paired():
                        raise HTTPError(403, "dashboard not paired")
                    from . import live_trading
                    new = body.get("limits")
                    if not isinstance(new, dict) or not new:
                        return self._send(400, {"error": "limits: {name: "
                                                "value, ...} required"})
                    try:
                        live_trading.set_limits(
                            conn, new, actor="dashboard ("
                            f"{self.client_address[0]})", clock=clock)
                    except ValueError as e:
                        return self._send(400, {"error": str(e)})
                    return self._send(200, {"ok": True,
                                            **self._live_status()})
                if u.path == "/api/live-disarm":
                    # DISARM is always one click away, from any paired
                    # device (§22). Arming: /api/live-arm (this PC only).
                    if not self._paired():
                        raise HTTPError(403, "dashboard not paired")
                    from . import live_trading
                    changed = live_trading.disarm(
                        conn, actor="dashboard",
                        reason=str(body.get("reason") or "disarmed from the "
                                   "dashboard")[:200], clock=clock)
                    return self._send(200, {"ok": True, "changed": changed,
                                            **self._live_status()})
                if u.path == "/api/thesis-action":
                    # money path: re-checked locally on purpose, so moving or
                    # refactoring the global gate above can never open it
                    if not self._paired():
                        raise HTTPError(403, "dashboard not paired")
                    try:
                        out = approvals.thesis_action(conn, body, resume_post,
                                                      clock=clock)
                        return self._send(200, out)
                    except approvals.AuthError as e:
                        return self._send(e.code, {"error": e.message})
                if u.path == "/api/override":
                    # overturning the desk is a money-path action: same local
                    # re-check as thesis-action, on top of the global gate
                    if not self._paired():
                        raise HTTPError(403, "dashboard not paired")
                    try:
                        r = httpx.post(f"{api_base}/internal/override",
                                       json={**body, "actor": "human"},
                                       headers={"x-claire-secret": secret},
                                       timeout=30)
                    except Exception as e:      # noqa: BLE001
                        return self._send(502, {"error":
                                                f"claire-api unreachable ({e})"})
                    out = r.json() if r.headers.get("content-type", "") \
                        .startswith("application/json") else {}
                    if r.status_code != 200:
                        return self._send(r.status_code,
                                          {"error": out.get("detail",
                                                            "override failed")})
                    return self._send(200, out)
                if u.path == "/api/telegram-decision":
                    # money path (Telegram buttons, relayed by Bob's bot):
                    # key re-checked locally like the approval route; the
                    # approver's Telegram user id is checked again inside
                    if not self._paired():
                        raise HTTPError(403, "dashboard not paired")
                    from . import telegram_approvals
                    code, out = telegram_approvals.handle_decision(
                        conn, market, body, resume_post, env=env or {},
                        clock=clock)
                    return self._send(code, out)
                if u.path == "/api/price-triggers":
                    # a trigger can become a trade — and in auto mode a
                    # pre-approved one — so it is a money-path action: the
                    # key is re-checked here, like thesis-action
                    if not self._paired():
                        raise HTTPError(403, "dashboard not paired")
                    from . import price_triggers
                    who = ("human via the paired dashboard ("
                           f"{self.client_address[0]})")
                    try:
                        if body.get("replace") is not None:
                            out = price_triggers.replace(
                                conn, body.get("replace"),
                                {k: v for k, v in body.items()
                                 if k != "replace"},
                                env=env or {}, clock=clock, created_by=who)
                        else:
                            out = price_triggers.create(
                                conn, body, env=env or {}, clock=clock,
                                created_by=who)
                    except price_triggers.TriggerError as e:
                        return self._send(e.code, {"error": e.message})
                    return self._send(200, out)
                if u.path == "/api/position-floor":
                    # change a HELD position's trailing alert % (Portfolio →
                    # Edit). An alert, not an order: breaching it
                    # only starts a sell review that needs approval. The
                    # best price since buying is kept.
                    if not self._paired():
                        raise HTTPError(403, "dashboard not paired")
                    return self._send(200, self._position_floor(body))
                if u.path == "/api/manual-trade":
                    # a trade you start (the Sell button) is a money-path
                    # action: same local re-check as thesis-action. Named so
                    # it does not share the /api/override prefix.
                    if not self._paired():
                        raise HTTPError(403, "dashboard not paired")
                    return self._manual_trade(body)
                if u.path == "/api/run":
                    return self._start_run(body)
                if u.path == "/api/agent":
                    return self._assign_agent(body)
                if u.path == "/api/provider-test":
                    pid = body.get("provider_id")
                    if not isinstance(pid, str) or conn.execute(
                            "SELECT 1 FROM providers WHERE id=?",
                            (pid,)).fetchone() is None:
                        raise HTTPError(400, "unknown provider_id")
                    # a row written before the allowlist existed must not get
                    # its (possibly secret) key sent anywhere by a probe
                    ref = conn.execute("SELECT api_key_ref FROM providers"
                                       " WHERE id=?", (pid,)).fetchone()[0]
                    if ref and ref not in PROVIDER_KEY_REFS:
                        raise HTTPError(400, f"provider {pid} has a key ref "
                                        "outside the allowlist — re-save it")
                    ok = provider_health.probe(conn, pid)
                    latest = provider_health.latest(conn).get(pid)
                    return self._send(200, dict(latest) if latest else
                                      {"ok": ok})
                if u.path == "/api/schedule":
                    try:
                        job = body.get("job")
                        row = conn.execute(
                            "SELECT spec FROM schedules WHERE job=?",
                            (job if isinstance(job, str) else "",)).fetchone()
                        if row is None:
                            raise KeyError(f"unknown job {job!r}")
                        enabled = body.get("enabled")
                        if enabled is not None and not isinstance(enabled,
                                                                  bool):
                            raise ValueError("enabled must be true/false")
                        patch = _validate_schedule_patch(
                            json.loads(row["spec"]), body.get("spec"))
                        scheduler.update(conn, job, enabled=enabled,
                                         spec_patch=patch or None)
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
                    from .tools.market import desk_ticker
                    t = desk_ticker(t, ex)
                    if not t or not ex:
                        return self._send(400, {"error":
                                                "ticker and exchange required"})
                    # watchlisted names feed the screener and so become run
                    # tickers: same strict shape the Desk enforces
                    self._check_symbol(t, ex)
                    note = body.get("note")
                    if note is not None and (not isinstance(note, str) or
                                             len(note) > 200):
                        raise HTTPError(400, "note must be text ≤200 chars")
                    if body.get("action") == "remove":
                        conn.execute("DELETE FROM watchlist WHERE ticker=?"
                                     " AND exchange=?", (t, ex))
                    else:
                        conn.execute(
                            "INSERT OR REPLACE INTO watchlist (ticker,"
                            " exchange, note, added_at) VALUES (?,?,?,?)",
                            (t, ex, note, int(clock())))
                    return self._send(200, {"ok": True})
                self._send(404, {"error": "not found"})
            except Exception as e:               # noqa: BLE001
                self._failed(e)

        # ── DELETE (price triggers only) ─────────────────────────────────
        def do_DELETE(self):                     # noqa: N802
            """Cancel an active price trigger: DELETE /api/price-triggers
            ?id=N. Same gates as every POST — Host/Origin, then the key."""
            try:
                self._check_request()
                if not self._paired():
                    raise HTTPError(403, "dashboard not paired — enter the "
                                    "dashboard key first")
                u = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                if u.path != "/api/price-triggers":
                    raise HTTPError(404, "not found")
                from . import price_triggers
                try:
                    out = price_triggers.cancel(conn, q.get("id"),
                                                actor="human", clock=clock)
                except price_triggers.TriggerError as e:
                    raise HTTPError(e.code, e.message) from None
                return self._send(200, out)
            except Exception as e:               # noqa: BLE001
                self._failed(e)

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

        def _client_gone(self):
            """True once the browser has closed its end. An SSE client never
            sends anything after its request, so a readable socket that
            yields no bytes is EOF — noticed within one wait instead of only
            when a write finally fails (which a half-open TCP can delay)."""
            try:
                r, _, _ = select.select([self.connection], [], [], 0)
                if not r:
                    return False
                return self.connection.recv(1, socket.MSG_PEEK) == b""
            except (OSError, ValueError):
                return True

        def _sse(self):
            q = _queue.Queue(maxsize=500)
            with sse_lock:
                if len(sse_clients) >= SSE_MAX_CLIENTS:
                    return self._send(503, {"error": "too many live streams"})
                sse_clients.append(q)
            # each stream is a thread: bound its life. EventSource reconnects
            # on its own, so recycling it is invisible to the page.
            deadline = time.monotonic() + sse_max_age
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                # a first byte at once: a browser may hold "open" until the
                # body starts, and the pill sat on "connecting…" for a
                # keep-alive interval (20 s). retry: reconnect in 3 s.
                self.wfile.write(b"retry: 3000\n: connected\n\n")
                self.wfile.flush()
                last_write = time.monotonic()
                while time.monotonic() < deadline and not self._client_gone():
                    # short waits so a disconnect is noticed within seconds
                    wait = max(0.05, min(5.0, deadline - time.monotonic()))
                    try:
                        ev = q.get(timeout=wait)
                        self.wfile.write(
                            f"data: {json.dumps(ev, default=str)}\n\n".encode())
                    except _queue.Empty:
                        if time.monotonic() - last_write < 20:
                            continue
                        self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    last_write = time.monotonic()
                self.close_connection = True
            except (BrokenPipeError, ConnectionResetError, OSError):
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
                except (OSError, JailError):    # bad id/name → not found
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
                            # what the exchange IS (app/exchanges.py)
                            "currency": desk_exchanges.currency(ex),
                            "suffix": desk_exchanges.SUFFIX.get(ex, ""),
                            "lot": desk_exchanges.lot_size(ex),
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
            kinds = {r[0] for r in conn.execute(
                "SELECT p.kind FROM agents a JOIN providers p"
                " ON p.id=a.provider_id WHERE a.enabled=1")}
            if "anthropic" in kinds and not env.get("ANTHROPIC_API_KEY"):
                flags.append("ANTHROPIC_API_KEY missing — LLM runs will fail"
                             " (add to etc/claire.env, restart claire-api)")
            if "claude_sdk" in kinds:
                from .providers import claude_sdk
                why = claude_sdk.not_ready()
                if why:
                    flags.append(f"Claude (Max subscription): {why}")
            for pid, h in provider_health.latest(conn).items():
                if h and not h["ok"]:
                    flags.append(f"provider {pid} failing its health probe")
            for svc, h in healthrows.items():
                if h["ok"] != "ok":
                    flags.append(f"{svc} is {h['ok']}")
            now = int(clock())
            day_ago = now - 86400
            acks = {r["key"]: r["acked_at"] for r in conn.execute(
                "SELECT key, acked_at FROM dashboard_acks")}
            # a warning's key is its text, so a CHANGED warning is new; a
            # cleared one returns after a day if it is still true
            items = [{"key": "flag:" + f[:110], "text": f} for f in flags]
            visible = [i for i in items
                       if acks.get(i["key"], 0) < day_ago]
            cleared = [dict(i, acked_at=acks[i["key"]]) for i in items
                       if i not in visible]
            # failed runs: clearing acknowledges the ones so far; only a
            # failure AFTER that brings the warning back
            seen = max(day_ago, acks.get("failed_runs", 0))
            (nfail,) = conn.execute(
                "SELECT COUNT(*) FROM work_items WHERE state='failed'"
                " AND updated_at > ?", (seen,)).fetchone()
            if nfail:
                visible.append({"key": "failed_runs", "text":
                                f"{nfail} failed run(s) in the last 24h"
                                + (" since you last cleared this"
                                   if seen > day_ago else "")
                                + " — see Trading", "link": "/trading"})
            return {"work_items": states, "health": healthrows, "cash": cash,
                    "claire_api": up,
                    "flags": [i["text"] for i in visible],
                    "flag_items": visible, "cleared": cleared,
                    "pending": states.get("awaiting_approval", 0)}

        def _reports_for(self, wi):
            """Every stored agent report for a run, in pipeline order."""
            out = []
            for rep in conn.execute(
                    "SELECT agent_id, kind, payload FROM agent_reports"
                    " WHERE work_item_id=? ORDER BY id", (wi,)):
                try:
                    out.append({"agent": rep["agent_id"], "kind": rep["kind"],
                                "data": json.loads(rep["payload"])})
                except ValueError:
                    pass
            return out

        def _approvals(self):
            paired = self._paired() or not dash_key
            cal = cal_or_default()
            nowdt = datetime.fromtimestamp(int(clock()), tz=timezone.utc)
            from . import live_trading
            live = live_trading.status(conn, env or {})
            accounts = [_approval_account(a, repo, env or {}, live)
                        for a in ordered_accounts(conn, env)]
            if not any(a["environment"] == "live" for a in accounts):
                # always SHOW the real account, so it is obvious where it
                # is and why it cannot be chosen yet (§22)
                lid = (env or {}).get("SAXO_LIVE_ACCOUNT_ID") or ""
                accounts.append({
                    "id": "saxo-live", "broker": "saxo_live", "ccy": "",
                    "environment": "live", "cash": 0, "fee_model": {},
                    "label": f"Saxo LIVE {lid} — real money".strip(),
                    "disabled": "not set up yet (bin/claire live setup)"})
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
                # an override ran no analysts of its own — its evidence is the
                # verdict it overturned, which is exactly what must be read
                # before approving one
                # an override inherits the verdict it overturned; a sell
                # review inherits the analysis that opened the position. Both
                # must be readable before approving.
                reports = self._reports_for(r["id"])
                inherited = r["override_of"] or r["prior_run"]
                if inherited:
                    reports = self._reports_for(inherited) + reports
                costs = conn.execute(
                    "SELECT COALESCE(SUM(cost_usd),0) c, COUNT(*) n FROM"
                    " agent_runs WHERE work_item_id=?", (r["id"],)).fetchone()
                cards.append({
                    "id": r["id"], "kind": r["kind"], "ticker": r["ticker"],
                    "exchange": exchange, "trigger": r["trigger"],
                    "reports": reports, "override_of": r["override_of"],
                    "prior_run": r["prior_run"],
                    "llm_cost": costs["c"], "llm_calls": costs["n"],
                    "created_at": r["created_at"],
                    "thesis": json.loads(r["thesis_json"] or "{}"),
                    # tokens ONLY for a paired browser (§10 rev2)
                    "token": r["approval_token"] if paired else None,
                    "locked": not paired,
                    "session": session,
                    "expires_at": r["expires_at"],
                    # a manual trade's prefill (qty / size / account)
                    "suggested": json.loads(r["suggested_json"] or "null"),
                    # what the desk holds per account: a sell card prefills
                    # its share count from it, and it says exit vs short
                    "held": {h["account_id"]: h["q"] for h in conn.execute(
                        "SELECT l.account_id, SUM(CASE WHEN l.qty_opened<0"
                        " THEN -l.qty_remaining ELSE l.qty_remaining END)"
                        "/1e6 AS q FROM lots l JOIN instruments i ON"
                        " i.id=l.instrument_id WHERE i.ticker=? AND"
                        " l.qty_remaining>0 GROUP BY l.account_id",
                        (r["ticker"],))}})
            # a PASS never reaches the gate, so an empty Approvals page used
            # to be the only sign a run had concluded at all
            decided = []
            for r in conn.execute(
                    "SELECT id, ticker, state, thesis_json, updated_at,"
                    " trigger, override_of FROM work_items"
                    " WHERE thesis_json IS NOT NULL"
                    " AND state IN ('done','rejected','expired')"
                    " AND updated_at > ? ORDER BY updated_at DESC LIMIT 12",
                    (int(clock()) - 7 * 86400,)):
                try:
                    t = json.loads(r["thesis_json"])
                except ValueError:
                    continue
                # already overturned? then the override owns the decision and
                # offering a second one would double-buy the same thesis
                ovr = conn.execute(
                    "SELECT id, state FROM work_items WHERE override_of=?"
                    " AND state NOT IN ('rejected','expired','failed',"
                    "'cancelled') ORDER BY created_at DESC LIMIT 1", (r["id"],)).fetchone()
                inst = conn.execute(
                    "SELECT exchange FROM instruments WHERE ticker=? LIMIT 1",
                    (r["ticker"],)).fetchone()
                decided.append({
                    "id": r["id"], "ticker": r["ticker"], "state": r["state"],
                    "exchange": inst["exchange"] if inst else None,
                    "direction": t.get("direction"),
                    "conviction": t.get("conviction"),
                    "currency": t.get("currency"),
                    "conditions": t.get("conditions") or [],
                    # the same evidence the approval cards show, so a PASS can
                    # be read on its merits rather than taken on trust
                    "reports": self._reports_for(r["id"]),
                    "overridable": (t.get("direction") == "pass"
                                    and not r["override_of"] and not ovr),
                    "overridden_by": ovr["id"] if ovr else None,
                    "trigger": r["trigger"], "at": r["updated_at"]})
            failed = approvals.failed_approvals(conn, clock=clock)
            return {"cards": cards, "accounts": accounts, "decided": decided,
                    "failed": failed,
                    "live": {"armed": live["armed"],
                             "summary": live["summary"],
                             "limits": live["limits"],
                             "today": live["today"]}}

        def _portfolio(self):
            from . import portfolio
            data = portfolio.build(conn, market, clock=clock, cal=cal)
            data["alerts"] = [dict(r) for r in conn.execute(
                "SELECT * FROM price_alerts ORDER BY instrument_id")]
            for a in data["accounts"]:
                a["realized_pl"] = repo.realized_pl(a["id"]) / 1e6
            return data

        def _usage(self, days):
            """GET /api/usage?days=N — token usage per agent / source /
            model / day (read-only; see usage_report)."""
            try:
                n = int(days)
            except (TypeError, ValueError):
                n = 7
            return usage_report(conn, now=clock(), days=n)

        def _runs(self):
            rows = []
            for r in conn.execute(
                    "SELECT * FROM work_items WHERE archived_at IS NULL"
                    " ORDER BY created_at DESC LIMIT 50"):
                d = dict(r)
                d.pop("approval_token", None)    # tokens live on cards only
                try:                             # plain-language facts for
                    d["story"] = self._run_story(d)   # the Trading page;
                except Exception as e:           # noqa: BLE001 — never
                    d["story"] = {"error": str(e)[:200]}  # break the list
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
                " tokens_out, started_at, ended_at, error FROM agent_runs"
                " WHERE work_item_id=? ORDER BY started_at", (wi,))]
            d["orders"] = [dict(o) for o in conn.execute(
                "SELECT id, side, qty/1e6 qty, status, broker_order_id,"
                " expires_at, leg, parent_order_id FROM orders"
                " WHERE work_item_id=?", (wi,))]
            try:
                d["narratives"] = narratives.list(wi) if narratives else []
            except JailError:                   # malformed id → no files
                d["narratives"] = []
            if narratives and "summary.md" in d["narratives"]:
                d["summary"] = narratives.read(wi, "summary")
            # plain-language layer for the Trading page (additive: every raw
            # field above stays for the "details" disclosure)
            d["story"] = self._run_story(d)
            d["steps"] = self._run_steps(wi, d["narratives"], d["agent_runs"])
            d["timeline"] = self._run_timeline(d["events"], d["story"])
            return d

        # ── Trading page: runs as human-readable stories (read-only) ─────
        _STEP_AGENTS = (("fundamental", "Research: fundamentals"),
                        ("technical", "Research: technicals"),
                        ("news", "Research: news"),
                        ("bull", "Bull case"), ("bear", "Bear case"),
                        ("arbiter", "Verdict"))

        @staticmethod
        def _friendly_error(msg):
            """A broker / provider error as one sentence a non-developer can
            act on; the raw text is kept alongside by every caller."""
            raw = str(msg or "")
            m = raw.lower()
            if "credit balance is too low" in m:
                return "the AI provider account ran out of credit"
            if "not a whole multiple" in m and "lot size" in m:
                return ("the broker only accepts whole shares, and the "
                        "order asked for a fraction of one")
            if "toofarfromentryorder" in m:
                return ("the broker refused the stop/target: too far from "
                        "the entry price")
            if "insufficient" in m and "cash" in m:
                return "not enough cash in the account"
            if "rate limit" in m or "429" in m:
                return "the AI provider was rate-limiting requests"
            if "levels amended" in m:
                return "you changed the levels; needs a retry to send"
            inner = re.search(r"'message': '([^']+)'", raw)
            text = inner.group(1) if inner else raw
            text = re.sub(r"^(broker rejected order: )", "broker refused: ",
                          text)
            return text[:220]

        def _orders(self):
            """Everything still outstanding: entry orders (queued by the desk
            or working at the broker), the protective stop/target legs that
            guard open positions, and price triggers — for the Orders page."""
            from . import price_triggers
            from .tools.market import yahoo_symbol
            accts = {a["id"]: dict(a) for a in conn.execute(
                "SELECT id, broker, environment FROM broker_accounts")}
            rows = [dict(r) for r in conn.execute(
                "SELECT o.id, o.work_item_id, o.account_id, o.side,"
                " o.qty/1e6 qty, o.limit_price/1e6 limit_price,"
                " o.stop_loss/1e6 stop_loss, o.take_profit/1e6 take_profit,"
                " o.status, o.broker_order_id, o.created_at, o.placed_at,"
                " o.expires_at, o.leg, o.parent_order_id, o.submitted_at,"
                " o.instrument_id, i.ticker, i.exchange, i.currency,"
                " o.trail_pct order_trail_pct,"
                " (SELECT COALESCE(SUM(e.qty),0)/1e6 FROM executions e"
                "  WHERE e.order_id=o.id) filled"
                " FROM orders o JOIN instruments i ON i.id=o.instrument_id"
                " WHERE o.status IN ('pending_session','placed',"
                " 'partially_filled') ORDER BY o.created_at DESC")]
            for r in rows:
                a = accts.get(r["account_id"]) or {}
                r["broker"] = a.get("broker")
                # a buy's trailing alert (a watcher rule on the position, not
                # part of the order): shown and editable on the Orders page
                ta = conn.execute(
                    "SELECT threshold FROM price_alerts WHERE rule='trail_pct'"
                    " AND instrument_id=? AND account_id=? AND side=1",
                    (r["instrument_id"], r["account_id"])).fetchone() \
                    if r["side"] == "buy" and not r["leg"] else None
                r["trail_pct"] = float(ta[0]) if ta else None
                r["account"] = self._account_label(a.get("broker"),
                                                   a.get("environment"))
                r["symbol"] = yahoo_symbol(r["ticker"], r["exchange"])
                # is the stop/target held AT SAXO? Otherwise (the built-in
                # sims, and the markets saxo-paper simulates locally — SGX:
                # Saxo SIM lists those only as CFDs) Claire's sim holds it
                # and the custodian fills it against the live price
                from .tools.saxo_broker import SIM_LOCAL_SUFFIXES
                suf = r["symbol"].rsplit(".", 1)[1] if "." in r["symbol"] \
                    else ""
                r["legs_at_broker"] = bool(
                    (r["broker"] or "").startswith("saxo") and not (
                        r["broker"] == "saxo" and suf in SIM_LOCAL_SUFFIXES))
                r["where"] = ("queued — sent when the market opens"
                              if r["status"] == "pending_session" and
                              not r["submitted_at"] else
                              "being sent to the broker"
                              if r["status"] == "pending_session" else
                              "part filled at the broker"
                              if r["status"] == "partially_filled" else
                              "working at the broker")
                # Claire: where Saxo holds the stop, the trail % is a real
                # trailing stop (the entry's order row carries it, and its
                # stop leg trails by trail % x the entry's limit)
                r["trail_is_stop"] = False
                if r["leg"] == "stop" and r["parent_order_id"] and \
                        r["legs_at_broker"]:
                    from .graph.nodes_execution import trail_distance
                    p = conn.execute("SELECT trail_pct, stop_loss,"
                                     " limit_price FROM orders WHERE id=?",
                                     (r["parent_order_id"],)).fetchone()
                    d = trail_distance(p) if p else None
                    if d:
                        r.update(trail_pct=p["trail_pct"], trails=float(d),
                                 trail_is_stop=True)
                elif not r["leg"] and r["order_trail_pct"] and \
                        r["legs_at_broker"] and r["stop_loss"]:
                    r.update(trail_pct=r["order_trail_pct"],
                             trail_is_stop=True)
            trig = price_triggers.list_triggers(
                conn, since=int(clock()) - 7 * 86400, limit=200)
            syms = sorted({r["symbol"] for r in rows} |
                          {yahoo_symbol(t["ticker"], t["exchange"])
                           for t in trig if t["status"] == "active"})
            quotes = {}
            if syms:
                try:
                    quotes = {s: {"price": float(v["price"]),
                                  "stale": v.get("stale")}
                              for s, v in market.spark(syms).items()
                              if v.get("price") is not None}
                except Exception:               # noqa: BLE001 — best effort
                    quotes = {}
            for t in trig:
                t["symbol"] = yahoo_symbol(t["ticker"], t["exchange"])
                a = accts.get(t["account_id"]) or {}
                t["account"] = self._account_label(a.get("broker"),
                                                   a.get("environment"))
            for r in rows:
                q = quotes.get(r["symbol"]) or {}
                r["last"] = q.get("price")
                r.update(session_facts(r["exchange"], cal_or_default(),
                                       int(clock())))
                if r["leg"] and r["parent_order_id"]:
                    # what the shares cost, so the page can say what the
                    # leg would make or lose if it fills
                    f = conn.execute(
                        "SELECT SUM(1.0*qty*price_native)/SUM(qty)/1e6"
                        " FROM executions WHERE order_id=?",
                        (r["parent_order_id"],)).fetchone()
                    r["entry_price"] = f[0] if f else None
            return {"entries": [r for r in rows if not r["leg"]],
                    "legs": [r for r in rows if r["leg"]],
                    "triggers": trig, "quotes": quotes, "now": int(clock()),
                    "sync": self._broker_sync()}

        def _broker_sync(self):
            """When the custodian last checked each broker account, and last
            reached its venue — the Orders page's sync line."""
            last = conn.execute(
                "SELECT checked_at, ok FROM service_health WHERE"
                " service='custodian' ORDER BY checked_at DESC LIMIT 1"
            ).fetchone()
            sched = conn.execute(
                "SELECT spec, enabled FROM schedules WHERE job='custodian'"
            ).fetchone()
            every = None
            if sched:
                try:
                    every = json.loads(sched["spec"]).get("minutes")
                except (TypeError, ValueError):
                    pass
            accounts = []
            for a in conn.execute(
                    "SELECT a.id, a.broker, a.environment, s.venue,"
                    " s.checked_at, s.contact_at, s.ok, s.orders_checked,"
                    " s.error FROM broker_accounts a LEFT JOIN broker_sync s"
                    " ON s.account_id=a.id ORDER BY a.id"):
                d = dict(a)
                d["label"] = self._account_label(a["broker"],
                                                 a["environment"])
                accounts.append(d)
            return {"custodian_at": last["checked_at"] if last else None,
                    "custodian_ok": last["ok"] if last else None,
                    "every_min": every,
                    "enabled": bool(sched["enabled"]) if sched else None,
                    "accounts": accounts}

        def _approvals_archive(self, days):
            """Approvals you have already dealt with — they leave the
            Approvals page and are kept here: what you decided and what
            happened next."""
            since = int(clock()) - max(1, min(days, 365)) * 86400
            out = []
            for r in conn.execute(
                    "SELECT w.id, w.ticker, w.kind, w.state, w.trigger,"
                    " w.thesis_json, w.updated_at, w.created_at FROM work_items w"
                    " WHERE w.updated_at >= ? AND w.state NOT IN"
                    " ('running','awaiting_approval')"
                    " AND EXISTS (SELECT 1 FROM events e WHERE e.item_id=w.id"
                    "  AND e.to_state='awaiting_approval')"
                    " ORDER BY w.updated_at DESC LIMIT 200", (since,)):
                try:
                    th = json.loads(r["thesis_json"] or "{}")
                except ValueError:
                    th = {}
                dec = conn.execute(
                    "SELECT to_state, ts, payload FROM events WHERE item_id=?"
                    " AND to_state IN ('approved','rejected','expired')"
                    " ORDER BY id LIMIT 1", (r["id"],)).fetchone()
                try:
                    dp = json.loads(dec["payload"] or "{}") if dec else {}
                except ValueError:
                    dp = {}
                fill = conn.execute(
                    "SELECT SUM(e.qty)/1e6 q, SUM(1.0*e.qty*e.price_native)"
                    "/SUM(e.qty)/1e6 px FROM executions e JOIN orders o"
                    " ON o.id=e.order_id WHERE o.work_item_id=? AND"
                    " o.leg IS NULL", (r["id"],)).fetchone()
                out.append({
                    "id": r["id"], "ticker": r["ticker"], "kind": r["kind"],
                    "state": r["state"], "trigger": r["trigger"],
                    "direction": th.get("direction"),
                    "entry": th.get("entry_high"),
                    "stop": th.get("stop_loss"),
                    "target": th.get("take_profit"),
                    "currency": th.get("currency"),
                    "decision": dec["to_state"] if dec else None,
                    "decided_at": dec["ts"] if dec else None,
                    "broker": dp.get("broker"),
                    "size": dp.get("size_base"), "qty": dp.get("qty"),
                    "filled_qty": fill["q"] if fill else None,
                    "fill_price": fill["px"] if fill else None,
                    "updated_at": r["updated_at"]})
            return {"items": out}

        def _trades(self, days):
            """Every fill in the book — buys and sells, including the ones a
            stop or target made at the broker while nobody was looking — with
            the profit or loss and why the trade happened (Trades page)."""
            from .tools.market import yahoo_symbol
            since = int(clock()) - max(1, min(days, 3650)) * 86400
            accts = {a["id"]: dict(a) for a in conn.execute(
                "SELECT id, broker, environment, base_currency"
                " FROM broker_accounts")}
            rows = [dict(r) for r in conn.execute(
                "SELECT e.id, e.order_id, e.account_id, e.side,"
                " e.qty/1e6 qty, e.price_native/1e6 price, e.currency,"
                " e.commission/1e6 fees, e.other_fees/1e6 other_fees,"
                " e.gross_base/1e6 gross, e.net_base/1e6 net,"
                " e.executed_at, e.work_item_id, i.ticker, i.exchange,"
                " i.name, o.leg, o.limit_price/1e6 order_limit,"
                " o.stop_loss/1e6 order_stop, o.parent_order_id"
                " FROM executions e JOIN instruments i ON i.id=e.instrument_id"
                " LEFT JOIN orders o ON o.id=e.order_id"
                " WHERE e.executed_at >= ? ORDER BY e.executed_at DESC",
                (since,))]
            work = {}

            def item(wi):
                if wi and wi not in work:
                    r = conn.execute("SELECT * FROM work_items WHERE id=?",
                                     (wi,)).fetchone()
                    work[wi] = dict(r) if r else None
                return work.get(wi) if wi else None

            def thesis(w):
                try:
                    return json.loads((w or {}).get("thesis_json") or "{}")
                except ValueError:
                    return {}

            def analysts(wi):
                out = []
                for rep in conn.execute(
                        "SELECT agent_id, payload FROM agent_reports WHERE"
                        " work_item_id=? AND kind='analyst' ORDER BY id",
                        (wi,)):
                    try:
                        s = json.loads(rep["payload"]).get("summary")
                    except (ValueError, AttributeError):
                        s = None
                    if s:
                        out.append(f"{rep['agent_id']}: {s}")
                return out

            def verdict(wi):
                r = conn.execute(
                    "SELECT payload FROM agent_reports WHERE work_item_id=?"
                    " AND agent_id='arbiter' ORDER BY id DESC LIMIT 1",
                    (wi,)).fetchone()
                try:
                    return json.loads(r["payload"]) if r else {}
                except ValueError:
                    return {}

            def approved_at(wi):
                r = conn.execute(
                    "SELECT MIN(ts) FROM events WHERE item_id=? AND"
                    " to_state='approved'", (wi,)).fetchone()
                return r[0] if r else None

            def reason(t):
                w = item(t["work_item_id"]) or {}
                th, kind = thesis(w), w.get("kind")
                why, details = "", []
                if t["leg"] == "stop":
                    why = ("Stop-loss hit — the protective stop "
                           + (f"at {t['order_stop']:g} " if t["order_stop"]
                              else "")
                           + "sold the shares at the broker automatically")
                elif t["leg"] == "target":
                    why = ("Target reached — the take-profit order "
                           + (f"at {t['order_limit']:g} " if t["order_limit"]
                              else "")
                           + "sold the shares at the broker automatically")
                elif kind == "pipeline":
                    conv = th.get("conviction")
                    why = ("Claire's desk recommended this "
                           f"{th.get('direction') or t['side']}"
                           + (f" ({conv:.0%} conviction)" if conv else "")
                           + " and you approved it")
                    details = analysts(w["id"])
                elif kind == "override":
                    origin = w.get("override_of")
                    v = verdict(origin) if origin else {}
                    why = ("The desk passed on it"
                           + (f" ({v['conviction']:.0%} conviction)"
                              if v.get("conviction") else "")
                           + " — you overrode it and approved the trade")
                    ev = conn.execute(
                        "SELECT payload FROM events WHERE item_id=? AND"
                        " to_state='override'", (w["id"],)).fetchone()
                    try:
                        note = json.loads(ev["payload"] or "{}").get("note") \
                            if ev else None
                    except ValueError:
                        note = None
                    if note:
                        details.append(f"your note: {note}")
                    details += analysts(origin) if origin else []
                elif kind == "sell_review":
                    why = ("Sell review after a price alert, approved by you"
                           + (f" — {w['trigger']}" if w.get("trigger")
                              else ""))
                    details = analysts(w["id"])
                elif kind == "manual":
                    cond = (th.get("conditions") or [""])[0]
                    why = (re.sub(r"^MANUAL (\w+)", lambda m: "Your manual "
                                  + m.group(1).lower(), cond) if cond else
                           (w.get("trigger") or "your manual trade"))
                    why += " — approved by you"
                elif w:
                    why = w.get("trigger") or kind or ""
                else:
                    why = "not linked to a Claire run (booked from the broker)"
                if t["leg"]:
                    parent = item(t["work_item_id"]) or {}
                    details.append("protecting the buy from run "
                                   + (parent.get("id") or "?"))
                return why, details, approved_at(w["id"]) if w else None

            # profit / loss: sells from their lot closures, buys from their lot
            open_syms = set()
            for t in rows:
                a = accts.get(t["account_id"]) or {}
                t["account"] = self._account_label(a.get("broker"),
                                                   a.get("environment"))
                t["base_currency"] = a.get("base_currency")
                t["symbol"] = yahoo_symbol(t["ticker"], t["exchange"])
                t["reason"], t["details"], t["approved_at"] = reason(t)
                cl = conn.execute(
                    "SELECT COUNT(*) n, SUM(realized_pl_base)/1e6 pl,"
                    " SUM(cost_base)/1e6 cost, SUM(qty)/1e6 q,"
                    " MAX(holding_days) held FROM lot_closures"
                    " WHERE close_execution_id=?", (t["id"],)).fetchone()
                lot = conn.execute(
                    "SELECT id, qty_opened/1e6 opened,"
                    " qty_remaining/1e6 remaining,"
                    " cost_per_share_base/1e6 cost FROM lots"
                    " WHERE open_execution_id=?", (t["id"],)).fetchone()
                t["closes"] = None
                t["lot"] = None
                if cl and cl["n"]:
                    t["closes"] = {"pl": cl["pl"], "cost": cl["cost"],
                                   "qty": cl["q"], "held_days": cl["held"],
                                   "pct": (cl["pl"] / cl["cost"] * 100
                                           if cl["cost"] else None)}
                if lot:
                    rz = conn.execute(
                        "SELECT SUM(realized_pl_base)/1e6, SUM(qty)/1e6,"
                        " GROUP_CONCAT(close_execution_id) FROM lot_closures"
                        " WHERE lot_id=?", (lot["id"],)).fetchone()
                    t["lot"] = {"opened": abs(lot["opened"]),
                                "remaining": lot["remaining"],
                                "cost": lot["cost"],
                                "realized": rz[0], "closed_qty": rz[1],
                                "closed_by": (rz[2] or "").split(",")
                                if rz[2] else []}
                    if lot["remaining"] > 0:
                        open_syms.add(t["symbol"])
            quotes = {}
            if open_syms:
                try:
                    quotes = {s: float(v["price"]) for s, v in
                              market.spark(sorted(open_syms)).items()
                              if v.get("price") is not None}
                except Exception:               # noqa: BLE001 — best effort
                    quotes = {}
            # money is totalled per ACCOUNT currency, never across them:
            # one "realized P/L" used to add SGD and USD trades (B5)
            summary = {"by_ccy": [], "wins": 0, "losses": 0, "buys": 0,
                       "sells": 0, "priced": True}
            money = {}

            def tot(t):
                ccy = t["base_currency"] or t["currency"] or "?"
                return money.setdefault(ccy, {
                    "ccy": ccy, "realized": 0.0, "unrealized": 0.0,
                    "fees": 0.0})
            for t in rows:
                tot(t)["fees"] += (t["fees"] or 0) + (t["other_fees"] or 0)
                summary["buys" if t["side"] == "buy" else "sells"] += 1
                if t["closes"]:
                    pl = t["closes"]["pl"] or 0
                    tot(t)["realized"] += pl
                    if pl:                  # break-even is neither
                        summary["wins" if pl > 0 else "losses"] += 1
                lot = t["lot"]
                if lot and lot["remaining"] > 0:
                    last = quotes.get(t["symbol"])
                    lot["last"] = last
                    if last is not None:
                        try:
                            fx = float(market.fx(t["currency"],
                                                 t["base_currency"] or
                                                 t["currency"]))
                        except Exception:       # noqa: BLE001
                            fx = 1.0 if t["currency"] == \
                                t["base_currency"] else None
                        if fx is not None:
                            sign = 1 if t["side"] == "buy" else -1
                            lot["unrealized"] = sign * (
                                last * fx - lot["cost"]) * lot["remaining"]
                            tot(t)["unrealized"] += lot["unrealized"]
                    else:
                        summary["priced"] = False
            summary["by_ccy"] = [money[c] for c in sorted(money)]
            return {"trades": rows, "summary": summary, "days": days,
                    "now": int(clock())}

        @staticmethod
        def _account_label(broker, env):
            if broker == "saxo":
                return "Saxo SIM (paper)"
            if broker in ("alpaca", "moomoo") and env != "live":
                return sim_label(broker)
            name = {"saxo_live": "Saxo", "alpaca": "Alpaca",
                    "moomoo": "moomoo"}.get(broker, broker or "broker")
            if env == "live":
                return f"{name} LIVE (real money)"
            return f"{name} {env or ''}".strip()

        def _run_story(self, d):
            """Who started a run, what it concluded and what happened to
            the order afterwards — derived, read-only, never money logic."""
            wi = d["id"]
            th = d.get("thesis")
            if th is None and d.get("thesis_json"):
                try:
                    th = json.loads(d["thesis_json"])
                except ValueError:
                    th = None
            th = th if isinstance(th, dict) else {}
            inst = conn.execute(
                "SELECT name, exchange, currency FROM instruments WHERE"
                " ticker=? ORDER BY (name IS NULL), id LIMIT 1",
                (d["ticker"],)).fetchone()
            trig = d.get("trigger") or ""
            kind = d.get("kind")
            reason = None
            if kind == "override" or d.get("override_of"):
                origin = "override"
            elif kind == "sell_review" or trig.startswith("watcher:"):
                origin = "price_alert"
                reason = trig.split(": ", 1)[1] if ": " in trig else trig
            elif trig.startswith("event scan"):
                origin = "volume_scan"
                reason = trig.split(": ", 1)[1] if ": " in trig else None
            elif trig.startswith("deterministic fallback"):
                origin = "daily_screen"
                reason = ("near the top of the most-active list (the "
                          "screener agent was unavailable)")
            elif trig.startswith("screener") or kind == "screen":
                origin = "daily_screen"
                reason = trig.split(": ", 1)[1] if ": " in trig else None
            elif trig.startswith("launched from the Trading page"):
                origin, reason = "you", "the Launch form on the Trading page"
            elif trig.startswith("launched from the Markets page"):
                origin, reason = "you", "Research on the Markets page"
            elif trig.startswith("manual "):
                origin, reason = "you", trig
            elif trig.startswith("chat: "):
                origin, reason = "chat", trig[len("chat: "):]
            elif not trig:
                # runs launched before 2026-11-06 recorded no source
                origin = "unrecorded"
            else:
                origin = "other"
                reason = trig
            conv = th.get("conviction")
            word = None
            if isinstance(conv, (int, float)):
                word = ("low" if conv < 0.4 else
                        "medium" if conv < 0.7 else "high")
            reasons = [c for c in (th.get("conditions") or [])
                       if isinstance(c, str) and
                       not c.startswith("HUMAN OVERRIDE")][:3]

            evs = [dict(e) for e in conn.execute(
                "SELECT ts, actor, to_state, payload FROM events"
                " WHERE item_id=? ORDER BY id", (wi,))]

            def payload(e):
                try:
                    p = json.loads(e["payload"] or "null")
                except ValueError:
                    p = None
                return p if isinstance(p, dict) else {}
            accts = {r["id"]: dict(r) for r in conn.execute(
                "SELECT id, broker, environment, base_currency"
                " FROM broker_accounts")}
            orders = [dict(o) for o in conn.execute(
                "SELECT id, account_id, side, qty/1e6 qty,"
                " limit_price/1e6 limit_price, stop_loss/1e6 stop_loss,"
                " take_profit/1e6 take_profit, status, broker_order_id,"
                " placed_at, updated_at, leg FROM orders WHERE work_item_id=?"
                " ORDER BY COALESCE(created_at, updated_at)", (wi,))]
            failed_rows = [o for o in orders if o["status"] == "rejected"
                           and not o["broker_order_id"]]
            real = [o for o in orders if o not in failed_rows]
            for o in real:
                a = accts.get(o["account_id"]) or {}
                o["account"] = self._account_label(a.get("broker"),
                                                   a.get("environment"))
                if o["status"] in ("pending_session", "placed",
                                   "partially_filled") and not o["leg"] \
                        and inst is not None:
                    # an outstanding order: say what has to happen for it
                    # to go out / fill — with today's price beside it
                    from .tools.market import yahoo_symbol
                    o["exchange"] = inst["exchange"]
                    o.update(session_facts(inst["exchange"],
                                           cal_or_default(), int(clock())))
                    try:
                        o["last"] = float(market.quote(yahoo_symbol(
                            d["ticker"], inst["exchange"]))["price"])
                    except Exception:           # noqa: BLE001 — best effort
                        o["last"] = None
                    o["filled"] = conn.execute(
                        "SELECT COALESCE(SUM(qty),0)/1e6 FROM executions"
                        " WHERE order_id=?", (o["id"],)).fetchone()[0]
                    o["expires_at"] = conn.execute(
                        "SELECT expires_at FROM orders WHERE id=?",
                        (o["id"],)).fetchone()[0]
                    o["submitted_at"] = conn.execute(
                        "SELECT submitted_at FROM orders WHERE id=?",
                        (o["id"],)).fetchone()[0]
            approval = None
            appr = [e for e in evs if e["to_state"] in
                    ("approved", "approve_retry")]
            if appr:
                p = payload(appr[-1])
                broker = p.get("broker")
                acct = (accts.get(real[-1]["account_id"]) if real else None) \
                    or next((a for a in accts.values()
                             if a["broker"] == broker), None) or {}
                approval = {"ts": appr[0]["ts"], "last_ts": appr[-1]["ts"],
                            "size": p.get("size_base"), "qty": p.get("qty"),
                            "currency": acct.get("base_currency"),
                            "account": self._account_label(
                                acct.get("broker") or broker,
                                acct.get("environment"))}
            tries, seen = [], {}
            for e in evs:
                if e["to_state"] != "resume_failed":
                    continue
                err = payload(e).get("error") or ""
                if "levels amended" in err:
                    continue
                tries.append(e["ts"])
                f = self._friendly_error(err)
                if f not in seen:
                    seen[f] = {"reason": f, "raw": err[:300], "count": 0}
                seen[f]["count"] += 1
            failure = None
            for e in reversed(evs):
                if e["to_state"] == "failed":
                    p = payload(e)
                    errs = p.get("errors") or ([p["error"]] if p.get("error")
                                               else [])
                    fr = list(dict.fromkeys(self._friendly_error(x)
                                            for x in errs))
                    failure = {"reason": "; ".join(fr) or "unknown error",
                               "raw": [str(x)[:400] for x in errs[:6]]}
                    break
            ended = next((dict(payload(e), ts=e["ts"]) for e in reversed(evs)
                          if e["to_state"] in ("rejected", "expired", "cancelled")),
                         None)
            fills = [dict(f) for f in conn.execute(
                "SELECT side, SUM(qty)/1e6 qty,"
                " SUM(1.0*qty*price_native)/SUM(qty)/1e6 avg_price, currency,"
                " MAX(executed_at) at, account_id FROM executions"
                " WHERE work_item_id=? GROUP BY side, currency, account_id",
                (wi,))]
            for f in fills:
                a = accts.get(f["account_id"]) or {}
                f["account"] = self._account_label(a.get("broker"),
                                                   a.get("environment"))
            overrides = [dict(r) for r in conn.execute(
                "SELECT id, state, created_at FROM work_items"
                " WHERE override_of=? ORDER BY created_at", (wi,))]
            return {
                "name": inst["name"] if inst else None,
                "exchange": inst["exchange"] if inst else None,
                "origin": origin, "origin_detail": reason,
                "override_of": d.get("override_of"),
                "overridden_by": overrides,
                "verdict": th.get("direction"),
                "conviction": conv, "conviction_word": word,
                "currency": th.get("currency"),
                "levels": {k: th.get(k) for k in (
                    "entry_low", "entry_high", "stop_loss", "take_profit")},
                "reasons": reasons,
                "approval": approval,
                "orders": real,
                "fills": fills,
                "failed_attempts": {
                    "rows": len(failed_rows), "tries": len(tries),
                    "first_ts": tries[0] if tries else None,
                    "last_ts": tries[-1] if tries else None,
                    "reasons": sorted(seen.values(),
                                      key=lambda r: -r["count"])[:5]},
                "failure": failure,
                "ended": ended,
            }

        def _run_steps(self, wi, files, agent_runs):
            """The pipeline as readable steps: each agent's one-line
            conclusion and signal, parsed from its report.md."""
            from .narrative_format import format_narrative
            out = []
            for agent, label in self._STEP_AGENTS:
                runs = [a for a in agent_runs if a["agent_id"] == agent]
                text = None
                if narratives and f"{agent}.report.md" in (files or []):
                    try:
                        text = format_narrative(
                            narratives.read(wi, f"{agent}.report"))
                    except (OSError, JailError):
                        text = None
                if text is None and not runs:
                    continue
                step = {"agent": agent, "label": label, "signal": None,
                        "conviction": None, "summary": None, "points": [],
                        "has_report": text is not None,
                        "calls": len(runs),
                        "status": ("error" if runs and all(
                            a["status"] == "error" for a in runs) else "ok"),
                        "error": None}
                errs = [a.get("error") for a in runs if a.get("error")]
                if step["status"] == "error" and errs:
                    step["error"] = self._friendly_error(errs[-1])
                for line in (text or "").splitlines():
                    s = line.strip()
                    m = re.match(r"\*\*(?:Signal|Verdict): ([\w-]+)\*\*.*?"
                                 r"conviction ([\d.]+)", s) or \
                        re.match(r"\*\*(Bull|Bear) case\*\*.*?conviction "
                                 r"([\d.]+)", s)
                    if m:
                        step["signal"] = m.group(1).lower()
                        try:
                            step["conviction"] = float(m.group(2))
                        except ValueError:
                            pass
                    elif s.startswith("- "):
                        if len(step["points"]) < 3:
                            step["points"].append(s[2:][:400])
                    elif s and not s.startswith(("#", "**", "_", "·")) \
                            and step["summary"] is None:
                        step["summary"] = s[:500]
                if agent in ("bull", "bear") and step["signal"]:
                    step["signal"] = None        # "bull case" is not a call
                out.append(step)
            return out

        def _run_timeline(self, events, story):
            """The audit trail as a few sentences; a retry loop's hundred
            identical rows collapse into one line."""
            out = []

            def p(e):
                try:
                    v = json.loads(e.get("payload") or "null")
                except ValueError:
                    v = None
                return v if isinstance(v, dict) else {}
            appr = story.get("approval") or {}
            i = 0
            while i < len(events):
                e = events[i]
                st, pl = e["to_state"], p(e)
                if st in ("approve_retry", "resume_failed") and not (
                        st == "resume_failed" and "levels amended" in
                        str(pl.get("error", ""))):
                    j, fails, retries, errs = i, 0, 0, {}
                    while j < len(events) and events[j]["to_state"] in (
                            "approve_retry", "resume_failed"):
                        q = p(events[j])
                        if "levels amended" in str(q.get("error", "")):
                            break
                        if events[j]["to_state"] == "resume_failed":
                            fails += 1
                            f = self._friendly_error(q.get("error"))
                            errs[f] = errs.get(f, 0) + 1
                        else:
                            retries += 1
                        j += 1
                    if fails:
                        why = "; ".join(
                            f"{k} (×{v})" if len(errs) > 1 and v > 1 else k
                            for k, v in errs.items())
                        out.append({"ts": e["ts"], "tone": "bad", "text":
                                    f"Sending the order failed {fails} "
                                    f"time{'s' if fails > 1 else ''} — "
                                    + why})
                    elif retries:
                        out.append({"ts": e["ts"], "tone": "", "text":
                                    "You retried sending the order"})
                    i = max(j, i + 1)
                    continue
                text, tone = None, ""
                if st == "running" and e.get("from_state") is None:
                    text = "Run started"
                elif st == "override":
                    text = (f"You overrode the verdict: "
                            f"{str(pl.get('direction') or '').upper()} at "
                            f"{pl.get('entry')}, stop {pl.get('stop')}, "
                            f"target {pl.get('target')}")
                elif st == "awaiting_approval":
                    text, tone = "Sent to you for approval", "warn"
                elif st == "approved":
                    size = pl.get("size_base")
                    size = (f"{size:,.0f}" if isinstance(size, (int, float))
                            else "")
                    text = (f"You approved {size}"
                            f" {appr.get('currency') or ''} on "
                            f"{appr.get('account') or pl.get('broker')}")
                    text, tone = " ".join(text.split()), "ok"
                elif st == "amended":
                    a, b = pl.get("from") or {}, pl.get("to") or {}
                    text = ("You changed the levels: " + ", ".join(
                        f"{k} {a.get(k)} → {b.get(k)}" for k in
                        ("entry", "stop", "target")
                        if a.get(k) != b.get(k)))
                elif st == "resume_failed":
                    text = None              # "levels amended": said above
                elif st == "executing":
                    text, tone = "Order accepted by the broker", "ok"
                elif st == "done":
                    text, tone = "Finished", "ok"
                    fl = story.get("fills") or []
                    if fl:
                        def px(v):
                            return f"{v:,.4f}".rstrip("0").rstrip(".")
                        text = "Finished — " + "; ".join(
                            ("bought" if f["side"] == "buy" else "sold")
                            + f" {f['qty']:g} shares at "
                            f"{px(f['avg_price'])} {f['currency']}"
                            for f in fl)
                elif st == "failed":
                    f = story.get("failure") or {}
                    text = f"Failed: {f.get('reason') or 'see details'}"
                    tone = "bad"
                elif st == "rejected":
                    text = "You rejected the trade"
                    if pl.get("reason"):
                        text += f" ({pl['reason']})"
                elif st == "expired":
                    text = "Expired" + (f": {pl['reason']}"
                                        if pl.get("reason") else "")
                elif st and not st.startswith("authorize:"):
                    text = str(st).replace("_", " ").capitalize()
                if text:
                    out.append({"ts": e["ts"], "text": text, "tone": tone})
                i += 1
            return out

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

        def _override_context(self, wi):
            """What the human needs on screen to author an override: the
            live quote, the levels that matter (moving averages, recent
            swing low/high, typical daily range) and what the desk holds —
            which decides whether a sell is an exit or a short."""
            from .tools.market import yahoo_symbol
            r = conn.execute("SELECT w.ticker, i.exchange, i.currency FROM"
                             " work_items w LEFT JOIN instruments i ON"
                             " i.ticker = w.ticker WHERE w.id=?",
                             (wi,)).fetchone()
            if r is None:
                return {"error": "unknown work item"}
            sym = yahoo_symbol(r["ticker"], r["exchange"] or "NASDAQ")
            out = {"ticker": r["ticker"], "symbol": sym,
                   "currency": r["currency"], "exchange": r["exchange"],
                   # board lot (mirrors wiring.prepare): a buy smaller than
                   # one lot is refused after approval, so the card warns
                   "lot": desk_exchanges.lot_size(r["exchange"])}
            fx = {}
            for (ccy,) in conn.execute(
                    "SELECT DISTINCT base_currency FROM broker_accounts"):
                try:
                    fx[ccy] = float(market.fx(r["currency"] or ccy, ccy))
                except Exception as e:          # noqa: BLE001 — hint only
                    swallowed(log, "override form: FX hint unavailable",
                              e, every=600)
                    pass
            out["fx"] = fx          # 1 unit of the stock's ccy in each account ccy
            try:
                q = market.quote(sym)
                out["quote"] = {k: q.get(k) for k in (
                    "price", "change_pct", "previous_close", "volume",
                    "currency", "stale", "src")}
                if out["quote"]["price"] is not None:
                    out["quote"]["price"] = float(out["quote"]["price"])
            except Exception as e:              # noqa: BLE001 — shown, not fatal
                out["quote_error"] = str(e)[:200]
            try:
                c = market.chart(sym, "6mo", "1d")
                out["levels"] = _levels(c)
                try:                # context only: never blocks the card
                    out["range2y"] = _range(market.chart(sym, "2y", "1wk"))
                except Exception:               # noqa: BLE001
                    out["range2y"] = None
            except Exception as e:              # noqa: BLE001
                out["levels_error"] = str(e)[:200]
            if not (out.get("levels") or {}).get("atr14"):
                # Yahoo rate-limits this machine; without ATR the
                # "use suggestion" buttons have nothing to work from.
                try:
                    lv = tradingview.levels(sym)
                    if lv:
                        out["levels"] = lv
                        out.pop("levels_error", None)
                except Exception as e:          # noqa: BLE001
                    out.setdefault("levels_error", str(e)[:200])
            held = conn.execute(
                "SELECT l.account_id, SUM(CASE WHEN l.qty_opened < 0 THEN"
                " -l.qty_remaining ELSE l.qty_remaining END)/1e6 AS q"
                " FROM lots l JOIN instruments i ON i.id = l.instrument_id"
                " WHERE i.ticker=? AND l.qty_remaining > 0"
                " GROUP BY l.account_id", (r["ticker"],)).fetchall()
            out["held"] = {h["account_id"]: h["q"] for h in held}
            out["held_total"] = sum(h["q"] for h in held)
            # London bars are in pence, the quote in pounds: same units
            px = (out.get("quote") or {}).get("price")
            # what the shares still held cost per share (the stock's own
            # currency) — a sell's summary shows the profit or loss on them
            out["avg_cost"] = {
                c["account_id"]: quote_units({"v": c["v"]}, px, ("v",))["v"]
                for c in conn.execute(
                    "SELECT l.account_id, SUM(1.0*l.qty_remaining*e.price_native)"
                    " /SUM(l.qty_remaining)/1e6 AS v FROM lots l"
                    " JOIN instruments i ON i.id = l.instrument_id"
                    " JOIN executions e ON e.id = l.open_execution_id"
                    " WHERE i.ticker=? AND l.qty_remaining > 0"
                    " AND l.qty_opened > 0 GROUP BY l.account_id",
                    (r["ticker"],)) if c["v"]}
            out["levels"] = quote_units(out.get("levels") or {}, px)
            out["range2y"] = quote_units(out.get("range2y"), px,
                                         ("high", "low"))
            return out

        def _amend(self, wi, body):
            """Push edited price levels to claire-api; raises AuthError with
            its reason (e.g. a stop on the wrong side) if refused."""
            cur = json.loads((conn.execute(
                "SELECT thesis_json FROM work_items WHERE id=?", (wi,))
                .fetchone() or {"thesis_json": "{}"})["thesis_json"] or "{}")
            want = {"entry": float(body["entry"]),
                    "stop": float(body["stop"]) if body.get("stop") else None,
                    "target": float(body["target"]) if body.get("target")
                    else None}
            if (cur.get("entry_high"), cur.get("stop_loss"),
                    cur.get("take_profit")) == (want["entry"], want["stop"],
                                                 want["target"]):
                return                              # unchanged: nothing to do
            try:
                r = httpx.post(f"{api_base}/internal/amend",
                               json={"work_item_id": wi, "actor": "human",
                                     **want},
                               headers={"x-claire-secret": secret}, timeout=30)
            except Exception as e:                  # noqa: BLE001
                raise approvals.AuthError(502, f"claire-api unreachable ({e})")
            if r.status_code != 200:
                raise approvals.AuthError(r.status_code, (r.json() or {}).get(
                    "detail", f"amend failed ({r.status_code})"))

        def _trade_check(self, body):
            """'Check with Saxo': size the edited order exactly as execution
            would (live quote, whole shares) and ask Saxo's precheck whether
            it would be accepted. Places nothing. Either for a work item
            (work_item_id) or for a trade not yet started — the Sell button
            sends ticker, exchange, direction, entry and qty."""
            from decimal import Decimal

            from .graph.nodes_execution import FRACTION_STEP, position_qty
            from .tools.market import yahoo_symbol
            from .wiring import build_brokers
            wi = str(body.get("work_item_id") or "")
            if wi:
                r = conn.execute("SELECT w.ticker, w.thesis_json, i.exchange"
                                 " FROM work_items w LEFT JOIN instruments i"
                                 " ON i.ticker=w.ticker WHERE w.id=?",
                                 (wi,)).fetchone()
                if r is None:
                    raise HTTPError(404, "unknown work item")
                ticker, exchange = r["ticker"], r["exchange"]
                t = json.loads(r["thesis_json"] or "{}")
            else:
                ticker = str(body.get("ticker") or "").upper().strip()
                exchange = str(body.get("exchange") or "").upper().strip()
                self._check_symbol(ticker, exchange)
                if body.get("direction") not in ("buy", "sell"):
                    raise HTTPError(400, "direction must be buy or sell")
                t = {"direction": body["direction"]}
            broker = body.get("broker")
            adapter = build_brokers(env or {}, conn).get(broker)
            if adapter is None or not hasattr(adapter, "precheck"):
                return {"ok": None, "note": "this account uses the built-in "
                        "paper sim — there is no broker to ask"}
            side = t.get("direction") or "buy"
            try:
                entry = float(body.get("entry") or t.get("entry_high") or 0)
            except (TypeError, ValueError):
                raise HTTPError(400, "entry must be a number") from None
            if entry <= 0:
                raise HTTPError(400, "an entry price is required")
            sym = yahoo_symbol(ticker, exchange or "NASDAQ")
            if side == "buy" and body.get("qty"):
                # the card's share count: check exactly that many
                qty = Decimal(str(body["qty"]))
                if not getattr(adapter, "fractional", True):
                    qty = qty.to_integral_value(rounding="ROUND_FLOOR")
            elif side == "buy":
                q = Decimal(str(market.quote(sym)["price"]))
                qty = position_qty(Decimal(str(body.get("size_base") or 0)),
                                   q, 1, FRACTION_STEP if getattr(
                                       adapter, "fractional", True)
                                   else Decimal(1))
            else:
                qty = Decimal(str(body.get("qty") or 0))
            if qty <= 0:
                return {"ok": False, "error": "that size buys zero shares"}
            # an exit carries no bracket (execution strips it), so neither
            # does its check
            held = self._desk_held_on(broker, ticker, exchange)
            exit_ = (side == "sell" and held > 0) or (side == "buy" and
                                                       held < 0)
            stop = None if exit_ else (float(body["stop"])
                                       if body.get("stop") else None)
            kw = dict(symbol=sym, side=side, qty=qty, limit=entry,
                      stop_loss=stop,
                      take_profit=None if exit_ else (
                          float(body["target"]) if body.get("target")
                          else None))
            try:
                trail = float(body.get("trail_pct") or 0)
            except (TypeError, ValueError):
                trail = 0
            from .graph.nodes_execution import _accepts
            if stop and 0 < trail <= 90 and \
                    _accepts(adapter, "trail_distance", "precheck"):
                # the exact order execution sends: the stop as a trailing
                # stop, distance = the floor % of the entry
                kw["trail_distance"] = entry * trail / 100
            out = adapter.precheck(**kw)
            if out.get("error") and "RateLimitExceeded" in out["error"]:
                out["error"] = "Saxo asks to slow down — wait a second and " \
                               "check again"
            elif out.get("error") and \
                    "SellOrdersAlreadyExistForOwnedContracts" in out["error"] \
                    and exit_:
                # the protective stop/target the desk holds for these shares
                # are what Saxo objects to — and execution cancels exactly
                # those first (DESIGN.md §11a). Saxo cannot check past them.
                out = {**out, "ok": None, "legs_block": True, "note":
                       "Saxo cannot fully check this sell while the "
                       "position's protective stop/target are working there "
                       "— on approval the desk cancels them first, then "
                       f"places your sell of {qty} @ {out.get('price')}; any "
                       "shares left are re-protected at the same levels."}
            return {**out, "qty": float(qty)}

        def _desk_held_on(self, broker, ticker, exchange):
            """Shares the desk's book holds on the account behind `broker`."""
            r = conn.execute(
                "SELECT COALESCE(SUM(CASE WHEN l.qty_opened<0 THEN"
                " -l.qty_remaining ELSE l.qty_remaining END),0) q FROM lots l"
                " JOIN instruments i ON i.id=l.instrument_id JOIN"
                " broker_accounts a ON a.id=l.account_id WHERE a.broker=? AND"
                " i.ticker=? AND i.exchange=? AND l.qty_remaining>0",
                (broker, ticker, exchange)).fetchone()
            return (r["q"] or 0) / 1e6

        def _position_floor(self, body):
            from .watcher import set_position_floor
            inst = str(body.get("instrument_id") or "")
            acct = str(body.get("account_id") or "")
            side = -1 if body.get("side") == "short" else 1
            try:
                pct = float(body.get("pct"))
            except (TypeError, ValueError):
                raise HTTPError(422, "the trailing alert must be a number")
            if not 0.5 <= pct <= 90:
                raise HTTPError(422, "the trailing alert must be between"
                                     " 0.5% and 90%")
            held = conn.execute(
                "SELECT COUNT(*) FROM lots WHERE instrument_id=? AND"
                " account_id=? AND qty_remaining > 0 AND"
                " (qty_opened > 0) = ?", (inst, acct, side > 0)).fetchone()[0]
            if not held:
                raise HTTPError(404, "the desk holds no such position")
            before = set_position_floor(conn, inst, acct, pct, side=side)
            return {"ok": True, "pct": pct, "before": before}

        def _manual_trade(self, body):
            """Forward a trade you started to claire-api (/internal/manual),
            which validates it and puts it at the approval gate."""
            allowed = ("ticker", "exchange", "direction", "entry", "stop",
                       "target", "note", "suggested", "source")
            fwd = {k: body.get(k) for k in allowed if body.get(k) is not None}
            if isinstance(fwd.get("ticker"), str) and \
                    isinstance(fwd.get("exchange"), str):
                from .tools.market import desk_ticker
                fwd["ticker"] = desk_ticker(fwd["ticker"].upper().strip(),
                                            fwd["exchange"].upper().strip())
            fwd["actor"] = "human"
            try:
                r = httpx.post(f"{api_base}/internal/manual", json=fwd,
                               headers={"x-claire-secret": secret}, timeout=30)
            except Exception as e:      # noqa: BLE001
                return self._send(502, {"error":
                                        f"claire-api unreachable ({e})"})
            out = r.json() if r.headers.get("content-type", "") \
                .startswith("application/json") else {}
            if r.status_code != 200:
                detail = out.get("detail", "manual trade failed")
                if isinstance(detail, list):    # pydantic 422 shape
                    detail = "; ".join(str(d.get("msg")) for d in detail
                                       if isinstance(d, dict)) or "invalid"
                return self._send(r.status_code, {"error": detail})
            return self._send(200, out)

        def _saxo_login_start(self, body):
            """Begin a Saxo sign-in from the Saxo accounts page. Needs the
            dashboard key (every POST does), so only Richard can start one;
            Saxo then sends the browser back to SAXO_CALLBACK on THIS server.
            Saxo only redirects to the registered host — localhost — so the
            button works in a browser on this PC."""
            from .tools import saxo_live
            which = body.get("env")
            if which not in saxo_live.ENVS:
                raise HTTPError(400, "env must be sim or live")
            try:
                cfg = saxo_live.app_config(env or {}, which)
            except saxo_live.SaxoError as e:
                raise HTTPError(400, str(e))
            path = urlparse(cfg["redirect_uri"]).path or "/"
            if path != SAXO_CALLBACK:
                raise HTTPError(400, f"the app's redirect URL path is {path}; "
                                     f"the dashboard listens on {SAXO_CALLBACK}")
            redirect = (f"http://localhost:{self.server.server_address[1]}"
                        f"{SAXO_CALLBACK}")
            url = saxo_live.begin_login(which, redirect, env=env or {},
                                        clock=clock)
            return self._send(200, {"url": url})

        def _saxo_login_callback(self, q):
            from .tools import saxo_live
            try:
                if q.get("error"):
                    raise saxo_live.SaxoError(q.get("error_description")
                                              or q["error"])
                which = saxo_live.complete_login(
                    q.get("state"), q.get("code"), env=env or {}, clock=clock)
                where = f"/saxo?env={which}&signin=ok"
            except saxo_live.SaxoError as e:
                body = (f"<meta charset=utf-8><link rel=stylesheet "
                        f"href=/style.css><main><h1>Saxo sign-in failed</h1>"
                        f"<p class=bad>{html_escape(str(e))}</p>"
                        f"<p><a href=/saxo>Back to Saxo accounts</a></p></main>")
                return self._send(400, body.encode(), "text/html; charset=utf-8")
            self.send_response(303)
            self.send_header("Location", where)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _saxo_account(self, which):
            """One Saxo account straight from Saxo (read-only), plus each
            position's share of gross exposure for the allocation bars."""
            from .tools import saxo_live
            out = saxo_live.summary(which, with_orders=True, env=env or {})
            out["signin"] = saxo_live.status(which, clock=clock)
            pos = out.get("positions") or []
            from .tools.saxo_broker import desk_symbol
            for p in pos:
                # the desk's market-data symbol, so the page can chart it
                p["chart_symbol"] = desk_symbol(p.get("symbol"))
            self._fill_missing_prices(pos, (out.get("balance") or {})
                                      .get("Currency"))
            gross = sum(abs(p.get("exposure_base") or 0) for p in pos)
            for p in pos:
                p["weight_pct"] = (round(abs(p.get("exposure_base") or 0)
                                         / gross * 100, 1) if gross else None)
            # the Sell button is offered only for what the DESK bought on
            # this very account; the rest is Richard's own holding
            broker = "saxo_live" if which == "live" else "saxo"
            from .portfolio import open_positions
            # saxo-paper only trades AT Saxo SIM when SIM execution is on;
            # otherwise its book lives in the in-process sim, not here
            at_saxo = which == "live" or (
                (env or {}).get("SAXO_SIM_EXECUTION") or "").lower() \
                == "enabled"
            mine = {(q["symbol"]): q for q in open_positions(conn)
                    if q["broker"] == broker} if at_saxo else {}
            for p in pos:
                d = mine.get(p.get("chart_symbol"))
                p["desk"] = {"held": d["qty"], "account_id": d["account_id"],
                             "broker": broker, "ticker": d["ticker"],
                             "exchange": d["exchange"]} if d else None
            out["desk_broker"] = broker
            return out

        def _fill_missing_prices(self, pos, acct_ccy):
            """Saxo sends CurrentPrice 0 where the account has no market data
            for that exchange (the live account and ASX, 2026-11-06: PRO
            shown at 0.00, a "-100%" loss, while Saxo's own total valued it
            at the real ~0.065). A zero price is no price: use the desk's own
            quote, and re-work P/L from it — marked, never passed off as
            Saxo's."""
            for p in pos:
                if p.get("current_price") or not p.get("amount") or \
                        not p.get("chart_symbol"):
                    continue
                try:
                    q = market.spark([p["chart_symbol"]]).get(
                        p["chart_symbol"]) or {}
                    cur = float(q["price"])
                except Exception:               # noqa: BLE001 — leave as is
                    p["price_missing"] = True
                    continue
                avg, amt = p.get("avg_open_price") or 0, p["amount"]
                p["current_price"] = cur
                p["market_value"] = round(amt * cur, 2)
                p["price_src"] = q.get("src") or "desk quote"
                p["pnl"] = round((cur - avg) * amt, 2) if avg else None
                p["pnl_pct"] = (round((cur - avg) / avg * 100 *
                                      (1 if amt >= 0 else -1), 2)
                                if avg else None)
                try:
                    fx = float(market.fx(p.get("currency") or acct_ccy,
                                         acct_ccy)) if acct_ccy else None
                except Exception:               # noqa: BLE001
                    fx = None
                if fx and p["pnl"] is not None:
                    p["pnl_base"] = round(p["pnl"] * fx, 2)
                    p["exposure_base"] = round(abs(amt * cur * fx), 2)

        def _maintenance(self):
            import sqlite3

            from . import maintenance
            try:
                out = maintenance.latest(conn)
            except sqlite3.OperationalError:    # claire-api not restarted yet
                out = {"latest": None, "history": []}
            job = next((r for r in scheduler.rows_with_next(conn, clock=clock)
                        if r.get("job") == "maintenance"), None)
            out["schedule"] = ({k: job.get(k) for k in (
                "enabled", "spec", "next_run_at", "last_run_at")}
                if job else None)
            out["retention"] = {
                "log_rotate_mb": maintenance.LOG_ROTATE_BYTES // 2 ** 20,
                "log_keep": maintenance.LOG_KEEP,
                "backup_keep": maintenance.BACKUP_KEEP,
                "checkpoint_days": maintenance.CHECKPOINT_DAYS,
                "health_days": maintenance.HEALTH_DAYS,
                "narrative_days": maintenance.NARRATIVE_DAYS,
                "sdk_days": maintenance.SDK_TRANSCRIPT_DAYS,
                "runs_keep": maintenance.RUNS_KEEP}
            return out

        def _from_this_pc(self):
            """The browser is on this machine (loopback), not the LAN."""
            ip = (self.client_address or ("",))[0]
            return ip in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

        def _nav_state(self):
            """What every page's menu shows: how many decisions are waiting
            and whether real money is switched on. Two reads, no writes —
            every open tab polls it."""
            from . import live_trading
            (pending,) = conn.execute(
                "SELECT COUNT(*) FROM work_items WHERE"
                " state='awaiting_approval'").fetchone()
            row = conn.execute(
                "SELECT armed FROM live_trading WHERE id=1").fetchone()
            enabled = live_trading.env_enabled(env or {})
            (live_accounts,) = conn.execute(
                "SELECT COUNT(*) FROM broker_accounts WHERE"
                " environment='live'").fetchone()
            return {"pending": pending,
                    "live_armed": bool(row and row["armed"]) and enabled,
                    "live_enabled": enabled, "live_accounts": live_accounts}

        def _live_status(self):
            from . import live_trading
            st = live_trading.status(conn, env or {}, clock=clock)
            # why arming is not possible right now, in the order to fix it
            if not st["enabled_in_env"]:
                why = (f"{live_trading.ENV_FLAG}=enabled is not set in "
                       "etc/claire.env (then bin/claire restart)")
            elif not st["accounts"]:
                why = "no live account yet: run bin/claire live setup"
            elif not self._from_this_pc():
                why = ("arming is only accepted from a browser on the desk's "
                       "own PC (http://127.0.0.1:7787)")
            else:
                why = None
            return {**st, "metrics": live_trading.metrics(conn, clock=clock),
                    "arm_phrase": live_trading.ARM_PHRASE,
                    "can_arm_here": why is None, "arm_blocked": why}

        def _agent_graph(self):
            """Every node carries a status the UI renders identically:
            active (running now) · ok (configured and proven) · warn
            (degraded) · error (cannot run / failing) · idle (configured,
            never run) · disabled."""
            now = int(clock())
            provs = {p["id"]: p for p in self._providers()}
            from .providers import claude_sdk
            sdk_why = claude_sdk.not_ready()
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
                elif p.get("kind") == "claude_sdk" and sdk_why:
                    state, detail = "error", sdk_why
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
                # where orders REALLY go — the same rule as
                # wiring.build_brokers (claire-api builds the adapters). An
                # MCP row is configuration only: no MCP server ever places.
                adapter = venue_adapter(acct, env or {})
                if adapter == "saxo-sim":
                    bstate, bdetail = "ok", ("orders go to Saxo SIM — real "
                                             "Saxo order handling, "
                                             "simulated money; SGX shares "
                                             "are simulated by the desk "
                                             "(Saxo SIM lists them only as "
                                             "CFDs)")
                elif adapter == "saxo-live":
                    bstate, bdetail = "warn", ("REAL MONEY at Saxo — only "
                                               "while live trading is armed")
                elif mcp and refs and not creds:
                    bstate, bdetail = "warn", ("Claire's paper sim · MCP "
                                               "configured, creds missing "
                                               "(unused)")
                elif mcp:
                    bstate, bdetail = "ok", ("Claire's paper sim · MCP "
                                             "configured but not used for "
                                             "orders")
                else:
                    bstate, bdetail = "idle", "Claire's paper sim"
                brokers.append({
                    "broker": acct["broker"], "account": acct["id"],
                    "environment": acct["environment"],
                    "cash": repo.cash_balance(acct["id"]) / 1e6,
                    "ccy": acct["base_currency"],
                    "open_orders": open_orders,
                    "state": bstate, "detail": bdetail,
                    "adapter": adapter,
                    "mcp": {"command": " ".join(json.loads(mcp["command"])),
                            "env_refs": refs,
                            "creds_present": creds,
                            "used_for_execution": False,
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
                    "loops": background_loops(env or {}),
                    "brokers": brokers, "claire_api": api_ok(),
                    "mcps": self._mcps(), "health": health,
                    "book": {"open_orders": open_orders,
                             "positions": positions, "pending": pending},
                    "market_data": {
                        "source": primary or "yahoo",
                        "fallbacks": " · ".join(
                            ["tradingview", "yahoo"]
                            + (["saxo charts"] if getattr(
                                market, "_chart_fallback", None) else [])
                            + ["ecb fx"]),
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
            pid = str(body.get("id", "")).strip()
            if not _ID_RE.fullmatch(pid):
                return self._send(400, {"error":
                                        "id must be 2-30 chars [a-z0-9_-]"})
            kind = body.get("kind")
            if kind not in PROVIDER_KINDS:
                return self._send(400, {"error": "kind must be anthropic |"
                                        " claude_sdk | openai_compatible |"
                                        " custom"})
            # C1: the key ref is a NAME from a fixed list, never free-form —
            # otherwise base_url=attacker + api_key_ref=CLAIRE_DASHBOARD_KEY
            # turns one probe into a key-exfiltration
            key_ref = body.get("api_key_ref") or None
            if key_ref is not None and key_ref not in PROVIDER_KEY_REFS:
                return self._send(400, {"error": "api_key_ref must be one of "
                                        + ", ".join(sorted(PROVIDER_KEY_REFS))
                                        + " (or empty)"})
            if kind == "claude_sdk":
                key_ref = None              # the SDK uses the CLI's login
            base_url = body.get("base_url") or None
            if base_url is not None and (
                    not isinstance(base_url, str) or len(base_url) > 300 or
                    urlparse(base_url).scheme not in ("http", "https") or
                    not urlparse(base_url).netloc):
                return self._send(400, {"error":
                                        "base_url must be an http(s) URL"})
            name = body.get("display_name") or pid
            if not isinstance(name, str) or len(name) > 60:
                return self._send(400, {"error":
                                        "display_name must be ≤60 chars"})
            for k in ("cost_per_1k_in", "cost_per_1k_out"):
                v = body.get(k)
                if v is not None and (isinstance(v, bool) or not isinstance(
                        v, (int, float)) or not 0 <= v <= 10):
                    return self._send(400, {"error": f"{k} must be 0..10"})
            caps = body.get("capabilities") or {}
            if not isinstance(caps, dict) or not all(
                    isinstance(v, bool) for v in caps.values()):
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
                (pid, name, kind, base_url, key_ref,
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
            if not command or not isinstance(command, list) or not all(
                    isinstance(c, str) and c for c in command) or \
                    len(command) > 32:
                return self._send(400, {"error": "command required"})
            refs = body.get("env_refs") or []
            if isinstance(refs, str):
                refs = [r.strip() for r in refs.split(",") if r.strip()]
            if not isinstance(refs, list) or any(
                    not isinstance(r, str) or
                    not re.fullmatch(r"[A-Z][A-Z0-9_]*", r) for r in refs):
                return self._send(400, {"error": "env_refs must be UPPER_CASE"
                                        " env var NAMES (values stay in"
                                        " claire.env)"})
            # the desk's own secrets must never be handed to a third-party
            # process, whenever MCP launching lands (same lesson as C1)
            if any(r.startswith("CLAIRE_") or r in PROVIDER_KEY_REFS
                   for r in refs):
                return self._send(400, {"error": "env_refs may not name "
                                        "CLAIRE_* or LLM provider keys"})
            broker = body.get("broker") or None
            if broker is not None and (not isinstance(broker, str) or
                                       not _ID_RE.fullmatch(broker)):
                return self._send(400, {"error": "broker must be an id"})
            conn.execute(
                "INSERT INTO mcp_servers (id, broker, command, env_refs,"
                " note, enabled) VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET broker=excluded.broker,"
                " command=excluded.command, env_refs=excluded.env_refs,"
                " note=excluded.note, enabled=excluded.enabled",
                (mid, broker, json.dumps(command),
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

        def _check_symbol(self, ticker, exchange):
            # the Desk's own validator, so the dashboards refuse exactly what
            # start_run would (imported lazily: it pulls in the graph)
            from .api.desk import validate_symbol
            try:
                validate_symbol(ticker, exchange)
            except ValueError as e:
                raise HTTPError(400, str(e)) from None

        def _start_run(self, body):
            """Validated before it leaves for claire-api: the ticker becomes
            part of the work-item id, which every page renders."""
            ticker = body.get("ticker")
            exchange = body.get("exchange")
            self._check_symbol(ticker, exchange)
            if not isinstance(body.get("currency"), str) or \
                    not _CCY_RE.fullmatch(body["currency"]):
                raise HTTPError(400, "currency must be a 3-letter code")
            if body.get("kind", "pipeline") not in ("pipeline",
                                                    "sell_review"):
                raise HTTPError(400, "kind must be pipeline|sell_review")
            lot = body.get("lot_size", 1)
            if not isinstance(lot, int) or isinstance(lot, bool) or \
                    not 1 <= lot <= 10000:
                raise HTTPError(400, "lot_size must be an integer 1..10000")
            fwd = {k: body[k] for k in ("ticker", "exchange", "currency",
                                        "kind", "lot_size", "instrument_id",
                                        "trigger") if k in body}
            try:
                r = httpx.post(f"{api_base}/api/run", json=fwd, timeout=10)
            except Exception as e:              # noqa: BLE001
                return self._send(502, {"error":
                                        f"claire-api unreachable ({e})"})
            try:
                out = r.json()
            except ValueError:
                out = {"error": f"claire-api {r.status_code}"}
            return self._send(r.status_code, out)

        def _assign_agent(self, body):
            # model / provider ids end up in pages and in provider API calls:
            # plain identifiers only
            for k in ("id", "provider_id", "fallback_provider_id"):
                v = body.get(k)
                if (v is not None or k != "fallback_provider_id") and (
                        not isinstance(v, str) or not _ID_RE.fullmatch(v)):
                    raise HTTPError(400, f"{k} must be 2-30 chars [a-z0-9_-]")
            for k in ("model", "fallback_model"):
                v = body.get(k)
                if (v is not None or k == "model") and (
                        not isinstance(v, str) or not _MODEL_RE.fullmatch(v)):
                    raise HTTPError(400, f"{k} must be a plain model name")
            if conn.execute("SELECT 1 FROM agents WHERE id=?",
                            (body["id"],)).fetchone() is None:
                raise HTTPError(400, f"unknown agent {body['id']!r}")
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
        def process_request_thread(self, request, client_address):
            """One thread per request, so close that thread's desk.db
            connection when the request ends (db.ThreadLocalConnection
            .release). A raw :memory: connection (tests) has no release."""
            try:
                super().process_request_thread(request, client_address)
            finally:
                release = getattr(conn, "release", None)
                if callable(release):
                    release()

        def handle_error(self, request, client_address):
            """A browser closing an SSE stream is normal, not an error — it
            was filling the log with tracebacks and hiding the real ones."""
            import sys
            if isinstance(sys.exc_info()[1], (BrokenPipeError,
                                              ConnectionResetError)):
                return
            super().handle_error(request, client_address)

    return Server((bind, port), Handler)


def post_sell_review(api_base, inst, trigger=None, *, post=httpx.post):
    """Ask claire-api to start a sell review; returns its work item id.
    RAISES when it did not start (api down, timeout, an error status, no id
    in the answer). It used to return None for all of those, and the watcher
    counted the alert as fired with nothing behind it."""
    r = post(f"{api_base}/api/run",
             json={"ticker": inst["ticker"], "exchange": inst["exchange"],
                   "currency": inst["currency"], "instrument_id": inst["id"],
                   "trigger": trigger, "kind": "sell_review"}, timeout=10)
    if r.status_code != 200:
        raise RuntimeError(f"claire-api answered HTTP {r.status_code}: "
                           f"{r.text[:120]}")
    wi = r.json().get("work_item_id")
    if not wi:
        raise RuntimeError("claire-api returned no work item id")
    return wi


def start_watcher_thread(conn, repo, market, *, api_base, interval=600,
                         clock=time.time, cal=None, alert=None):
    """The §15 loop lives in the dashboards process; sell-review launches go
    through claire-api's HTTP seam with a SHORT timeout (§15 lesson 8)."""
    from .watcher import Watcher

    def start_sell_review(inst, trigger=None):
        return post_sell_review(api_base, inst, trigger)

    watcher = Watcher(conn, repo, market, start_sell_review, clock=clock,
                      cal=cal, alert=alert)

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
            except Exception as e:               # noqa: BLE001
                swallowed(log, "watcher cadence could not be read; using the default",
                          e, every=600)
                pass
            try:
                if enabled:
                    rep_ = watcher.tick()
                    # so Agents & Schedule shows when it last ran (it said
                    # "never": only claire-api's scheduler wrote this row)
                    conn.execute(
                        "UPDATE schedules SET last_run_at=?, last_result=?"
                        " WHERE job='watcher'",
                        (int(clock()), json.dumps({
                            "checked": rep_.get("checked"),
                            "fired": len(rep_.get("fired") or []),
                            "launch_failed": len(
                                rep_.get("launch_failed") or []),
                            "market_closed": len(rep_.get("closed") or []),
                            "unpriced": len(rep_.get("unpriced") or [])})))
            except Exception:                    # noqa: BLE001
                log.exception("watcher tick failed")
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
    log.warning("generated CLAIRE_DASHBOARD_KEY (see %s)", env_path)
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
    from . import logsetup
    logsetup.configure("claire-dashboards")
    var = API_ROOT / "var"
    conn = db.connect(var / "desk.db")
    db.init(conn)
    repo = Repo(conn)
    market = build_market()
    secret = os.environ.get("CLAIRE_INTERNAL_SECRET", "")
    dash_key = ensure_dash_key(API_ROOT / "etc" / "claire.env")
    cal = sessions.load(conn) or None
    from . import alerts
    start_watcher_thread(conn, repo, market,
                         api_base="http://127.0.0.1:7788", cal=cal,
                         alert=alerts.send)
    # approvals + refused trades → the owner's Telegram DM (buttons relayed
    # back by Bob's bot to /api/telegram-decision)
    from . import telegram_approvals
    telegram_approvals.start_pusher_thread(conn, market, os.environ)
    # the arbiter's notes in plain English, written as soon as a card
    # arrives so the Approvals page never waits on the LLM
    from . import arbiter_notes
    arbiter_notes.start_writer_thread(conn)
    bind =os.environ.get("CLAIRE_DASHBOARD_BIND", "127.0.0.1")
    # optional extra Host names (e.g. a DNS alias for this box); the
    # loopback + hostname + LAN-IP set is always derived from `bind`
    extra = [h.strip().lower() for h in os.environ.get(
        "CLAIRE_DASHBOARD_HOSTS", "").split(",") if h.strip()]
    allowed = (default_allowed_hosts(bind) | set(extra)) if extra else None
    srv = create_server(conn, repo, market, secret=secret, dash_key=dash_key,
                        narratives=Narratives(var / "narratives"),
                        env=os.environ, cal=cal, bind=bind,
                        allowed_hosts=allowed)
    log.info("dashboards on http://%s:7787", bind)
    if bind not in ("127.0.0.1", "localhost"):
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))          # no packet sent; reads the route
            lan = s.getsockname()[0]
            s.close()
            log.info("reachable on your network at http://%s:7787", lan)
        except OSError:
            pass
        log.warning("anyone on this network can READ the desk; every POST "
                    "(runs, approvals, edits, cash) needs the dashboard key")
    srv.serve_forever()


if __name__ == "__main__":
    main()
