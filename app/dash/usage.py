"""The Usage page's numbers: tokens and API-equivalent value per day, model,
agent and what started the run. Pure functions over desk.db — moved out of
dashboards.py unchanged (REVIEW-2026-11-11 §3.2, step 1)."""
import json
import time
from datetime import datetime
from pathlib import Path

from ..providers import registry

# ── LLM usage (the /usage page) ─────────────────────────────────────────────
# Every model call is a row in agent_runs. On the Max subscription (claude_sdk)
# cost_usd is 0 — nothing is billed — so the page counts TOKENS, and shows an
# "API-equivalent value" (published per-token prices) purely as a measure of
# what the subscription absorbs. tokens_in is stored as the TRUE prompt size
# (fresh + cache write + cache read, see claude_sdk.Meter.finish and
# registry.MeterCallback.on_llm_end); fresh input is derived, never re-added.
BOB_USAGE_FILE = Path.home() / "bob" / "usage.json"   # read-only, never written


USAGE_AGENT_LABELS = {
    "screener": "Screener", "fundamental": "Fundamental analyst",
    "technical": "Technical analyst", "news": "News analyst",
    "bull": "Bull researcher", "bear": "Bear researcher",
    "arbiter": "Arbiter", "claire_chat": "Claire chat",
    "recorder": "Recorder (trade journal)"}


USAGE_SOURCE_LABELS = {
    "daily_asia": "Daily screen (Asia)", "daily_eu": "Daily screen (Europe)",
    "daily_us": "Daily screen (US)", "daily": "Daily screen",
    "volume_scan": "Volume scan", "exit_review": "Exit review",
    "override": "Your override", "manual": "Your manual run",
    "chat": "Claire chat", "agent_qa": "Agent Q&A", "screener": "Screener",
    "journal": "Trade journal", "other": "Other research run"}


def usage_source(agent_id, kind=None, trigger=None, exchange=None,
                 linked=True):
    """Which entry point paid for an agent_runs row. The rules, in order:

    No work item (work_item_id NULL):
      claire_chat           → chat       (Claire chat: Bob, Telegram, /ask)
      screener              → screener   (the daily-screen picker itself)
      recorder              → journal    (trade-journal prose after a fill)
      any other agent       → agent_qa   (dashboard "ask this agent")
    A work item (research run), by work_items.kind then trigger text:
      kind override, or trigger "human override …"      → override
      kind sell_review, or trigger "watcher: …"         → exit_review
      trigger "event scan: …"                           → volume_scan
      trigger "screener agent…" / "deterministic fallback…", or kind screen
                         → daily_<region> (region from the ticker's exchange
                           via autonomous.REGIONS; unknown → daily)
      no trigger (dashboard Run button, Bob, autonomous CLI) → manual
      any other trigger text                            → manual
    `linked`: None = work_item_id is NULL, True = the work item exists,
    False = work_item_id names a row that no longer exists → other."""
    if linked is None:                       # no work item at all
        if agent_id == "claire_chat":
            return "chat"
        if agent_id == "screener":
            return "screener"
        if agent_id == "recorder":
            return "journal"
        return "agent_qa"
    if not linked:                           # dangling work_item_id
        return "other"
    t = (trigger or "").strip().lower()
    if kind == "override" or t.startswith("human override"):
        return "override"
    if kind == "sell_review" or t.startswith("watcher:"):
        return "exit_review"
    if t.startswith("event scan"):
        return "volume_scan"
    if kind == "screen" or t.startswith("screener agent") or \
            t.startswith("deterministic fallback"):
        from ..exchanges import REGIONS
        for region, exchanges in REGIONS.items():
            if exchange in exchanges:
                return f"daily_{region}"
        return "daily"
    return "manual"


def _usage_model_label(model):
    """claude-sonnet-4-6 → Claude Sonnet 4.6; unknown ids pass through."""
    m = str(model or "")
    if not m.startswith("claude-"):
        return m or "unknown"
    words, nums = [], []
    for part in m[len("claude-"):].split("-"):
        if part.isdigit() and len(part) <= 2:
            nums.append(part)
        elif not part.isdigit():          # a date suffix is dropped
            words.append(part.capitalize())
    return " ".join(["Claude"] + words + ([".".join(nums)] if nums else []))


def _usage_price(model):
    """(in, out) $/1k from the published table, matching a dated id by
    prefix; None for a model with no published price."""
    if model in registry.MODEL_PRICES:
        return registry.MODEL_PRICES[model]
    for k in sorted(registry.MODEL_PRICES, key=len, reverse=True):
        if str(model or "").startswith(k):
            return registry.MODEL_PRICES[k]
    return None


