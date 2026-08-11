"""The screener agent, wired into scheduled analysis runs (DESIGN.md §4).

Deterministic code builds a MULTI-FACTOR universe — three orthogonal scans
(momentum, unusual volume, oversold quality) plus your watchlist and
TradingView's rating leaders — and every candidate arrives tagged with why it
surfaced and its numbers. The agent then judges that universe with three kinds
of context it previously lacked: what was analysed recently (no re-picking the
same name daily), what the portfolio already holds (diversification), and its
own track record (which of its past picks actually made money).

Two hard rules survive from v1 of this module: the agent may only pick from
candidates it was shown (hallucinated tickers are dropped at validation), and
any failure raises — the caller falls back to a deterministic picker, so a
missing API key or provider outage never kills a scheduled run.
"""
import json
import time

from pydantic import BaseModel, Field

from .graph.nodes_analysis import default_structured_factory
from .tools import tradingview

RECENT_SESSIONS_DAYS = 5        # don't re-analyse the same name inside a week
CATALYST_SHORTLIST = 8          # how many names get a news check (phase 2)


class ScreenerError(Exception):
    pass


class ScreenerPick(BaseModel):
    ticker: str
    exchange: str
    reason: str = Field(max_length=200)


class Shortlist(BaseModel):
    picks: list[ScreenerPick]


# ── universe ─────────────────────────────────────────────────────────────
def gather_candidates(market, exchanges, held, *, conn=None, per_scan=10,
                      scans=("momentum", "unusual_volume", "oversold"),
                      _factor_scan=None, _recs=None) -> list[dict]:
    """Merge the factor scans + watchlist + rating leaders. Dedupes by
    ticker, keeping every scan tag that surfaced it (a name found by two
    scans is a stronger candidate, and the agent gets to see that)."""
    factor_scan = _factor_scan or tradingview.factor_scan
    recs = _recs or tradingview.recommendations
    by_ticker: dict[tuple, dict] = {}

    def add(c, tag):
        if not c.get("ticker") or c["ticker"] in held:
            return
        key = (c["ticker"], c["exchange"])
        cur = by_ticker.setdefault(key, {**c, "scans": []})
        for k, v in c.items():                  # fill gaps from later sources
            if cur.get(k) is None and v is not None:
                cur[k] = v
        if tag not in cur["scans"]:
            cur["scans"].append(tag)

    for ex in exchanges:
        for preset in scans:
            try:
                for c in factor_scan(ex, preset, count=per_scan):
                    add(c, preset)
            except Exception:                   # noqa: BLE001 — one scan or
                continue                        # venue down ≠ no screening
        try:
            for r in recs(ex, count=10)["rows"]:
                add({"ticker": r.get("ticker"), "exchange": ex,
                     "name": r.get("name"), "price": r.get("price"),
                     "change_pct": r.get("change_pct"),
                     "volume": r.get("volume"), "mcap": r.get("mcap"),
                     "rsi": r.get("rsi"), "tv_rating": r.get("rating_label")},
                    "tv_rated")
        except Exception:                       # noqa: BLE001
            pass

    if conn is not None:                        # your watchlist always counts
        for w in conn.execute("SELECT * FROM watchlist"):
            if w["exchange"] in exchanges and w["ticker"] not in held:
                add({"ticker": w["ticker"], "exchange": w["exchange"],
                     "name": w["note"] or "(watchlist)"}, "watchlist")
    return list(by_ticker.values())


# ── context the agent needs to judge well ────────────────────────────────
def recent_tickers(conn, *, clock=time.time, days=RECENT_SESSIONS_DAYS):
    since = int(clock()) - days * 86400
    return {r["ticker"] for r in conn.execute(
        "SELECT DISTINCT ticker FROM work_items WHERE created_at > ?",
        (since,))}


def portfolio_context(conn) -> str:
    rows = [dict(r) for r in conn.execute(
        "SELECT i.ticker, i.exchange, v.qty, v.cost_base FROM v_positions v"
        " JOIN instruments i ON i.id = v.instrument_id")]
    if not rows:
        return "Portfolio: no open positions — a first position is fine."
    total = sum(r["cost_base"] or 0 for r in rows) or 1
    parts = [f"{r['ticker']} ({r['exchange']}, "
             f"{(r['cost_base'] or 0) / total * 100:.0f}% of book)"
             for r in rows]
    return ("Portfolio already holds: " + ", ".join(parts) +
            ". Prefer candidates that diversify exchange and sector.")


def track_record(conn, *, limit=12) -> str:
    rows = [dict(r) for r in conn.execute(
        "SELECT ticker, state, realized_pl FROM v_thesis_outcomes"
        " ORDER BY work_item_id DESC LIMIT ?", (limit,))]
    if not rows:
        return "Track record: no completed theses yet."
    approved = [r for r in rows if r["state"] in ("done", "executing")]
    winners = [r for r in approved if (r["realized_pl"] or 0) > 0]
    closed = [r for r in approved if (r["realized_pl"] or 0) != 0]
    return (f"Your track record (last {len(rows)} theses): "
            f"{len(approved)} approved by Richard, "
            f"{len(winners)}/{len(closed) or 0} profitable once closed. "
            + ("Recent: " + ", ".join(
                f"{r['ticker']} {r['realized_pl']:+.0f}" for r in closed[:5])
               if closed else "Nothing closed yet."))


