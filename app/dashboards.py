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
import json
import logging
import threading
import time
from http.server import ThreadingHTTPServer

import httpx

from . import approvals, scheduler, sessions
# This file used to hold the whole server: 3,500 lines, 2,775 of them one
# function. What it defined above create_server now lives in app/dash/common.py
# (and app/levels.py, app/accounts.py, app/dash/usage.py); the request handler
# is app/dash/handler.py with its views and actions beside it
# (REVIEW-2026-11-11 §3.2). Those names are re-exported here because tests and
# other modules import them from `app.dashboards`.
from .accounts import (_approval_account, account_rank,  # noqa: F401
                       ordered_accounts, sim_label, venue_adapter)
from .dash.common import (HTTPError, LOOPBACK_HOSTS, MAX_BODY,  # noqa: F401
                          PAGES, PIPELINE_EDGES, PROVIDER_KINDS, ROOT, SAXO_CALLBACK, SSE_MAX_AGE,
                          SSE_MAX_CLIENTS, STATIC, WEB, _CCY_RE, _ID_RE,
                          _MODEL_RE, _validate_schedule_patch,
                          background_loops, default_allowed_hosts, host_only,
                          session_facts)
from .dash.usage import (BOB_USAGE_FILE, USAGE_AGENT_LABELS,  # noqa: F401
                         USAGE_SOURCE_LABELS, bob_usage, usage_report,
                         usage_source)
from .levels import LEVEL_KEYS, _levels, _range, quote_units  # noqa: F401
from .logsetup import swallowed
from .providers import health as provider_health  # noqa: F401 — tests patch it
from .providers.registry import PROVIDER_KEY_REFS  # noqa: F401
from .tools.brokers import seed_mcps
from .dash.ctx import Ctx
from .dash.handler import Handler

log = logging.getLogger(__name__)


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

    ctx = Ctx(conn=conn, repo=repo, market=market, api_base=api_base,
              secret=secret, dash_key=dash_key, narratives=narratives,
              clock=clock, env=env, cal=cal, resume_post=resume_post,
              sse_clients=sse_clients, sse_lock=sse_lock,
              sse_max_age=sse_max_age, host_allowed=host_allowed,
              api_ok=api_ok, cal_or_default=cal_or_default)

    class BoundHandler(Handler):
        """This server's handler: the shared Handler plus this server's
        Ctx. A subclass per server, so two servers in one process (tests)
        never see each other's."""
    BoundHandler.ctx = ctx
    return Server((bind, port), BoundHandler)


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