def _usage_value(model, fresh, cwrite, cread, out):
    """API-equivalent $ for one call — NOT billed on the subscription."""
    p = _usage_price(model)
    if not p:
        return 0.0
    cin, cout = p
    return (fresh / 1000 * cin
            + cwrite / 1000 * cin * registry.CACHE_WRITE_MULTIPLIER
            + cread / 1000 * cin * registry.CACHE_READ_MULTIPLIER
            + out / 1000 * cout)


def _usage_bucket():
    return {"calls": 0, "errors": 0, "input": 0, "fresh": 0, "cache_write": 0,
            "cache_read": 0, "output": 0, "tokens": 0, "api_value": 0.0,
            "billed": 0.0}


def _usage_add(b, r):
    b["calls"] += 1
    b["errors"] += r["status"] == "error"
    for k in ("input", "fresh", "cache_write", "cache_read", "output",
              "tokens", "api_value", "billed"):
        b[k] += r[k]


def _usage_day_start(now, days_back):
    """Local midnight `days_back` calendar days before now's day (built from
    the date, not now - n*86400, so DST days stay whole)."""
    from datetime import timedelta
    d = datetime.fromtimestamp(now).date() - timedelta(days=days_back)
    return int(datetime(d.year, d.month, d.day).timestamp())


def bob_usage(path=None, *, now=None, days=7):
    """Bob's own per-day token totals from ~/bob/usage.json (read-only).
    Bob writes one bucket per local day; input_tokens there is FRESH input
    only, so input here = fresh + cache read + cache write to match the
    desk's convention. Bob's cost_usd is the CLI's API-equivalent estimate
    — Bob also runs on the Max plan, so it is not billed either."""
    from datetime import timedelta
    path = Path(path or BOB_USAGE_FILE)
    now = time.time() if now is None else now
    try:
        data = json.loads(path.read_text())
        daily = data.get("daily") or {}
        if not isinstance(daily, dict):
            raise ValueError("daily is not an object")
    except FileNotFoundError:
        return {"available": False, "path": str(path),
                "reason": "no usage file — Bob has not recorded any usage"}
    except (OSError, ValueError) as e:
        return {"available": False, "path": str(path),
                "reason": f"unreadable usage file: {str(e)[:120]}"}

    def norm(b):
        b = b if isinstance(b, dict) else {}
        g = lambda k: int(b.get(k) or 0)                       # noqa: E731
        fresh, cr, cw = g("input_tokens"), g("cache_read_tokens"), \
            g("cache_creation_tokens")
        return {"calls": g("requests"), "fresh": fresh, "cache_read": cr,
                "cache_write": cw, "input": fresh + cr + cw,
                "output": g("output_tokens"),
                "tokens": fresh + cr + cw + g("output_tokens"),
                "api_value": float(b.get("cost_usd") or 0.0)}

    today = datetime.fromtimestamp(now).date()

    def window(n):
        tot = norm({})
        for i in range(n):
            day = norm(daily.get((today - timedelta(days=i)).isoformat()))
            for k in tot:
                tot[k] += day[k]
        return tot
    series = [{"date": (today - timedelta(days=i)).isoformat(),
               **norm(daily.get((today - timedelta(days=i)).isoformat()))}
              for i in reversed(range(days))]
    return {"available": True, "path": str(path),
            "granularity": "per day (no per-call or per-agent detail)",
            "last_day": max(daily) if daily else None,
            "today": window(1), "d7": window(7), "d30": window(30),
            "lifetime": norm(data.get("lifetime")), "series": series}


