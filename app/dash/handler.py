"""The request handler: plumbing, authentication, and the GET / POST /
DELETE dispatch. The views and actions it dispatches to are mixins in
the sibling modules; everything a request needs from the server is on
`self.ctx` (app/dash/ctx.py), which create_server sets on a subclass.
Moved out of the create_server closure in app/dashboards.py
(REVIEW-2026-11-11 §3.2).

Routing is three tables (below) instead of one long if-chain, and every
route is a method of its own. The connection is HTTP/1.1 and kept open
between requests (§4.2); `end_headers` is where that is made safe."""
import gzip
import hmac
import json
import logging
import queue as _queue
import re
import select
import socket
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs
from urllib.parse import urlparse

import httpx

from .. import approvals
from .. import deskname
from .. import scheduler
from ..levels import _range
from ..levels import quote_units
from ..providers import health as provider_health
from ..providers.registry import PROVIDER_KEY_REFS
from ..tools import tradingview
from .common import WEB
from .common import SAXO_CALLBACK
from .common import PAGES
from .common import STATIC
from .common import MAX_BODY
from .common import HTTPError
from .common import host_only
from .common import _validate_schedule_patch
from .views_system import SystemViews
from .views_runs import RunViews
from .views_orders import OrderViews
from . import claire_api
from . import common
from .actions import Actions

log = logging.getLogger(__name__)


# ── the route tables ─────────────────────────────────────────────────
# What answers each URL: path → the NAME of a Handler method, looked up
# on the handler when the request arrives (so a subclass or a test can
# still replace one). This was a 55-branch `if u.path == …` chain with
# the route logic written inside it (REVIEW-2026-11-11 §3.2).
#
# Views that need nothing from the request: the answer is 200 and
# whatever the view returns. They live in the views_* mixins.
GET_VIEWS = {
    "/api/overview": "_overview",
    "/api/approvals": "_approvals",
    "/api/portfolio": "_portfolio",
    "/api/runs": "_runs",
    "/api/agents": "_agents",
    "/api/providers": "_providers",
    "/api/agent-graph": "_agent_graph",
    "/api/live-status": "_live_status",
    "/api/nav-state": "_nav_state",
    "/api/maintenance": "_maintenance",
    "/api/exchanges": "_exchanges",
    "/api/mcps": "_mcps",
    "/api/data-sources": "_data_sources",
    "/api/orders": "_orders",
}
# Everything else. A route adds itself with @get(path) / @post(path) on
# its own method, so the URL sits directly on the code that guards and
# answers it — which is where anyone checking "is this one key-gated?"
# looks, and where the tests that read this file look too.
GET_ROUTES = {}         # path → method(q): q is the query string, as a dict
POST_ROUTES = {}        # path → method(body): body is the parsed JSON object


def _adds_to(table):
    def at(path):
        def register(method):
            # a second method on the same URL would silently replace the
            # first — and with it whatever checks the first one made
            if path in table or (table is GET_ROUTES and path in GET_VIEWS):
                raise RuntimeError(f"two routes for {path}")
            table[path] = method.__name__
            return method
        return register
    return at


get = _adds_to(GET_ROUTES)
post = _adds_to(POST_ROUTES)