# ── catalyst check (phase 2) ─────────────────────────────────────────────
def catalyst_notes(candidates, *, search_fn=None, env=None,
                   limit=CATALYST_SHORTLIST) -> dict:
    """Bounded news pass over the strongest few names only — a handful of
    searches per scan, never one per candidate. Failures are silent: a
    missing headline is not a reason to fail a screening run."""
    from .tools.search import web_search
    search = search_fn or (lambda q: web_search(q, max_results=3,
                                                env=env or {}))
    ranked = sorted(candidates,
                    key=lambda c: (len(c.get("scans", [])),
                                   c.get("rel_volume") or 0), reverse=True)
    notes = {}
    for c in ranked[:limit]:
        try:
            hits = search(f"{c['ticker']} {c.get('name') or ''} stock news")
        except Exception:                       # noqa: BLE001
            continue
        if hits:
            notes[c["ticker"]] = "; ".join(
                (h.get("title") or "")[:110] for h in hits[:2])
    return notes


# ── prompt ───────────────────────────────────────────────────────────────
def _render(cands, notes) -> str:
    lines = []
    for c in cands:
        bits = [f"{c['ticker']} ({c['exchange']})", str(c.get("name") or "")]
        if c.get("price") is not None:
            bits.append(f"px {c['price']}")
        if c.get("change_pct") is not None:
            bits.append(f"today {c['change_pct']:+.1f}%")
        if c.get("perf_1m") is not None:
            bits.append(f"1m {c['perf_1m']:+.0f}%")
        if c.get("rel_volume"):
            bits.append(f"rel-vol {c['rel_volume']:.1f}x")
        if c.get("off_52w_high_pct") is not None:
            bits.append(f"{c['off_52w_high_pct']:.0f}% vs 52w high")
        if c.get("rsi"):
            bits.append(f"RSI {c['rsi']:.0f}")
        if c.get("mcap"):
            bits.append(f"mcap {c['mcap'] / 1e9:.1f}B")
        if c.get("sector"):
            bits.append(str(c["sector"]))
        if c.get("tv_rating"):
            bits.append(f"TV {c['tv_rating']}")
        bits.append("found by: " + ", ".join(c.get("scans") or ["listing"]))
        line = "- " + " · ".join(b for b in bits if b)
        if notes.get(c["ticker"]):
            line += f"\n    news: {notes[c['ticker']]}"
        lines.append(line)
    return "\n".join(lines)


def screener_pick(conn, market, exchanges, held, *, limit=2, env=None,
                  structured_factory=None, clock=time.time,
                  with_catalysts=True, _gather=None) -> list[ScreenerPick]:
    """One metered structured call on the screener agent's own model+prompt.
    Raises ScreenerError on any failure — callers MUST fall back."""
    if not exchanges:
        return []
    gather = _gather or gather_candidates
    cands = gather(market, exchanges, held, conn=conn)
    if not cands:
        raise ScreenerError("no candidate data from any source")

    recent = recent_tickers(conn, clock=clock)
    fresh = [c for c in cands if c["ticker"] not in recent
             or "watchlist" in (c.get("scans") or [])]
    if fresh:                                   # dedupe unless it empties us
        cands = fresh
    notes = catalyst_notes(cands, env=env) if with_catalysts else {}

    factory = structured_factory or default_structured_factory(conn, env=env)
    try:
        llm, system = factory("screener", Shortlist, None)
        prompt = (
            f"Open markets right now: {', '.join(exchanges)}.\n"
            f"{portfolio_context(conn)}\n"
            f"{track_record(conn)}\n"
            f"Already held (never pick): {', '.join(sorted(held)) or 'none'}.\n"
            f"Analysed in the last {RECENT_SESSIONS_DAYS} days (avoid unless "
            f"something material changed): {', '.join(sorted(recent)) or 'none'}"
            f"\n\nCandidates — the ONLY names you may pick from. Each shows "
            f"which scan surfaced it (momentum = strength, unusual_volume = "
            f"something is happening today, oversold = mean reversion, "
            f"watchlist = Richard asked for it, tv_rated = analyst "
            f"consensus). A name found by SEVERAL scans is a stronger "
            f"signal:\n{_render(cands, notes)}\n\n"
            f"Shortlist up to {limit} for full pipeline analysis. Weigh the "
            f"evidence you were given — liquidity, a checkable catalyst, and "
            f"fit with the existing book. One short reason each, citing the "
            f"numbers. Fewer picks — or none — beats padding.")
        obj = llm.invoke([("system", system), ("user", prompt)])
        if isinstance(obj, dict):
            obj = Shortlist.model_validate(obj)
    except Exception as e:
        raise ScreenerError(str(e)[:200]) from e
    valid = {(c["ticker"], c["exchange"]) for c in cands}
    picks = [p for p in obj.picks if (p.ticker, p.exchange) in valid][:limit]
    if not picks:
        raise ScreenerError("screener returned no valid picks")
    return picks


# ── intraday event scan (phase 3) ────────────────────────────────────────
def event_scan(conn, exchanges, held, *, threshold=3.0, _factor_scan=None):
    """Cheap hourly sweep: only unusual volume, no LLM. Returns names whose
    relative volume crossed the threshold and that we haven't just analysed —
    the caller decides whether to wake the full pipeline."""
    factor_scan = _factor_scan or tradingview.factor_scan
    recent = recent_tickers(conn)
    hits = []
    for ex in exchanges:
        try:
            rows = factor_scan(ex, "unusual_volume", count=10)
        except Exception:                       # noqa: BLE001
            continue
        for c in rows:
            if (c["ticker"] in held or c["ticker"] in recent
                    or not c.get("rel_volume")):
                continue
            if c["rel_volume"] >= threshold:
                hits.append(c)
    return sorted(hits, key=lambda c: -c["rel_volume"])
