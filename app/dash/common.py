"""What the dashboard server's pieces share: constants, HTTPError, and
the small pure helpers. Moved out of app/dashboards.py unchanged
(REVIEW-2026-11-11 §3.2); dashboards.py re-exports every name."""
import logging
import re
import socket
from datetime import datetime
from datetime import timezone
from pathlib import Path


from .. import sessions
from ..logsetup import swallowed

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
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
        from ..tools import saxo_live
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
        from .. import alerts
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
