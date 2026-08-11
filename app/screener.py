"""The screener agent, wired into scheduled analysis runs (DESIGN.md §4).

Deterministic code gathers the candidate universe (Yahoo listings + TradingView
ratings, held names excluded); the screener agent judges it and returns a
STRUCTURED shortlist with reasons. Two hard rules: it may only pick from the
candidates it was shown (hallucinated tickers are dropped at validation), and
any failure raises — the caller falls back to the deterministic picker, so a
missing API key or provider outage never kills a scheduled run.
"""
from pydantic import BaseModel, Field

from .graph.nodes_analysis import default_structured_factory
from .tools import tradingview


class ScreenerError(Exception):
    pass


class ScreenerPick(BaseModel):
    ticker: str
    exchange: str
    reason: str = Field(max_length=200)


class Shortlist(BaseModel):
    picks: list[ScreenerPick]


def gather_candidates(market, exchanges, held, *, per_exchange=15) -> list[dict]:
    out = []
    for ex in exchanges:
        rows, tv = [], {}
        try:
            rows = market.screener(ex, count=per_exchange)["rows"]
        except Exception:                       # noqa: BLE001 — source down:
            pass                                # the other one may still work
        try:
            tv = {r["ticker"]: r for r in
                  tradingview.recommendations(ex, count=20)["rows"]}
        except Exception:                       # noqa: BLE001
            pass
        seen = set()
        for r in rows:
            ticker = (r.get("symbol") or "").split(".")[0]
            if not ticker or ticker in held or ticker in seen:
                continue
            seen.add(ticker)
            t = tv.get(ticker, {})
            out.append({"ticker": ticker, "exchange": ex,
                        "name": r.get("name"), "price": r.get("price"),
                        "change_pct": r.get("change_pct"),
                        "mcap": r.get("mcap"),
                        "tv_rating": t.get("rating_label")})
        for ticker, t in list(tv.items())[:8]:  # TV-only names count too
            if ticker not in seen and ticker not in held:
                seen.add(ticker)
                out.append({"ticker": ticker, "exchange": ex,
                            "name": t.get("name"), "price": t.get("price"),
                            "change_pct": t.get("change_pct"),
                            "mcap": t.get("mcap"),
                            "tv_rating": t.get("rating_label")})
    return out


def _render(cands) -> str:
    lines = []
    for c in cands:
        bits = [f"{c['ticker']} ({c['exchange']})", str(c.get("name") or "")]
        if c.get("price") is not None:
            bits.append(f"px {c['price']}")
        if c.get("change_pct") is not None:
            bits.append(f"chg {c['change_pct']:+.1f}%")
        if c.get("mcap"):
            bits.append(f"mcap {c['mcap'] / 1e9:.1f}B")
        if c.get("tv_rating"):
            bits.append(f"TV: {c['tv_rating']}")
        lines.append("- " + " · ".join(bits))
    return "\n".join(lines)


def screener_pick(conn, market, exchanges, held, *, limit=2, env=None,
                  structured_factory=None) -> list[ScreenerPick]:
    """One metered structured call on the screener agent's own model+prompt.
    Raises ScreenerError on any failure — callers MUST fall back."""
    if not exchanges:
        return []
    cands = gather_candidates(market, exchanges, held)
    if not cands:
        raise ScreenerError("no candidate data from any source")
    factory = structured_factory or default_structured_factory(conn, env=env)
    try:
        llm, system = factory("screener", Shortlist, None)
        prompt = (
            f"Open markets right now: {', '.join(exchanges)}.\n"
            f"Already held (do not pick): {', '.join(sorted(held)) or 'none'}.\n"
            f"Candidate universe (the ONLY names you may pick from):\n"
            f"{_render(cands)}\n\n"
            f"Shortlist up to {limit} for a full pipeline analysis. Favour "
            "liquidity and a fresh, checkable catalyst; one short reason each. "
            "Fewer picks — or none — beats padding.")
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