class Handler(SystemViews, RunViews, OrderViews, Actions, BaseHTTPRequestHandler):
    ctx = None      # set per server by create_server

    # The pages poll several endpoints every few seconds, and under
    # HTTP/1.0 each poll was a new TCP connection and a new thread
    # (REVIEW-2026-11-11 §4.2). HTTP/1.1 keeps both for the browser's
    # next request.
    protocol_version = "HTTP/1.1"

    # The socket timeout. It bounds a client that announces a body and
    # never sends it (or stalls mid-headers), and it is also how long an
    # idle kept-alive connection may hold its thread: long enough to
    # cover the pages' 8, 15 and 20 s polls, short enough that a closed
    # laptop lid frees its threads within half a minute. Deliberately
    # not 20 or 30: a timeout equal to a poll period would hang up just
    # as the next poll is sent. The browser opens a new connection when
    # it next needs one.
    timeout = 25

    # A response goes out as two writes (headers, then body). On a
    # connection that stays open, Nagle's algorithm holds the second one
    # back until the browser acknowledges the first — about 40 ms added
    # to every request after the first. Send each write at once instead.
    disable_nagle_algorithm = True

    def log_message(self, *a):
        pass

    # ── keeping the connection ───────────────────────────────────────
    # A kept connection is only safe while both ends agree where one
    # exchange stops and the next starts. The methods below track, for
    # the request in hand, what was sent and what was read, so that
    # end_headers can close the connection whenever that is in doubt.
    _status = 0             # the status of the response being written
    _sent = frozenset()     # the names of the headers written for it
    _body_read = True       # the request's body (if any) has been taken
    _answered = False       # a final response has been started

    def handle_one_request(self):
        """One request, then give this thread's desk.db handle back. The
        thread now outlives the request, and a handle kept with it would
        carry anything a failed request left open — a transaction, and
        with it SQLite's write lock — into the next request and hold it
        for as long as the page keeps polling. Closing the handle rolls
        that back, exactly as the end of the thread did under HTTP/1.0.
        A raw :memory: connection (tests) has no release."""
        try:
            super().handle_one_request()
        finally:
            release = getattr(getattr(self.ctx, "conn", None), "release",
                              None)
            if callable(release):
                release()

    def parse_request(self):
        self._body_read = self._answered = False
        return super().parse_request()

    def send_response_only(self, code, message=None):
        self._status, self._sent = code, set()
        super().send_response_only(code, message)

    def send_header(self, keyword, value):
        self._sent.add(keyword.lower())
        super().send_header(keyword, value)

    def _unread_body(self):
        """The request said a body follows and nobody read all of it — a
        POST refused at the key or Content-Type check, a body over the
        limit, a GET that carries one — or said it in a way that cannot
        be trusted. Whatever is left in the socket would be read as the
        NEXT request on this connection."""
        headers = getattr(self, "headers", None)
        if headers is None:
            return False
        lengths = headers.get_all("Content-Length") or []
        if headers.get("Transfer-Encoding") or len(lengths) > 1:
            return True         # no telling where this request ends
        if not lengths:
            return False
        if not re.fullmatch(r"[0-9]+", lengths[0].strip()):
            return True         # blank, negative, not a number
        return int(lengths[0]) > 0 and not self._body_read

    def end_headers(self):
        if self._status >= 200:             # not an interim "100 Continue"
            # 204 and 304 have no body by definition; anything else with
            # no Content-Length (the live-event and chat streams) ends
            # when the connection does, so it cannot be kept
            open_ended = "content-length" not in self._sent and \
                self._status not in (204, 304)
            if open_ended or self._unread_body():
                self.close_connection = True
            # say so, also when it was the browser that asked to close
            if self.close_connection and "connection" not in self._sent:
                self.send_header("Connection", "close")
            self._answered = True
        super().end_headers()

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
        if self._answered:
            # the answer had already started (a stream, or a write that
            # stalled half-way). A second response behind it would be
            # read as the reply to the browser's next request: end the
            # connection instead.
            self.close_connection = True
            return
        try:
            self._send(code, body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    GZIP_MIN = 1024         # below this the saving is not worth the CPU
    GZIP_TYPES = ("application/json", "text/", "application/javascript",
                  "image/svg+xml")

    def _send(self, code, body, ctype="application/json", headers=()):
        data = body if isinstance(body, bytes) else json.dumps(
            body, default=str).encode()
        # the desk's name is a setting (app/deskname.py): every page, script
        # and piece of data says it as it leaves. Untouched with the default.
        if ctype.startswith(self.GZIP_TYPES):
            data = deskname.brand_bytes(data, self.ctx.env or None)
        extra = list(headers)
        # /api/runs and /api/approvals are over 100 KB of JSON and are
        # polled; nothing was compressed (REVIEW-2026-11-11 §4.2)
        if len(data) >= self.GZIP_MIN and ctype.startswith(self.GZIP_TYPES) \
                and "gzip" in (self.headers.get("Accept-Encoding") or ""):
            data = gzip.compress(data, compresslevel=5)
            extra += [("Content-Encoding", "gzip"),
                      ("Vary", "Accept-Encoding")]
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, path, ctype):
        """A page or a shared script/stylesheet. The browser keeps its copy
        and asks "still this version?" each time (no-cache = always check):
        an unchanged file costs a 304 with no body, a changed one is picked
        up at once — nothing is ever served stale."""
        st = path.stat()
        # the name is part of the version: renaming the desk must not leave
        # the browser showing its cached copy with the old one
        named = deskname.version(self.ctx.env or None)   # '' by default
        etag = (f'"{st.st_mtime_ns:x}-{st.st_size:x}'
                + ("-" + named if named else "") + '"')
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            return
        self._send(200, path.read_bytes(), ctype,
                   headers=[("ETag", etag), ("Cache-Control", "no-cache")])

    def _check_request(self):
        """Host allowlist + Origin match, for every method. Without the
        Host check a DNS-rebinding page (evil.example → our IP) is
        'same-origin' to the browser and can read every GET route."""
        host_hdr = self.headers.get("Host")
        if not host_hdr or not self.ctx.host_allowed(host_only(host_hdr)):
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
        self._body_read = True      # the connection can take another request
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
        return bool(self.ctx.dash_key) and hmac.compare_digest(
            given.encode("utf-8", "replace"), self.ctx.dash_key.encode())

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
                return self._send_file(WEB / PAGES[u.path],
                                       "text/html; charset=utf-8")
            if u.path in STATIC:
                name, ctype = STATIC[u.path]
                return self._send_file(WEB / name, ctype)
            view = GET_VIEWS.get(u.path)
            if view:
                return self._send(200, getattr(self, view)())
            route = GET_ROUTES.get(u.path)
            if route:
                return getattr(self, route)(q)
            self._send(404, {"error": "not found"})
        except Exception as e:               # noqa: BLE001
            self._failed(e)

    @get("/api/chat-history")
    def _get_chat_history(self, q):
        # what was said to Claire is more personal than the book:
        # paired browsers only, like the real Saxo account page
        if not self._paired():
            raise HTTPError(403, "dashboard not paired")
        return self._send(200, self._chat_history(
            q.get("thread", "dashboard")))

    @get("/api/usage")
    def _get_usage(self, q):
        return self._send(200, self._usage(q.get("days", "7")))

    @get("/api/schedules")
    def _get_schedules(self, q):
        return self._send(200, scheduler.rows_with_next(
            self.ctx.conn, clock=self.ctx.clock))

    @get("/api/position-chart")
    def _get_position_chart(self, q):
        from .. import portfolio
        return self._send(200, portfolio.chart_for(
            self.ctx.conn, self.ctx.market, q.get("instrument", ""),
            range_=q.get("range", "5d"),
            interval=q.get("interval", "60m")))

    @get("/api/watchlist")
    def _get_watchlist(self, q):
        return self._send(200, [dict(r) for r in self.ctx.conn.execute(
            "SELECT * FROM watchlist ORDER BY exchange, ticker")])

    @get("/api/watchlist-data")
    def _get_watchlist_data(self, q):
        # the Watchlist page in one call (app/watchlist.py)
        from .. import watchlist
        return self._send(200, watchlist.view(
            self.ctx.conn, self.ctx.repo, self.ctx.market, env=self.ctx.env or {}, clock=self.ctx.clock,
            cal=self.ctx.cal_or_default()))

    @get("/api/price-triggers")
    def _get_price_triggers(self, q):
        from .. import price_triggers
        return self._send(200, price_triggers.list_triggers(
            self.ctx.conn, status=q.get("status"),
            ticker=q.get("ticker")))

    @get("/api/events")
    def _get_events(self, q):
        return self._sse()

    @get("/api/fx")
    def _get_fx(self, q):
        return self._send(200, {"rate": str(self.ctx.market.fx(
            q.get("from", "USD"), q.get("to", "USD")))})

    @get(SAXO_CALLBACK)
    def _get_saxo_callback(self, q):
        return self._saxo_login_callback(q)

    @get("/api/saxo-account")
    def _get_saxo_account(self, q):
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

    @get("/api/trades")
    def _get_trades(self, q):
        return self._send(200, self._trades(
            int(q.get("days", 90) or 90)))

    @get("/api/approvals-archive")
    def _get_approvals_archive(self, q):
        return self._send(200, self._approvals_archive(
            int(q.get("days", 30) or 30)))

    @get("/api/range2y")
    def _get_range2y(self, q):
        # the 2-year high / low for a symbol (weekly bars, cached
        # by the market layer) — the Watchlist order summary
        sym = q.get("symbol", "")
        try:
            rng = _range(self.ctx.market.chart(sym, "2y", "1wk"))
        except Exception as e:      # noqa: BLE001 — optional
            return self._send(200, {"range2y": None,
                                    "error": str(e)[:200]})
        try:                        # bars may be in pence
            px = self.ctx.market.quote(sym).get("price")
        except Exception:           # noqa: BLE001
            px = None
        return self._send(200, {"range2y": quote_units(
            rng, px, ("high", "low"))})

    @get("/api/arbiter-notes")
    def _get_arbiter_notes(self, q):
        # the arbiter's notes in plain English for an approval
        # card. Reading a stored explanation is free; writing
        # one is an LLM call, so it needs the dashboard key.
        from .. import arbiter_notes
        wi = q.get("id", "")
        row = self.ctx.conn.execute("SELECT thesis_json FROM work_items"
                           " WHERE id=?", (wi,)).fetchone()
        if row is None:
            return self._send(404, {"error": "unknown work item"})
        notes = arbiter_notes.notes_of(row["thesis_json"])
        hit = arbiter_notes.cached(self.ctx.conn, wi, notes) if notes \
            else {"summary": None, "notes": []}
        if hit is not None:
            return self._send(200, hit)
        if not self._paired():
            raise HTTPError(403, "dashboard not paired")
        try:
            return self._send(200, arbiter_notes.explain(self.ctx.conn, wi))
        except Exception as e:      # noqa: BLE001 — LLM/parse
            return self._send(502, {"error": "could not write the"
                                    " plain-English notes: "
                                    f"{str(e)[:200]}"})

    @get("/api/levels-for-override")
    def _get_levels_for_override(self, q):
        return self._send(200, self._override_context(
            q.get("id", "")))

    @get("/api/agent-activity")
    def _get_agent_activity(self, q):
        return self._send(200, self._agent_activity(
            q.get("id", "")))

    @get("/api/agent-narrative")
    def _get_agent_narrative(self, q):
        return self._send(200, self._narrative(
            q.get("agent", ""), q.get("work_item", "")) or
            {"error": "no narrative for that run"})

    @get("/api/exchange-info")
    def _get_exchange_info(self, q):
        return self._send(200, self._exchange_info(
            q.get("exchange", "NASDAQ")))

    @get("/api/screener")
    def _get_screener(self, q):
        return self._send(200, self.ctx.market.screener(
            q.get("exchange", "NASDAQ"),
            q.get("sort", "intradaymarketcap"),
            int(q.get("start", 0)), int(q.get("count", 100))))

    @get("/api/tv-recs")
    def _get_tv_recs(self, q):
        return self._send(200, tradingview.recommendations(
            q.get("exchange", "NASDAQ")))

    @get("/api/run-detail")
    def _get_run_detail(self, q):
        return self._send(200, self._run_detail(q.get("id", "")))

    @get("/api/quote")
    def _get_quote(self, q):
        syms = [s for s in q.get("symbols", "").split(",") if s]
        return self._send(200, self.ctx.market.spark(syms))

    @get("/api/search")
    def _get_search(self, q):
        return self._send(200, self.ctx.market.search(q.get("q", "")))

    @get("/api/chart")
    def _get_chart(self, q):
        return self._send(200, self.ctx.market.chart(
            q.get("symbol", ""), q.get("range", "6mo"),
            q.get("interval", "1d")))

    @get("/api/feed")
    def _get_feed(self, q):
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
            rows = [dict(r) for r in self.ctx.conn.execute(
                "SELECT * FROM events WHERE id > ? ORDER BY id"
                " LIMIT 300", (since,))]
        else:
            rows = [dict(r) for r in self.ctx.conn.execute(
                "SELECT * FROM (SELECT * FROM events ORDER BY"
                " id DESC LIMIT 300) ORDER BY id")]
        return self._send(200, {
            "events": rows,
            "last": rows[-1]["id"] if rows else since})

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
            route = POST_ROUTES.get(u.path)
            if route:
                return getattr(self, route)(body)
            self._send(404, {"error": "not found"})
        except Exception as e:               # noqa: BLE001
            self._failed(e)

    @post("/api/ask")
    def _post_ask(self, body):
        return self._ask_proxy(body, "/ask")

    @post("/api/agent-ask")
    def _post_agent_ask(self, body):
        return self._ask_proxy(body, "/agent/ask")

    @post("/api/flag-ack")
    def _post_flag_ack(self, body):
        # clear (or bring back) a Mission Control warning
        keys = body.get("keys") or [body.get("key")]
        if not all(isinstance(k, str) and 0 < len(k) <= 120
                   for k in keys) or len(keys) > 50:
            raise HTTPError(400, "keys must be short strings")
        for k in keys:
            if body.get("restore"):
                self.ctx.conn.execute("DELETE FROM dashboard_acks"
                             " WHERE key=?", (k,))
            else:
                self.ctx.conn.execute(
                    "INSERT INTO dashboard_acks (key, acked_at)"
                    " VALUES (?,?) ON CONFLICT(key) DO UPDATE"
                    " SET acked_at=excluded.acked_at",
                    (k, int(self.ctx.clock())))
        return self._send(200, {"ok": True})

    @post("/api/order-control")
    def _post_order_control(self, body):
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
        return self._send(200, claire_api.post(
            self.ctx, "/internal/order", req, timeout=60,
            what="order change"))

    @post("/api/trade-retry")
    def _post_trade_retry(self, body):
        # money path: re-checked locally, like thesis-action
        if not self._paired():
            raise HTTPError(403, "dashboard not paired")
        wi = str(body.get("work_item_id") or "")
        try:
            if body.get("entry"):
                # the human edited the levels: amend the
                # trade in claire-api (the graph owns it)
                self._amend(wi, body)
            out = approvals.retry_failed(
                self.ctx.conn, wi, self.ctx.resume_post,
                confirm_live=body.get("confirm_live"),
                changes={k: body.get(k) for k in (
                    "size_base", "qty", "trail_pct", "order_type",
                    "broker")})
            return self._send(200, out)
        except approvals.AuthError as e:
            return self._send(e.code, {"error": e.message})

    @post("/api/trade-cancel")
    def _post_trade_cancel(self, body):
        # money path: re-checked locally, like thesis-action
        if not self._paired():
            raise HTTPError(403, "dashboard not paired")
        wi = str(body.get("work_item_id") or "")
        try:
            out = approvals.cancel_failed(self.ctx.conn, wi,
                                          clock=self.ctx.clock)
            return self._send(200, out)
        except approvals.AuthError as e:
            return self._send(e.code, {"error": e.message})

    @post("/api/trade-check")
    def _post_trade_check(self, body):
        return self._send(200, self._trade_check(body))

    @post("/api/saxo-login-start")
    def _post_saxo_login_start(self, body):
        return self._saxo_login_start(body)

    @post("/api/maintenance-run")
    def _post_maintenance_run(self, body):
        # starts a run in claire-api (it owns the maintainer and
        # the AI agent); the page polls /api/maintenance
        if not self._paired():
            raise HTTPError(403, "dashboard not paired")
        return self._send(200, claire_api.post(
            self.ctx, "/internal/maintenance", timeout=10,
            what="maintenance run"))

    @post("/api/live-arm")
    def _post_live_arm(self, body):
        # ARMING from the dashboard (§22.1): paired key + the
        # typed phrase + a browser on THIS PC (loopback). A LAN
        # device can disarm but never arm. Locks 1 (env flag) and
        # 3 (per-trade LIVE approval) are untouched by this.
        if not self._paired():
            raise HTTPError(403, "dashboard not paired")
        if not self._from_this_pc():
            raise HTTPError(403, "live trading can only be armed "
                            "from a browser on the desk's own PC")
        from .. import live_trading
        try:
            live_trading.arm(
                self.ctx.conn, self.ctx.env or {}, actor="dashboard:this-pc",
                phrase=str(body.get("phrase") or "").strip(),
                clock=self.ctx.clock)
        except live_trading.LiveTradingBlocked as e:
            return self._send(409, {"error": str(e)})
        return self._send(200, {"ok": True,
                                **self._live_status()})

    @post("/api/live-limits")
    def _post_live_limits(self, body):
        if not self._paired():
            raise HTTPError(403, "dashboard not paired")
        from .. import live_trading
        new = body.get("limits")
        if not isinstance(new, dict) or not new:
            return self._send(400, {"error": "limits: {name: "
                                    "value, ...} required"})
        try:
            live_trading.set_limits(
                self.ctx.conn, new, actor="dashboard ("
                f"{self.client_address[0]})", clock=self.ctx.clock)
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        return self._send(200, {"ok": True,
                                **self._live_status()})

    @post("/api/live-disarm")
    def _post_live_disarm(self, body):
        # DISARM is always one click away, from any paired
        # device (§22). Arming: /api/live-arm (this PC only).
        if not self._paired():
            raise HTTPError(403, "dashboard not paired")
        from .. import live_trading
        changed = live_trading.disarm(
            self.ctx.conn, actor="dashboard",
            reason=str(body.get("reason") or "disarmed from the "
                       "dashboard")[:200], clock=self.ctx.clock)
        return self._send(200, {"ok": True, "changed": changed,
                                **self._live_status()})

    @post("/api/thesis-action")
    def _post_thesis_action(self, body):
        # money path: re-checked locally on purpose, so moving or
        # refactoring the global gate above can never open it
        if not self._paired():
            raise HTTPError(403, "dashboard not paired")
        try:
            out = approvals.thesis_action(self.ctx.conn, body, self.ctx.resume_post,
                                          clock=self.ctx.clock)
            return self._send(200, out)
        except approvals.AuthError as e:
            return self._send(e.code, {"error": e.message})

    @post("/api/override")
    def _post_override(self, body):
        # overturning the desk is a money-path action: same local
        # re-check as thesis-action, on top of the global gate
        if not self._paired():
            raise HTTPError(403, "dashboard not paired")
        return self._send(200, claire_api.post(
            self.ctx, "/internal/override",
            {**body, "actor": "human"}, what="override"))

    @post("/api/telegram-decision")
    def _post_telegram_decision(self, body):
        # money path (Telegram buttons, relayed by Bob's bot):
        # key re-checked locally like the approval route; the
        # approver's Telegram user id is checked again inside
        if not self._paired():
            raise HTTPError(403, "dashboard not paired")
        from .. import telegram_approvals
        code, out = telegram_approvals.handle_decision(
            self.ctx.conn, self.ctx.market, body, self.ctx.resume_post, env=self.ctx.env or {},
            clock=self.ctx.clock)
        return self._send(code, out)

    @post("/api/price-triggers")
    def _post_price_triggers(self, body):
        # a trigger can become a trade — and in auto mode a
        # pre-approved one — so it is a money-path action: the
        # key is re-checked here, like thesis-action
        if not self._paired():
            raise HTTPError(403, "dashboard not paired")
        from .. import price_triggers
        who = ("human via the paired dashboard ("
               f"{self.client_address[0]})")
        try:
            if body.get("replace") is not None:
                out = price_triggers.replace(
                    self.ctx.conn, body.get("replace"),
                    {k: v for k, v in body.items()
                     if k != "replace"},
                    env=self.ctx.env or {}, clock=self.ctx.clock, created_by=who)
            else:
                out = price_triggers.create(
                    self.ctx.conn, body, env=self.ctx.env or {}, clock=self.ctx.clock,
                    created_by=who)
        except price_triggers.TriggerError as e:
            return self._send(e.code, {"error": e.message})
        return self._send(200, out)

    @post("/api/position-floor")
    def _post_position_floor(self, body):
        # change a HELD position's trailing alert % (Portfolio →
        # Edit). An alert, not an order: breaching it
        # only starts a sell review that needs approval. The
        # best price since buying is kept.
        if not self._paired():
            raise HTTPError(403, "dashboard not paired")
        return self._send(200, self._position_floor(body))

    @post("/api/position-levels")
    def _post_position_levels(self, body):
        # move a HELD position's stop-loss / take-profit — resting
        # orders that sell by themselves, so a money-path action:
        # the key is re-checked here, like thesis-action
        if not self._paired():
            raise HTTPError(403, "dashboard not paired")
        return self._send(200, self._position_levels(body))

    @post("/api/manual-trade")
    def _post_manual_trade(self, body):
        # a trade you start (the Sell button) is a money-path
        # action: same local re-check as thesis-action. Named so
        # it does not share the /api/override prefix.
        if not self._paired():
            raise HTTPError(403, "dashboard not paired")
        return self._manual_trade(body)

    @post("/api/run")
    def _post_run(self, body):
        return self._start_run(body)

    @post("/api/agent")
    def _post_agent(self, body):
        return self._assign_agent(body)

    @post("/api/provider-test")
    def _post_provider_test(self, body):
        pid = body.get("provider_id")
        if not isinstance(pid, str) or self.ctx.conn.execute(
                "SELECT 1 FROM providers WHERE id=?",
                (pid,)).fetchone() is None:
            raise HTTPError(400, "unknown provider_id")
        # a row written before the allowlist existed must not get
        # its (possibly secret) key sent anywhere by a probe
        ref = self.ctx.conn.execute("SELECT api_key_ref FROM providers"
                           " WHERE id=?", (pid,)).fetchone()[0]
        if ref and ref not in PROVIDER_KEY_REFS:
            raise HTTPError(400, f"provider {pid} has a key ref "
                            "outside the allowlist — re-save it")
        ok = provider_health.probe(self.ctx.conn, pid)
        latest = provider_health.latest(self.ctx.conn).get(pid)
        return self._send(200, dict(latest) if latest else
                          {"ok": ok})

    @post("/api/schedule")
    def _post_schedule(self, body):
        try:
            job = body.get("job")
            row = self.ctx.conn.execute(
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
            scheduler.update(self.ctx.conn, job, enabled=enabled,
                             spec_patch=patch or None)
            return self._send(200, {"ok": True})
        except (KeyError, ValueError) as e:
            return self._send(400, {"error": str(e)})

    @post("/api/provider")
    def _post_provider(self, body):
        return self._upsert_provider(body)

    @post("/api/mcp")
    def _post_mcp(self, body):
        return self._upsert_mcp(body)

    @post("/api/account-action")
    def _post_account_action(self, body):
        return self._account_action(body)

    @post("/api/watchlist")
    def _post_watchlist(self, body):
        t = str(body.get("ticker", "")).upper().strip()
        ex = str(body.get("exchange", "")).upper().strip()
        from ..tools.market import desk_ticker
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
            self.ctx.conn.execute("DELETE FROM watchlist WHERE ticker=?"
                         " AND exchange=?", (t, ex))
        else:
            self.ctx.conn.execute(
                "INSERT OR REPLACE INTO watchlist (ticker,"
                " exchange, note, added_at) VALUES (?,?,?,?)",
                (t, ex, note, int(self.ctx.clock())))
        return self._send(200, {"ok": True})

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
            from .. import price_triggers
            try:
                out = price_triggers.cancel(self.ctx.conn, q.get("id"),
                                            actor="human", clock=self.ctx.clock)
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
            with httpx.stream("POST", f"{self.ctx.api_base}{upstream}", json=body,
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
        with self.ctx.sse_lock:
            # read from the module each time, so the limit can be changed
            if len(self.ctx.sse_clients) >= common.SSE_MAX_CLIENTS:
                return self._send(503, {"error": "too many live streams"})
            self.ctx.sse_clients.append(q)
        # each stream is a thread: bound its life. EventSource reconnects
        # on its own, so recycling it is invisible to the page.
        deadline = time.monotonic() + self.ctx.sse_max_age
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            # a stream has no length: it ends when the connection does
            self.send_header("Connection", "close")
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
            with self.ctx.sse_lock:
                if q in self.ctx.sse_clients:
                    self.ctx.sse_clients.remove(q)

    def _from_this_pc(self):
        """The browser is on this machine (loopback), not the LAN."""
        ip = (self.client_address or ("",))[0]
        return ip in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def _check_symbol(self, ticker, exchange):
        # the Desk's own validator, so the dashboards refuse exactly what
        # start_run would (imported lazily: it pulls in the graph)
        from ..api.desk import validate_symbol
        try:
            validate_symbol(ticker, exchange)
        except ValueError as e:
            raise HTTPError(400, str(e)) from None
