"""Trade approvals over Telegram (pusher + decision handler).

PUSH (a background thread in the dashboards process, `run_pusher`): every
~20 s, each work item that reaches the approval gate (state
'awaiting_approval') — and each approved trade the venue REFUSED and that is
parked for the human (approvals.failed_approvals, not auto-retried) — is sent
once per state to the approver's private chat: a chart photo, then a detailed
HTML message with inline buttons. Sent items are remembered in
`telegram_notices` (created here, IF NOT EXISTS). When an item leaves that
state by any route (dashboard, expiry, execution) its message is edited to
say so and its buttons are removed.

DECIDE (`handle_decision`, behind the keyed POST /api/telegram-decision): Bob's
bot (the only getUpdates poller on the token) relays each button tap / typed
LIVE reply here. Rules:

* callback_data never carries the approval token (Telegram's servers see
  it): it is "cl:<notice id>:<op>[:<arg>]"; the token is read from desk.db.
* Only the approver's Telegram USER id may decide (TELEGRAM_APPROVER_ID, else
  the positive id in TELEGRAM_CHAT_ID) — being in an allowed chat is not
  enough. Checked here as well as in the bot.
* The tap must come from the very chat + message the notice was sent as.
* Two steps: a size/choice only shows a confirmation; only ✔ (carrying a
  one-time nonce) submits. A LIVE (real-money) account additionally needs
  the reply LIVE typed within 2 minutes, sent on as confirm_live="LIVE";
  approvals.thesis_action / retry_failed stay the authoritative checks.
* Stale buttons (item decided elsewhere, expired, changed) answer "already
  decided" and the message is edited to show the outcome.
* Every accepted decision is written to the work item's events (actor
  "human", payload channel "telegram") from inside the resume hook — i.e.
  only once the approval gate has accepted it, and BEFORE claire-api appends
  its own outcome rows. Never after: approvals.last_event() must keep
  seeing 'resume_failed' last on a parked refusal, or the custodian would
  auto-resend a trade the venue turned down.
"""
import hashlib
import hmac
import json
import math
import secrets
import socket
import threading
import time
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from . import alerts, approvals
import logging

log = logging.getLogger(__name__)

SGT = ZoneInfo("Asia/Singapore")
CB_PREFIX = "cl"
DEFAULT_SIZES = (500, 1000, 2000)
DEFAULT_TRAIL_PCT = 8
LIVE_WINDOW_S = 120
# A price trigger in "auto" mode creates its card and approves it itself a
# moment later (a 'pre_approved' event, then thesis_action). Cards from a
# trigger therefore wait this long before being pushed, and are re-checked
# right before sending — buttons for a trade already being placed would be
# worse than none.
TRIGGER_GRACE_S = 45
MAX_SEND_ATTEMPTS = 3
TG_LIMIT = 4000                      # Telegram's hard limit is 4096

_LOCK = threading.RLock()            # one decision / edit at a time

TABLE_SQL = """
CREATE TABLE IF NOT EXISTS telegram_notices (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  work_item_id TEXT NOT NULL,
  state_key TEXT NOT NULL,           -- 'approval' | 'failed:<failed_at ts>'
  chat_id TEXT, message_id INTEGER, photo_message_id INTEGER,
  status TEXT NOT NULL,              -- sending | sent | error | closed
  attempts INTEGER NOT NULL DEFAULT 0,
  base_html TEXT,                    -- the message as first sent
  options_json TEXT,                 -- accounts, sizes, selection, pending
  thesis_hash TEXT,
  outcome TEXT,
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
  UNIQUE(work_item_id, state_key)
)"""


def ensure_table(conn):
    conn.execute(TABLE_SQL)


# ── Telegram transport (injectable for tests) ─────────────────────────────
class Telegram:
    def __init__(self, token, client=None):
        self.token, self.client = token, client

    def photo(self, chat_id, png, caption=None):
        return alerts.send_photo(self.token, chat_id, png, caption=caption,
                                 client=self.client)

    def message(self, chat_id, html, keyboard=None):
        return alerts.send_message(self.token, chat_id, html,
                                   keyboard=keyboard, client=self.client)

    def edit(self, chat_id, message_id, html, keyboard=None):
        return alerts.edit_message(self.token, chat_id, message_id, html,
                                   keyboard=keyboard, client=self.client)


# ── small helpers ─────────────────────────────────────────────────────────
def h(x):
    """HTML-escape anything for Telegram's HTML parse mode."""
    return escape("" if x is None else str(x), quote=True)


def num(x, d=2):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(x):
        return "—"
    return f"{x:,.{d}f}"


def qty_str(q):
    return f"{q:,.0f}" if float(q) == int(q) else f"{q:,.1f}"


def pct(a, b):
    """% move from b to a."""
    try:
        return (float(a) - float(b)) / float(b) * 100
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def spct(p):
    return "—" if p is None else f"{p:+.1f}%"


def sgt(ts):
    return datetime.fromtimestamp(int(ts), tz=SGT).strftime("%a %d %b %H:%M SGT")


def left(seconds):
    s = max(int(seconds), 0)
    if s >= 3600:
        return f"{s // 3600}h {s % 3600 // 60:02d}m"
    return f"{s // 60}m"


def conviction_word(c):
    if not isinstance(c, (int, float)):
        return "unknown"
    return "low" if c < 0.4 else "medium" if c < 0.7 else "high"


def thesis_hash(thesis_json):
    return hashlib.sha256((thesis_json or "").encode()).hexdigest()[:16]


def origin(kind, trigger):
    """What started the run, in words (mirrors dashboards.usage_source)."""
    t = (trigger or "").strip()
    tl = t.lower()
    reason = t.split(": ", 1)[1] if ": " in t else ""
    if tl.startswith("manual ") and " from trigger" in tl:
        return "Your price trigger" + (f" — {reason}" if reason else "")
    if kind == "manual" or tl.startswith("manual "):
        src = t.split(" from ", 1)[1] if " from " in t else ""
        return "Manual trade you started" + (f" from {src}" if src else "")
    if kind == "override" or tl.startswith("human override"):
        return "Your override of a PASS" + (f" ({t[len('human override of '):]})"
                                            if tl.startswith("human override of ")
                                            else "")
    if kind == "sell_review" or tl.startswith("watcher:"):
        return "Exit review (price watcher)" + (f" — {reason}" if reason else "")
    if tl.startswith("event scan"):
        return "Volume scan" + (f" — {reason}" if reason else "")
    if tl.startswith("deterministic fallback"):
        return "Daily screen (most-active fallback)"
    if kind == "screen" or tl.startswith("screener"):
        return "Daily screen" + (f" — {reason}" if reason else "")
    if not t:
        return "Manual run (you, Bob or the dashboard)"
    return f"Other — {t}"


def _pos(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) and v > 0 else None