def usage_report(conn, *, now=None, days=7, bob_path=None):
    """Everything /api/usage returns. Read-only over desk.db."""
    from datetime import timedelta
    now = time.time() if now is None else now
    days = max(1, min(int(days), 365))
    span = max(days, 30)
    since = _usage_day_start(now, span - 1)
    win_start = _usage_day_start(now, days - 1)
    starts = {"today": _usage_day_start(now, 0),
              "d7": _usage_day_start(now, 6), "d30": _usage_day_start(now, 29)}
    names = {r["id"]: r["display_name"] for r in conn.execute(
        "SELECT id, display_name FROM agents")}
    provs = {r["id"]: r["display_name"] for r in conn.execute(
        "SELECT id, display_name FROM providers")}
    exch = {}
    for r in conn.execute("SELECT ticker, exchange FROM instruments"):
        exch.setdefault(r["ticker"], r["exchange"])
    rows = []
    for r in conn.execute(
            "SELECT r.id, r.agent_id, r.work_item_id, r.provider_id, r.model,"
            " r.started_at, r.status, r.error,"
            " COALESCE(r.tokens_in,0) ti, COALESCE(r.tokens_out,0) tout,"
            " COALESCE(r.cache_write_tokens,0) cw,"
            " COALESCE(r.cache_read_tokens,0) cr, COALESCE(r.cost_usd,0) cost,"
            " w.id AS wid, w.kind, w.ticker, w.trigger, w.created_at AS wat"
            " FROM agent_runs r LEFT JOIN work_items w ON w.id=r.work_item_id"
            " WHERE r.started_at >= ? ORDER BY r.started_at", (since,)):
        fresh = max(r["ti"] - r["cw"] - r["cr"], 0)
        linked = None if r["work_item_id"] is None else bool(r["wid"])
        src = usage_source(r["agent_id"], r["kind"], r["trigger"],
                           exch.get(r["ticker"]), linked=linked)
        rows.append({
            "agent_id": r["agent_id"], "work_item_id": r["work_item_id"],
            "provider_id": r["provider_id"], "model": r["model"],
            "started_at": r["started_at"] or 0, "status": r["status"],
            "error": r["error"], "source": src, "ticker": r["ticker"],
            "kind": r["kind"], "trigger": r["trigger"], "wat": r["wat"],
            "input": r["ti"], "fresh": fresh, "cache_write": r["cw"],
            "cache_read": r["cr"], "output": r["tout"],
            "tokens": r["ti"] + r["tout"], "billed": r["cost"],
            "api_value": _usage_value(r["model"], fresh, r["cw"], r["cr"],
                                      r["tout"])})

    totals = {k: _usage_bucket() for k in starts}
    for r in rows:
        for k, s in starts.items():
            if r["started_at"] >= s:
                _usage_add(totals[k], r)
    win = [r for r in rows if r["started_at"] >= win_start]

    def group(keyfn, label):
        out = {}
        for r in win:
            k = keyfn(r)
            if k not in out:
                out[k] = {"key": k, "label": label(r), **_usage_bucket()}
            _usage_add(out[k], r)
        return sorted(out.values(), key=lambda b: -b["tokens"])

    by_agent = group(lambda r: r["agent_id"],
                     lambda r: USAGE_AGENT_LABELS.get(r["agent_id"]) or
                     names.get(r["agent_id"]) or r["agent_id"])
    by_source = group(lambda r: r["source"],
                      lambda r: USAGE_SOURCE_LABELS[r["source"]])
    by_model = group(lambda r: f"{r['provider_id']}|{r['model']}",
                     lambda r: _usage_model_label(r["model"]))
    for b in by_model:
        pid, model = b["key"].split("|", 1)
        b.update(model=model, provider_id=pid,
                 provider=provs.get(pid) or pid,
                 priced=_usage_price(model) is not None)

    today = datetime.fromtimestamp(now).date()
    series = []
    for i in reversed(range(days)):
        d = today - timedelta(days=i)
        series.append({"date": d.isoformat(), "by_source": {},
                       **_usage_bucket()})
    idx = {s["date"]: s for s in series}
    for r in win:
        s = idx.get(datetime.fromtimestamp(r["started_at"]).date().isoformat())
        if s is None:
            continue
        _usage_add(s, r)
        s["by_source"][r["source"]] = s["by_source"].get(r["source"], 0) \
            + r["tokens"]

    runs = {}
    for r in win:
        if not r["work_item_id"]:
            continue
        b = runs.setdefault(r["work_item_id"], {
            "work_item_id": r["work_item_id"], "ticker": r["ticker"],
            "kind": r["kind"], "trigger": r["trigger"],
            "created_at": r["wat"] or r["started_at"], "source": r["source"],
            "source_label": USAGE_SOURCE_LABELS[r["source"]],
            **_usage_bucket()})
        _usage_add(b, r)
    top_runs = sorted(runs.values(), key=lambda b: -b["tokens"])[:10]

    window = _usage_bucket()
    for r in win:
        _usage_add(window, r)
    errs = [r for r in win if r["status"] == "error"]
    return {
        "now": int(now), "days": days, "window_start": win_start,
        "totals": totals, "window": window,
        "by_agent": by_agent, "by_source": by_source, "by_model": by_model,
        "series": series, "top_runs": top_runs,
        "errors": {"count": len(errs), "recent": [
            {"agent_id": r["agent_id"],
             "agent": USAGE_AGENT_LABELS.get(r["agent_id"]) or r["agent_id"],
             "at": r["started_at"], "error": (r["error"] or "")[:200]}
            for r in errs[-5:][::-1]]},
        "source_labels": USAGE_SOURCE_LABELS,
        "pricing_note": ("API-equivalent value = published per-token prices "
                         "(registry.MODEL_PRICES; cache write x"
                         f"{registry.CACHE_WRITE_MULTIPLIER}, cache read x"
                         f"{registry.CACHE_READ_MULTIPLIER}). NOT billed: "
                         "calls on the Claude Max subscription cost $0; "
                         "'billed' is what metered API calls actually cost."),
        "bob": bob_usage(bob_path, now=now, days=days)}