def pre_approved(conn, wi):
    """The owner's standing pre-approval (an auto-mode price trigger) is
    being exercised on this card: never offer buttons for it."""
    return conn.execute("SELECT 1 FROM events WHERE item_id=? AND"
                        " to_state='pre_approved' LIMIT 1",
                        (wi,)).fetchone() is not None


def trigger_row(conn, wi):
    try:
        return conn.execute("SELECT * FROM price_triggers WHERE"
                            " work_item_id=? ORDER BY id DESC LIMIT 1",
                            (wi,)).fetchone()
    except Exception:                          # noqa: BLE001 — older desk.db
        return None


def is_trigger_card(conn, row):
    t = (row["trigger"] or "").lower()
    return (t.startswith("manual ") and " from trigger" in t) or \
        trigger_row(conn, row["id"]) is not None


def trigger_rule(conn, wi):
    """'Buy 50 STVN when price ≤ 5.00 (limit 5.00) — fired at 4.98' for a
    card a price trigger created, else None."""
    t = trigger_row(conn, wi)
    if t is None:
        return None
    try:
        from .price_triggers import describe, fmt_price
        rule = describe(t)
        if t["fired_price"] is not None:
            rule += f" — fired at {fmt_price(t['fired_price'])}"
    except Exception:                          # noqa: BLE001 — shape drift
        rule = f"trigger #{t['id']}"
    return {"id": t["id"], "rule": rule, "mode": t["mode"],
            "note": t["note"]}


def suggestion(row):
    """work_items.suggested_json — what the card should prefill for a manual
    trade (qty, size, account, trailing floor). Being added concurrently by
    another change, so read defensively: missing column, NULL, bad JSON or
    odd keys all mean "no suggestion". Returns {size_base, qty, broker,
    account_id, trail_pct} with only the usable keys present."""
    try:
        raw = row["suggested_json"]
    except (IndexError, KeyError):
        return {}
    if not raw:
        return {}
    try:
        s = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if isinstance(s, (int, float)):
        s = {"size_base": s}
    if not isinstance(s, dict):
        return {}
    out = {}
    size = _pos(next((s[k] for k in ("size_base", "size", "amount",
                                     "notional_base") if s.get(k) is not None),
                     None))
    if size:
        out["size_base"] = size
    qty = _pos(s.get("qty") if s.get("qty") is not None else s.get("shares"))
    if qty:
        out["qty"] = qty
    for k in ("broker", "account_id", "account"):
        if isinstance(s.get(k), str) and s[k]:
            out["account_id" if k == "account" else k] = s[k]
    trail = _pos(s.get("trail_pct"))
    if trail and trail <= 90:
        out["trail_pct"] = trail
    return out


def suggested_size(row):
    return suggestion(row).get("size_base")


def dashboard_url(env):
    """A link that works from a phone: CLAIRE_DASHBOARD_URL if set, else the
    LAN address the dashboards are bound to (localhost is useless there)."""
    explicit = (env.get("CLAIRE_DASHBOARD_URL") or "").strip().rstrip("/")
    if explicit:
        return explicit
    bind = (env.get("CLAIRE_DASHBOARD_BIND") or "127.0.0.1").strip()
    host = bind
    if bind in ("0.0.0.0", "::", ""):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))          # no packet sent; reads route
            host = s.getsockname()[0]
            s.close()
        except OSError:
            host = socket.gethostname()
    return f"http://{host}:7787"


# ── facts ─────────────────────────────────────────────────────────────────
def accounts(conn, env, live_armed):
    """Selectable accounts, default first: Saxo SIM when SIM execution is on,
    else the first paper account. The real-money account only when armed."""
    sim_on = (env.get("SAXO_SIM_EXECUTION") or "").lower() == "enabled"
    out = []
    from .accounting.repo import Repo
    repo = Repo(conn)
    for a in conn.execute("SELECT * FROM broker_accounts ORDER BY rowid"):
        live = a["environment"] == "live"
        if live and not live_armed:
            continue
        if live:
            label = f"Saxo LIVE {env.get('SAXO_LIVE_ACCOUNT_ID') or ''}".strip()
            short, kind = "⚠ LIVE", "REAL MONEY"
        elif a["broker"] == "saxo" and sim_on:
            label = f"Saxo SIM {env.get('SAXO_SIM_ACCOUNT_ID') or ''}".strip()
            short, kind = "Saxo SIM", "paper"
        else:
            label = f"{a['broker']} built-in sim"
            short, kind = f"{a['broker']} sim", "paper"
        out.append({"id": a["id"], "broker": a["broker"], "label": label,
                    "short": short, "kind": kind, "live": live,
                    "ccy": a["base_currency"],
                    "whole": live or (a["broker"] == "saxo" and sim_on),
                    "cash": repo.available_cash(a["id"]) / 1e6})
    default = next((i for i, a in enumerate(out)
                    if a["broker"] == "saxo" and not a["live"] and sim_on),
                   next((i for i, a in enumerate(out) if not a["live"]), 0))
    if out:
        out.insert(0, out.pop(default))
    return out


def holdings(conn, ticker):
    rows = conn.execute(
        "SELECT l.account_id, SUM(CASE WHEN l.qty_opened < 0 THEN"
        " -l.qty_remaining ELSE l.qty_remaining END)/1e6 AS q"
        " FROM lots l JOIN instruments i ON i.id = l.instrument_id"
        " WHERE i.ticker=? AND l.qty_remaining > 0"
        " GROUP BY l.account_id", (ticker,)).fetchall()
    return {r["account_id"]: float(r["q"]) for r in rows if r["q"]}


def reports_for(conn, wi_row):
    ids = [wi_row["id"]]
    for k in ("override_of", "prior_run"):
        try:
            if wi_row[k]:
                ids.insert(0, wi_row[k])
        except (IndexError, KeyError):
            pass
    out = {}
    for wid in ids:                      # later (own) reports win
        for r in conn.execute("SELECT agent_id, payload FROM agent_reports"
                              " WHERE work_item_id=? ORDER BY id", (wid,)):
            try:
                out[r["agent_id"]] = json.loads(r["payload"])
            except ValueError:
                pass
    return out


def shares_for(size_base, price_base, whole, lot=1):
    if not price_base or price_base <= 0:
        return 0.0
    raw = size_base / price_base
    if lot > 1:
        return float(int(raw / lot) * lot)
    if whole:
        return float(math.floor(raw))
    return math.floor(raw * 10) / 10


def gather(conn, market, wi_row, env, *, clock=time.time, phase="approval",
           failed=None):
    """Everything the message and keyboard need, with market data fetched
    once. Market failures degrade to missing numbers, never to no message."""
    from . import live_trading
    from .tools.market import yahoo_symbol
    th = json.loads(wi_row["thesis_json"] or "{}")
    inst = conn.execute("SELECT * FROM instruments WHERE ticker=?"
                        " ORDER BY (name IS NULL), id LIMIT 1",
                        (wi_row["ticker"],)).fetchone()
    exchange = inst["exchange"] if inst else "NASDAQ"
    ccy = th.get("currency") or (inst["currency"] if inst else "USD")
    sym = yahoo_symbol(wi_row["ticker"], exchange)
    f = {"wi": wi_row["id"], "ticker": wi_row["ticker"], "exchange": exchange,
         "name": inst["name"] if inst else None, "symbol": sym,
         "kind": wi_row["kind"], "trigger": wi_row["trigger"],
         "origin": origin(wi_row["kind"], wi_row["trigger"]),
         "thesis": th, "side": th.get("direction") or "?", "ccy": ccy,
         "phase": phase, "expires_at": wi_row["expires_at"],
         "now": int(clock()), "lot": 100 if exchange == "SGX" else 1,
         "suggestion": suggestion(wi_row), "failed": failed}
    f["suggested"] = f["suggestion"].get("size_base")
    f["price_trigger"] = trigger_rule(conn, wi_row["id"])
    f["trail_pct"] = f["suggestion"].get("trail_pct") or DEFAULT_TRAIL_PCT
    try:
        q = market.quote(sym)
        f["price"] = float(q["price"]) if q.get("price") is not None else None
        chg = q.get("change_pct")
        if chg is None and q.get("previous_close"):
            chg = pct(f["price"], q["previous_close"])
        f["change_pct"] = float(chg) if chg is not None else None
    except Exception:                          # noqa: BLE001
        f["price"], f["change_pct"] = None, None
    try:
        f["chart"] = market.chart(sym, "6mo", "1d")
    except Exception:                          # noqa: BLE001
        f["chart"] = None
    live = live_trading.status(conn, env)
    f["live_armed"] = bool(live["armed"])
    f["accounts"] = accounts(conn, env, f["live_armed"])
    f["fx"] = {}
    for a in f["accounts"]:
        if a["ccy"] not in f["fx"]:
            try:
                f["fx"][a["ccy"]] = float(market.fx(ccy, a["ccy"]))
            except Exception:                  # noqa: BLE001
                f["fx"][a["ccy"]] = None
    f["held"] = holdings(conn, wi_row["ticker"])
    f["reports"] = reports_for(conn, wi_row)
    return f


def ref_price(f):
    th = f["thesis"]
    return th.get("entry_high") or th.get("entry_low") or f.get("price")


# ── message ───────────────────────────────────────────────────────────────
SIGNAL_DOT = {"bullish": "🟢", "bearish": "🔴", "neutral": "⚪"}


def _one_line(s, n=150):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def build_message(f, opts, base_url):
    th, side = f["thesis"], f["side"]
    head_dot = "🟢" if side == "buy" else "🔴" if side == "sell" else "⚪"
    lines = []
    if f["phase"] == "failed":
        lines.append(f"⚠️ <b>REFUSED — {h(side.upper())} {h(f['ticker'])}</b>"
                     f" · {h(f['exchange'])}")
    else:
        lines.append(f"{head_dot} <b>{h(side.upper())} {h(f['ticker'])}</b> ·"
                     f" {h(f['exchange'])}"
                     + (f" · {h(_one_line(f['name'], 40))}" if f["name"] else ""))
    lines.append(f"<i>Started by:</i> {h(_one_line(f['origin'], 160))}")
    if f["phase"] == "failed":
        fl = f["failed"]
        lines.append(f"<i>Retry window:</i> {h(left(fl['expires_in_s']))} left"
                     f" (until {h(sgt(fl['expires_at']))})")
    elif f["expires_at"]:
        lines.append(f"<i>Approval expires:</i> {h(sgt(f['expires_at']))}"
                     f" ({h(left(f['expires_at'] - f['now']))} left)")
    pt = f.get("price_trigger")
    if pt:
        lines.append(f"🎯 <b>Your trigger #{h(pt['id'])}:</b> {h(pt['rule'])}"
                     + (" · it asks before trading" if pt["mode"] == "ask"
                        else f" · mode {h(pt['mode'])}"))
        if pt.get("note"):
            lines.append(f"<i>Note:</i> {h(_one_line(pt['note'], 160))}")
    if f["kind"] == "override":
        lines.append("⚠ <b>Human override</b> — the desk said PASS; the levels"
                     " are yours. Nothing re-analysed the position.")
    lines.append("")

    if f["phase"] == "failed":
        fl = f["failed"]
        ap = fl.get("approval") or {}
        lines.append(f"<b>Why it was refused:</b> {h(plain_refusal(fl['error']))}")
        lines.append(f"<i>Venue said:</i> <code>{h(_one_line(fl['error'], 300))}"
                     "</code>")
        what = approved_what(f, ap)
        lines.append(f"<i>You approved:</i> {h(what)} on {h(ap.get('broker'))},"
                     f" trailing floor {h(ap.get('trail_pct') or '—')}% ·"
                     f" attempts {h(fl['attempts'])}")
        lines.append("")

    conv = th.get("conviction")
    if f["kind"] == "manual":
        lines.append(f"<b>Order:</b> {h(side.upper())} — your manual trade;"
                     " the desk did not analyse it")
    else:
        lines.append(f"<b>Verdict:</b> {h(side.upper())} · conviction "
                     f"{h(conviction_word(conv))} ({h(num(conv))})")
    price = f.get("price")
    lines.append(f"<b>Price:</b> {h(num(price))} {h(f['ccy'])}"
                 + (f" ({h(spct(f['change_pct']))} today)"
                    if f.get("change_pct") is not None else ""))
    ref = ref_price(f)
    el, eh = th.get("entry_low"), th.get("entry_high")
    if el or eh:
        rng = num(el) if el == eh or not eh else f"{num(el)}–{num(eh)}"
        lines.append(f"<b>Entry:</b> {h(rng)} · limit {h(num(eh or el))}"
                     + (f" ({h(spct(pct(eh or el, price)))} vs last)"
                        if price else ""))
    stop, tgt = th.get("stop_loss"), th.get("take_profit")
    if stop or tgt:
        parts = []
        if stop:
            parts.append(f"<b>Stop:</b> {h(num(stop))} ({h(spct(pct(stop, ref)))})")
        if tgt:
            parts.append(f"<b>Target:</b> {h(num(tgt))} ({h(spct(pct(tgt, ref)))})")
        lines.append(" · ".join(parts))
    if stop and tgt and ref:
        risk = (ref - stop) if side == "buy" else (stop - ref)
        reward = (tgt - ref) if side == "buy" else (ref - tgt)
        if risk > 0:
            lines.append(f"<b>Reward:risk</b> {h(num(reward / risk, 1))} : 1")

    conds = [c for c in (th.get("conditions") or []) if isinstance(c, str)]
    if conds:
        lines.append("")
        lines.append("<b>Key reasons &amp; conditions</b>")
        for c in conds[:5]:
            lines.append(f"• {h(_one_line(c, 220))}")

    reps = f["reports"]
    an = [(a, reps[a]) for a in ("fundamental", "technical", "news")
          if a in reps]
    if an:
        lines.append("")
        lines.append("<b>Analysts</b>")
        for a, d in an:
            sig = d.get("signal") or "?"
            lines.append(f"{SIGNAL_DOT.get(sig, '⚪')} <b>{h(a)}</b> {h(sig)}"
                         f" {h(num(d.get('conviction')))} — "
                         f"{h(_one_line(d.get('summary'), 150))}")
    if "bull" in reps or "bear" in reps:
        lines.append(f"<b>Debate:</b> bull {h(num(reps.get('bull', {}).get('conviction')))}"
                     f" vs bear {h(num(reps.get('bear', {}).get('conviction')))}")

    lines.append("")
    acct = selected_account(f, opts)
    held_total = sum(f["held"].values())
    if f["held"]:
        hs = ", ".join(f"{qty_str(q)} sh in {k}" for k, q in f["held"].items())
        lines.append(f"<b>Desk holds:</b> {h(hs)}")
    else:
        lines.append("<b>Desk holds:</b> none")
    if acct:
        lines.append(f"<b>Account:</b> {h(acct['label'])} — {h(acct['kind'])}"
                     f" · free cash {h(num(acct['cash'], 0))} {h(acct['ccy'])}")
    if f["phase"] == "approval" and acct:
        if side == "buy":
            sug = f.get("suggested")
            size = sug or 1000
            sh, cost = estimate(f, acct, size)
            lines.append(f"<b>{'Suggested size' if sug else 'Typical size'}:"
                         f"</b> {h(num(size, 0))} {h(acct['ccy'])} → "
                         f"{h(qty_str(sh))} sh ≈ {h(num(cost))} {h(acct['ccy'])}"
                         + (" (whole shares)" if acct["whole"] else ""))
            if not opts.get("sizes"):
                lines.append("⚠ No offered size fits this account's free cash"
                             " — top up or use the Approvals page.")
        elif side == "sell" and not held_total:
            lines.append("⚠ The desk holds none — this sell would open a SHORT."
                         " Decide that on the Approvals page.")
        lines.append(f"<i>Trailing floor {h(num(f['trail_pct'], 1))}% · pick below —"
                     " you confirm before anything is sent.</i>")
    lines.append(f"<i>As of {h(sgt(f['now']))} · {h(f['wi'])}</i>")
    return _fit("\n".join(lines))


def _fit(text):
    """Stay under Telegram's limit without cutting an HTML tag in half:
    drop whole lines from the analyst/conditions middle if needed."""
    if len(text) <= TG_LIMIT:
        return text
    lines = text.split("\n")
    while len("\n".join(lines)) > TG_LIMIT - 20 and len(lines) > 8:
        lines.pop(len(lines) // 2)
    return "\n".join(lines[:len(lines) // 2] + ["…"] + lines[len(lines) // 2:])


def approved_what(f, ap):
    """'1,000 USD' / '12 sh' — what the human approved on a refused trade."""
    if ap.get("size_base"):
        ccy = next((a["ccy"] for a in f.get("accounts") or []
                    if a["broker"] == ap.get("broker")), "")
        return f"{num(ap['size_base'], 0)} {ccy}".strip()
    return f"{qty_str(float(ap.get('qty') or 0))} sh"


def plain_refusal(err):
    e = (err or "").lower()
    if "lot size" in e or "whole multiple" in e:
        return "the share quantity was not a whole lot the venue accepts"
    if "price band" in e or "outside" in e and "entry" in e:
        return "the live price moved outside the approved entry range"
    if "cash" in e or "insufficient" in e or "buying power" in e:
        return "not enough free cash in the account"
    if "limit" in e and "live" in e:
        return "a real-money safety limit refused it"
    if "not armed" in e or "live trading" in e:
        return "live trading is not armed"
    if "market closed" in e or "session" in e:
        return "the market session would not take the order"
    if "rejected" in e:
        return "the broker rejected the order"
    return "the desk or broker refused the order (details below)"


def selected_account(f, opts):
    accts = opts.get("accounts") or f.get("accounts") or []
    i = opts.get("acct", 0)
    return accts[i] if 0 <= i < len(accts) else None


def estimate(f, acct, size_base):
    """(shares, est. cost in account ccy) for a buy of `size_base`."""
    ref = ref_price(f)
    rate = (f.get("fx") or {}).get(acct["ccy"])
    if not ref or not rate:
        return 0.0, 0.0
    px = float(ref) * rate
    sh = shares_for(float(size_base), px, acct["whole"], f.get("lot", 1))
    return sh, sh * px


def _num_or(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def sell_choices(f, acct):
    held = float(f["held"].get(acct["id"], 0))
    if held <= 0:
        return []
    half = math.floor(held / 2) if acct["whole"] else math.floor(held * 5) / 10
    out = [("All", held)]
    if half > 0 and half < held:
        out.append(("Half", float(half)))
    sq = (f.get("suggestion") or {}).get("qty")
    if sq and sq < held and sq not in (q for _, q in out) and (
            not acct["whole"] or sq == int(sq)):
        out.insert(0, ("Suggested", float(sq)))
    return out


def initial_account(f):
    """Index of the account preselected on the keyboard: the one a refused
    trade was approved on; else the suggestion's account; for a sell, the
    account holding the most shares; else the default (index 0)."""
    accts = f.get("accounts") or []

    def find(pred):
        return next((i for i, a in enumerate(accts) if pred(a)), None)
    if f.get("failed"):
        b = (f["failed"].get("approval") or {}).get("broker")
        i = find(lambda a: a["broker"] == b)
        if i is not None:
            return i
    sug = f.get("suggestion") or {}
    if sug.get("account_id") or sug.get("broker"):
        i = find(lambda a: a["id"] == sug.get("account_id")
                 or (not sug.get("account_id") and a["broker"] == sug["broker"]))
        if i is not None and not accts[i]["live"]:
            return i
    if f.get("side") == "sell" and f.get("held"):
        best = max(((q, i) for i, a in enumerate(accts)
                    for k, q in f["held"].items() if k == a["id"]),
                   default=None)
        if best:
            return best[1]
    return 0


def size_choices(f, acct):
    sug = f.get("suggested")
    sizes = [sug, round(sug / 2, 2)] if sug else list(DEFAULT_SIZES)
    return [s for s in sizes if s and s <= acct["cash"] + 1e-9] or []


# ── keyboard ──────────────────────────────────────────────────────────────
def cb(nid, op, arg=""):
    data = f"{CB_PREFIX}:{nid}:{op}" + (f":{arg}" if arg != "" else "")
    assert len(data.encode()) <= 64, data
    return data


def parse_callback(data):
    parts = str(data or "").split(":")
    if len(parts) < 3 or parts[0] != CB_PREFIX or not parts[1].isdigit():
        return None
    return int(parts[1]), parts[2], (parts[3] if len(parts) > 3 else "")


def build_keyboard(nid, f, opts, base_url):
    rows = []
    if f["phase"] == "failed":
        rows.append([{"text": "🔁 Retry same order", "callback_data": cb(nid, "rt")},
                     {"text": "✖ Cancel trade", "callback_data": cb(nid, "cx")}])
    else:
        acct = selected_account(f, opts)
        if acct:
            if f["side"] == "buy":
                row = []
                for i, s in enumerate(opts.get("sizes") or []):
                    sh, cost = estimate(f, acct, s)
                    row.append({"text": f"{num(s, 0)} {acct['ccy']} ≈{qty_str(sh)} sh",
                                "callback_data": cb(nid, "s", i)})
                if row:
                    rows.append(row)
            elif f["side"] == "sell":
                px = (_num_or(f["thesis"].get("entry_high")) or f.get("price")
                      or 0) * ((f.get("fx") or {}).get(acct["ccy"]) or 0)
                row = [{"text": f"{lab} ({qty_str(q)} sh" + (
                            f" ≈ {num(q * px, 0)} {acct['ccy']})" if px
                            else ")"),
                        "callback_data": cb(nid, "s", i)}
                       for i, (lab, q) in enumerate(sell_choices(f, acct))]
                if row:
                    rows.append(row)
        accts = opts.get("accounts") or []
        if len(accts) > 1:
            rows.append([{"text": ("✓ " if i == opts.get("acct", 0) else "")
                          + a["short"], "callback_data": cb(nid, "a", i)}
                         for i, a in enumerate(accts)])
        rows.append([{"text": "✖ Reject", "callback_data": cb(nid, "rj")}])
    rows.append([{"text": "Open Approvals ↗", "url": f"{base_url}/approvals"}])
    return rows


# ── notices table ─────────────────────────────────────────────────────────
def _notice(conn, nid):
    return conn.execute("SELECT * FROM telegram_notices WHERE id=?",
                        (nid,)).fetchone()


def _save_opts(conn, nid, opts, clock):
    conn.execute("UPDATE telegram_notices SET options_json=?, updated_at=?"
                 " WHERE id=?", (json.dumps(opts), int(clock()), nid))


def _close(conn, tg, n, outcome_html, clock, *, status="closed"):
    conn.execute("UPDATE telegram_notices SET status=?, outcome=?,"
                 " updated_at=? WHERE id=?",
                 (status, outcome_html, int(clock()), n["id"]))
    try:
        tg.edit(n["chat_id"], n["message_id"],
                _fit((n["base_html"] or "") + "\n\n" + outcome_html), None)
    except Exception as e:                     # noqa: BLE001 — best effort
        log.warning("telegram: edit failed for notice %s: %s", n["id"], e)


STATE_WORDS = {"approved": "approved", "executing": "approved — executing",
               "done": "executed", "rejected": "rejected",
               "expired": "expired", "failed": "failed",
               "running": "being re-analysed"}


def stale_reason(conn, n, wi, opts, clock):
    """None while the notice's buttons are still valid, else why not."""
    now = int(clock())
    if wi is None:
        return "the work item no longer exists"
    if n["state_key"] == "approval":
        if wi["state"] != "awaiting_approval":
            return f"already decided — {STATE_WORDS.get(wi['state'], wi['state'])}"
        if wi["token_state"] == "pending_resume" or \
                pre_approved(conn, wi["id"]):
            # authorized already (dashboard, or an auto trigger exercising
            # its pre-approval) and being handed to execution
            return "already approved — being placed"
        if wi["token_state"] != "minted":
            return "already decided"
        if wi["expires_at"] and now >= wi["expires_at"]:
            return "the approval expired"
        if n["thesis_hash"] and thesis_hash(wi["thesis_json"]) != n["thesis_hash"]:
            return "the trade was changed since this message — open Approvals"
        return None
    fl = next((x for x in approvals.failed_approvals(conn, clock=clock)
               if x["id"] == wi["id"]), None)
    if fl is None:
        return f"already handled — {STATE_WORDS.get(wi['state'], wi['state'])}"
    if f"failed:{fl['failed_at']}" != n["state_key"]:
        return "it failed again since — see the newer message"
    if fl["expires_in_s"] <= 0:
        return "the retry window has closed"
    return None


# ── push ──────────────────────────────────────────────────────────────────
def _claim(conn, wi, key, clock):
    """Take the right to send (wi, key). True if this caller should send."""
    now = int(clock())
    # already claimed (the usual case, every 20 s pass): look first — an
    # ignored INSERT still burns an AUTOINCREMENT id (1,660 ids for 16 rows)
    have = conn.execute("SELECT status FROM telegram_notices WHERE"
                        " work_item_id=? AND state_key=?", (wi, key)).fetchone()
    if have is not None and have["status"] != "error":
        return False
    cur = conn.execute(
        "INSERT OR IGNORE INTO telegram_notices (work_item_id, state_key,"
        " status, created_at, updated_at) VALUES (?,?,'sending',?,?)",
        (wi, key, now, now))
    if cur.rowcount == 1:
        return True
    cur = conn.execute(
        "UPDATE telegram_notices SET status='sending', updated_at=?"
        " WHERE work_item_id=? AND state_key=? AND status='error'"
        " AND attempts < ? AND updated_at < ?",
        (now, wi, key, MAX_SEND_ATTEMPTS, now - 60))
    return cur.rowcount == 1


class SkipNotice(Exception):
    """Nothing to send after all (the item moved on while gathering)."""


def send_notice(conn, market, tg, wi_row, key, chat_id, env, *, clock,
                base_url, failed=None, render=None):
    f = gather(conn, market, wi_row, env, clock=clock,
               phase="failed" if failed else "approval", failed=failed)
    opts = {"accounts": f["accounts"], "acct": 0, "pending": None}
    opts["acct"] = initial_account(f)
    if f["accounts"]:
        opts["sizes"] = size_choices(f, f["accounts"][opts["acct"]])
    nid = conn.execute("SELECT id FROM telegram_notices WHERE work_item_id=?"
                       " AND state_key=?", (wi_row["id"], key)).fetchone()[0]
    if not failed:
        # gather() took a while (market data): re-check that this is still a
        # card for the human before any message leaves
        cur = conn.execute("SELECT state FROM work_items WHERE id=?",
                           (wi_row["id"],)).fetchone()
        if cur is None or cur["state"] != "awaiting_approval" or \
                pre_approved(conn, wi_row["id"]):
            raise SkipNotice("no longer awaiting the human")
    html_msg = build_message(f, opts, base_url)
    photo_id = None
    if f.get("chart"):
        try:
            png = (render or _render)(f)
            cap = (f"<b>{h(f['side'].upper())} {h(f['ticker'])}</b> ·"
                   f" {h(f['exchange'])} — "
                   + ("refused trade, details below" if failed
                      else "awaiting your approval, details below"))
            photo_id = tg.photo(chat_id, png, cap)["message_id"]
        except Exception as e:                 # noqa: BLE001 — text still goes
            log.warning("telegram: chart for %s failed: %s", wi_row["id"], e)
    kb = build_keyboard(nid, f, opts, base_url)
    try:
        msg = tg.message(chat_id, html_msg, kb)
    except alerts.TelegramError as e:
        if "url" not in (e.description or "").lower():
            raise
        # a URL button Telegram will not take (e.g. a bare LAN address):
        # keep the decision buttons, put the link in the text instead
        kb = [r for r in kb if "url" not in r[0]]
        html_msg += f"\nApprovals: {h(base_url)}/approvals"
        msg = tg.message(chat_id, html_msg, kb)
    opts["facts"] = _facts_for_storage(f)
    conn.execute(
        "UPDATE telegram_notices SET status='sent', chat_id=?, message_id=?,"
        " photo_message_id=?, base_html=?, options_json=?, thesis_hash=?,"
        " updated_at=? WHERE id=?",
        (str(chat_id), msg["message_id"], photo_id, html_msg,
         json.dumps(opts), thesis_hash(wi_row["thesis_json"]), int(clock()),
         nid))
    return nid


def _facts_for_storage(f):
    """What the decision handler needs later (no chart, no reports)."""
    return {k: f[k] for k in ("wi", "ticker", "exchange", "side", "ccy",
                              "thesis", "price", "held", "fx", "lot",
                              "suggested", "suggestion", "trail_pct",
                              "phase", "failed", "now")
            if k in f}


def _render(f):
    from .chart_png import render_chart
    th = f["thesis"]
    lv = {"entry": th.get("entry_high") or th.get("entry_low"),
          "stop": th.get("stop_loss"), "target": th.get("take_profit"),
          "last": f.get("price")}
    sub = (f"6 months daily · {f['side'].upper()} · conviction "
           f"{num(th.get('conviction'))} ({conviction_word(th.get('conviction'))})")
    return render_chart(f["chart"], title=f"{f['ticker']} · {f['exchange']}",
                        subtitle=sub, levels=lv, currency=f["ccy"])


def push_once(conn, market, tg, chat_id, env, *, clock=time.time,
              base_url=None, render=None):
    """One pass: send new notices, close ones whose item moved on."""
    ensure_table(conn)
    base_url = base_url or dashboard_url(env)
    now = int(clock())
    sent = []
    todo = []
    for r in conn.execute("SELECT * FROM work_items WHERE"
                          " state='awaiting_approval' AND (expires_at IS NULL"
                          " OR expires_at > ?)", (now,)).fetchall():
        if pre_approved(conn, r["id"]):
            continue                   # an auto trigger is placing it
        if is_trigger_card(conn, r) and now < r["created_at"] + TRIGGER_GRACE_S:
            continue                   # give auto mode time to claim it
        todo.append((r, "approval", None))
    for fl in approvals.failed_approvals(conn, clock=clock):
        if fl["auto_retry"] or fl["expires_in_s"] <= 0:
            continue                   # transient: the custodian retries it
        r = conn.execute("SELECT * FROM work_items WHERE id=?",
                         (fl["id"],)).fetchone()
        todo.append((r, f"failed:{fl['failed_at']}", fl))
    for r, key, fl in todo:
        with _LOCK:
            if not _claim(conn, r["id"], key, clock):
                continue
        try:
            sent.append(send_notice(conn, market, tg, r, key, chat_id, env,
                                    clock=clock, base_url=base_url,
                                    failed=fl, render=render))
        except SkipNotice:
            conn.execute("DELETE FROM telegram_notices WHERE work_item_id=?"
                         " AND state_key=? AND status='sending'",
                         (r["id"], key))
        except Exception as e:                 # noqa: BLE001
            log.warning("telegram: notice for %s failed: %s", r["id"], e)
            conn.execute("UPDATE telegram_notices SET status='error',"
                         " attempts=attempts+1, updated_at=? WHERE"
                         " work_item_id=? AND state_key=?",
                         (int(clock()), r["id"], key))
    # close the ones whose buttons no longer mean anything
    for n in conn.execute("SELECT * FROM telegram_notices WHERE"
                          " status='sent'").fetchall():
        wi = conn.execute("SELECT * FROM work_items WHERE id=?",
                          (n["work_item_id"],)).fetchone()
        with _LOCK:
            n = _notice(conn, n["id"])
            if n["status"] != "sent":
                continue
            why = stale_reason(conn, n, wi, {}, clock)
            if why:
                _close(conn, tg, n, f"<b>Closed:</b> {h(why)}"
                       f" ({h(sgt(clock()))}).", clock)
    return sent


def run_pusher(conn, market, env, *, interval=20, clock=time.time):
    """Thread body for the dashboards process. Quiet when Telegram is not
    configured; re-reads ~/bob/telegram.env each pass."""
    warned = False
    while True:
        try:
            st = alerts.telegram_settings()
            if st["token"] and st["approver_id"]:
                push_once(conn, market, Telegram(st["token"]),
                          st["approver_id"], env, clock=clock)
                warned = False
            elif not warned:
                log.warning("telegram approvals: no bot token / approver id "
                            "in %s — not pushing", alerts.TELEGRAM_ENV)
                warned = True
        except Exception:                      # noqa: BLE001 — keep looping
            log.exception("telegram approvals pass failed")
        time.sleep(interval)


def start_pusher_thread(conn, market, env, *, interval=20):
    t = threading.Thread(target=run_pusher, args=(conn, market, env),
                         kwargs={"interval": interval}, daemon=True,
                         name="telegram-approvals")
    t.start()
    return t


# ── decide ────────────────────────────────────────────────────────────────
def _reply(code, answer, *, ok=False, alert=False, **extra):
    return code, {"ok": ok, "answer": answer[:190], "alert": alert, **extra}


def handle_decision(conn, market, body, resume_post, *, env, clock=time.time,
                    tg=None, settings=None):
    """(HTTP code, {ok, answer, alert, ...}) for one relayed tap or LIVE
    reply. `answer` is what the bot shows the user (answerCallbackQuery)."""
    settings = settings or alerts.telegram_settings()
    approver = settings.get("approver_id")
    from_id = str(body.get("from_id") or "")
    if not approver:
        return _reply(503, "Telegram approver is not configured on the desk.")
    if not from_id or not hmac.compare_digest(from_id.encode(),
                                              str(approver).encode()):
        return _reply(403, "Only the desk owner can decide trades.",
                      alert=True)
    tg = tg or Telegram(settings.get("token"))
    ensure_table(conn)
    chat_id = str(body.get("chat_id") or "")
    with _LOCK:
        if "live" in body:
            return _live_reply(conn, tg, body, chat_id, from_id, resume_post,
                               env=env, clock=clock)
        parsed = parse_callback(body.get("data"))
        if parsed is None:
            return _reply(400, "Unknown button.")
        nid, op, arg = parsed
        n = _notice(conn, nid)
        try:
            mid = int(body.get("message_id") or 0)
        except (TypeError, ValueError):
            mid = 0
        if n is None or n["chat_id"] != chat_id or n["message_id"] != mid:
            return _reply(404, "This button does not belong to a current "
                               "trade message.")
        if n["status"] != "sent":
            return _reply(200, "Already decided.", alert=False)
        wi = conn.execute("SELECT * FROM work_items WHERE id=?",
                          (n["work_item_id"],)).fetchone()
        opts = json.loads(n["options_json"] or "{}")
        why = stale_reason(conn, n, wi, opts, clock)
        if why:
            _close(conn, tg, n, f"<b>Closed:</b> {h(why)}.", clock)
            return _reply(200, f"Already decided: {why}")
        f = opts.get("facts") or {}
        f.setdefault("accounts", opts.get("accounts") or [])
        if op == "a":
            if _phase(n) != "approval":
                return _reply(400, "A refused trade retries on the account "
                                   "you approved — change it on Approvals.")
            return _op_account(conn, tg, n, f, opts, arg, env, clock)
        if op == "bk":
            opts["pending"] = None
            _save_opts(conn, nid, opts, clock)
            _restore(tg, n, f, opts, env)
            return _reply(200, "Back.", ok=True)
        if op in ("s", "rj", "rt", "cx"):
            return _op_choose(conn, tg, n, wi, f, opts, op, arg, env, clock)
        if op == "ok":
            p = opts.get("pending") or {}
            if not p.get("nonce") or not hmac.compare_digest(
                    str(arg).encode(), str(p["nonce"]).encode()):
                return _reply(409, "That confirmation is out of date — pick "
                                   "again.", alert=True)
            if p.get("live") and p["op"] in ("approve", "retry"):
                p["live_deadline"] = int(clock()) + LIVE_WINDOW_S
                opts["pending"] = p
                _save_opts(conn, nid, opts, clock)
                tg.edit(n["chat_id"], n["message_id"], _fit(
                    (n["base_html"] or "") + "\n\n" + p["text"] +
                    "\n\n⚠️ <b>REAL MONEY.</b> Reply <code>LIVE</code> (capitals)"
                    f" within 2 minutes (by {h(sgt(p['live_deadline']))}) to"
                    " place it. Anything else: nothing happens."),
                    [[{"text": "✖ Back", "callback_data": cb(nid, "bk")}]])
                return _reply(200, "Type LIVE within 2 minutes to place this "
                                   "real-money order.", ok=True, alert=True,
                              await_live=True)
            return _execute(conn, tg, n, wi, f, opts, p, resume_post,
                            from_id=from_id, confirm_live=None, clock=clock,
                            env=env)
        return _reply(400, "Unknown button.")


def _restore(tg, n, f, opts, env, note=""):
    tg.edit(n["chat_id"], n["message_id"],
            _fit((n["base_html"] or "") + (f"\n\n{note}" if note else "")),
            build_keyboard(n["id"], {**f, "phase": _phase(n)}, opts,
                           dashboard_url(env)))


def _phase(n):
    return "approval" if n["state_key"] == "approval" else "failed"


def _op_account(conn, tg, n, f, opts, arg, env, clock):
    from . import live_trading
    accts = opts.get("accounts") or []
    try:
        i = int(arg)
        a = accts[i]
    except (ValueError, IndexError):
        return _reply(400, "Unknown account.")
    if a.get("live") and not live_trading.status(conn, env)["armed"]:
        return _reply(409, "Saxo LIVE is not armed.", alert=True)
    opts["acct"], opts["pending"] = i, None
    opts["sizes"] = size_choices(f, a) if f.get("side") == "buy" else []
    _save_opts(conn, n["id"], opts, clock)
    _restore(tg, n, f, opts, env,
             note=f"<b>Account:</b> {h(a['label'])} — {h(a['kind'])}")
    return _reply(200, f"Account: {a['label']}", ok=True)


def _op_choose(conn, tg, n, wi, f, opts, op, arg, env, clock):
    """Build the confirmation screen; nothing is submitted here."""
    phase = _phase(n)
    acct = selected_account(f, opts)
    th = f.get("thesis") or {}
    side, tk = f.get("side", "?"), f.get("ticker")
    lim = th.get("entry_high") or th.get("entry_low")
    at = f" at limit {num(lim)} {f.get('ccy', '')}" if lim else ""
    p = {"op": None, "nonce": secrets.token_hex(4), "live": False}
    if phase == "approval" and op == "s":
        if acct is None:
            return _reply(400, "No account available.")
        p.update(op="approve", broker=acct["broker"],
                 trail_pct=f.get("trail_pct") or DEFAULT_TRAIL_PCT,
                 live=bool(acct.get("live")))
        try:
            i = int(arg)
        except ValueError:
            return _reply(400, "Unknown size.")
        if side == "buy":
            sizes = opts.get("sizes") or []
            if not 0 <= i < len(sizes):
                return _reply(400, "Unknown size.")
            sh, cost = estimate(f, acct, sizes[i])
            if sh <= 0:
                return _reply(400, "That size buys 0 shares at this price — "
                                   "pick a larger one.", alert=True)
            p["size_base"] = sizes[i]
            what = (f"BUY {qty_str(sh)} {tk} ≈ {num(cost)} {acct['ccy']}{at}"
                    f" (size {num(sizes[i], 0)} {acct['ccy']})")
        elif side == "sell":
            ch = sell_choices(f, acct)
            if not 0 <= i < len(ch):
                return _reply(400, "Unknown quantity.")
            lab, q = ch[i]
            p["qty"] = q
            px = (lim or f.get("price") or 0) * ((f.get("fx") or {}).get(
                acct["ccy"]) or 0)
            what = (f"SELL {qty_str(q)} {tk} ({lab.lower()})"
                    f" ≈ {num(q * px)} {acct['ccy']}{at}")
        else:
            return _reply(400, "Nothing to size on this trade.")
        p["text"] = (f"<b>Confirm:</b> {h(what)} on {h(acct['label'])} — "
                     f"<b>{h(acct['kind'])}</b>? Trailing floor "
                     f"{h(num(p['trail_pct'], 1))}%.")
        ok_label = "✔ Place it"
    elif phase == "approval" and op == "rj":
        p.update(op="reject")
        p["text"] = (f"<b>Reject</b> {h(side.upper())} {h(tk)}? The desk will"
                     " not trade it.")
        ok_label = "✔ Reject it"
    elif phase == "failed" and op == "rt":
        ap = ((f.get("failed") or {}).get("approval") or {})
        live = ap.get("broker") == "saxo_live"
        p.update(op="retry", live=live)
        what = approved_what(f, ap)
        p["text"] = (f"<b>Retry</b> {h(side.upper())} {h(tk)} — the same order"
                     f" ({h(what)} on {h(ap.get('broker'))})"
                     + (" — <b>REAL MONEY</b>" if live else " — paper")
                     + "? Every check runs again.")
        ok_label = "✔ Retry it"
    elif phase == "failed" and op == "cx":
        p.update(op="cancel")
        p["text"] = (f"<b>Cancel</b> the refused {h(side.upper())} {h(tk)}?"
                     " It is rejected for good.")
        ok_label = "✔ Cancel trade"
    else:
        return _reply(400, "That button does not apply here.")
    opts["pending"] = p
    _save_opts(conn, n["id"], opts, clock)
    tg.edit(n["chat_id"], n["message_id"],
            _fit((n["base_html"] or "") + "\n\n" + p["text"]),
            [[{"text": ok_label, "callback_data": cb(n["id"], "ok", p["nonce"])},
              {"text": "✖ Back", "callback_data": cb(n["id"], "bk")}]])
    return _reply(200, "Check the details, then tap ✔ to confirm.", ok=True)


def _live_reply(conn, tg, body, chat_id, from_id, resume_post, *, env,
                clock):
    if str(body.get("live") or "").strip() != "LIVE":
        return _reply(400, "Type exactly LIVE.")
    now = int(clock())
    for n in conn.execute("SELECT * FROM telegram_notices WHERE status='sent'"
                          " AND chat_id=? ORDER BY updated_at DESC",
                          (chat_id,)).fetchall():
        opts = json.loads(n["options_json"] or "{}")
        p = opts.get("pending") or {}
        if not p.get("live_deadline"):
            continue
        f = opts.get("facts") or {}
        if now > p["live_deadline"]:
            opts["pending"] = None
            _save_opts(conn, n["id"], opts, clock)
            _restore(tg, n, f, opts, env,
                     note="LIVE came too late — nothing was placed.")
            return _reply(200, "Too late — the 2-minute window closed. "
                               "Nothing was placed.")
        wi = conn.execute("SELECT * FROM work_items WHERE id=?",
                          (n["work_item_id"],)).fetchone()
        why = stale_reason(conn, n, wi, opts, clock)
        if why:
            _close(conn, tg, n, f"<b>Closed:</b> {h(why)}.", clock)
            return _reply(200, f"Already decided: {why}")
        return _execute(conn, tg, n, wi, f, opts, p, resume_post,
                        from_id=from_id, confirm_live="LIVE", clock=clock,
                        env=env)
    return _reply(200, "No real-money confirmation is waiting.")


def _log_event(conn, wi, op, from_id, p, clock):
    conn.execute(
        "INSERT INTO events (item_id, ts, actor, from_state, to_state,"
        " payload) VALUES (?,?,?,?,?,?)",
        (wi["id"], int(clock()), "human", wi["state"], f"telegram:{op}",
         json.dumps({"channel": "telegram", "telegram_user_id": from_id,
                     **{k: p.get(k) for k in ("size_base", "qty", "broker",
                                              "trail_pct")},
                     "confirm_live": bool(p.get("live"))})))


def _execute(conn, tg, n, wi, f, opts, p, resume_post, *, from_id,
             confirm_live, clock, env):
    op = p.get("op")
    logged = []

    def post(body):
        # the gate has accepted the decision: audit it now, before
        # claire-api appends its own outcome rows (see module docstring)
        if not logged:
            _log_event(conn, wi, op, from_id, p, clock)
            logged.append(True)
        return resume_post(body)

    try:
        if op in ("approve", "reject"):
            payload = {"work_item_id": wi["id"], "action": op,
                       "token": wi["approval_token"] or ""}
            if op == "approve":
                payload.update(broker=p["broker"], trail_pct=p.get("trail_pct")
                               or DEFAULT_TRAIL_PCT,
                               size_base=p.get("size_base"), qty=p.get("qty"),
                               confirm_live=confirm_live)
            approvals.thesis_action(conn, payload, post, clock=clock)
        elif op == "retry":
            approvals.retry_failed(conn, wi["id"], post,
                                   confirm_live=confirm_live)
        elif op == "cancel":
            approvals.cancel_failed(conn, wi["id"], clock=clock)
            _log_event(conn, wi, op, from_id, p, clock)   # item now terminal
        else:
            return _reply(400, "Nothing to confirm.")
    except approvals.AuthError as e:
        msg = e.message
        if e.code == 502 and logged:
            # the gate accepted it and it is recorded (token pending_resume),
            # but claire-api did not answer: the custodian re-sends it
            # (approvals.retry_pending). The decision itself stands.
            _close(conn, tg, n, f"⏳ <b>{h(op.upper())} recorded via Telegram"
                   f"</b> ({h(sgt(clock()))}) — claire-api did not answer;"
                   " the custodian will hand it over. Check Approvals.",
                   clock)
            return _reply(200, "Recorded — claire-api unreachable, the "
                               "custodian will retry.", ok=True, alert=True)
        if e.code == 400 and not logged:
            # a fixable input problem (cash, size, live not confirmed):
            # keep the buttons so another choice can be made
            opts["pending"] = None
            _save_opts(conn, n["id"], opts, clock)
            _restore(tg, n, f, opts, env, note=f"⚠ <b>Not placed:</b> {h(msg)}")
            return _reply(200, f"Not placed: {msg}", alert=True)
        _close(conn, tg, n, f"⚠ <b>Not done:</b> {h(msg)} ({h(sgt(clock()))}).",
               clock)
        return _reply(200, f"Not done: {msg}", alert=True)
    words = {"approve": "✅ <b>APPROVED via Telegram</b>",
             "reject": "✖ <b>REJECTED via Telegram</b>",
             "retry": "🔁 <b>RETRY sent via Telegram</b>",
             "cancel": "✖ <b>CANCELLED via Telegram</b>"}[op]
    detail = p.get("text", "").replace("<b>Confirm:</b> ", "")
    _close(conn, tg, n, f"{words} · {h(sgt(clock()))}"
           + (f"\n{detail}" if op in ("approve", "retry") else ""), clock)
    return _reply(200, {"approve": "Approved — sent to the desk.",
                        "reject": "Rejected.",
                        "retry": "Retry sent to the desk.",
                        "cancel": "Cancelled."}[op], ok=True)


# ── sample (inert) message, for checking rendering on a phone ─────────────
def sample_keyboard(base_url):
    s = "cl:sample"
    return [[{"text": "500 USD ≈4 sh", "callback_data": s},
             {"text": "1,000 USD ≈9 sh", "callback_data": s},
             {"text": "2,000 USD ≈19 sh", "callback_data": s}],
            [{"text": "✓ Saxo SIM", "callback_data": s},
             {"text": "alpaca sim", "callback_data": s},
             {"text": "moomoo sim", "callback_data": s}],
            [{"text": "✖ Reject", "callback_data": s}],
            [{"text": "Open Approvals ↗", "url": f"{base_url}/approvals"}]]
