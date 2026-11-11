"""The Watchlist page's data (GET /api/watchlist-data, DESIGN.md §15a).

One request for the whole page: per watchlisted name the live quote (one
batch spark call), the reference levels from daily bars (the same `_levels`
the override form uses), what the desk holds there — shares, average cost,
P/L, the working stop/target legs at the venue — and its price triggers;
plus the accounts a trade can go to, with whether a trigger may be placed
automatically on each (paper only). Read-only; every failure is reported
per row, never as a failed page.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from . import portfolio, price_triggers, sessions
from .tools.market import yahoo_symbol
import logging
from .logsetup import swallowed

log = logging.getLogger(__name__)


def _quotes(market, symbols):
    if not symbols:
        return {}
    try:
        return dict(market.spark(symbols))
    except Exception as e:                      # noqa: BLE001
        swallowed(log, "batch quote failed; pricing each symbol on its own", e)
        pass
    out = {}
    for s in symbols:                           # one bad name must not blind
        try:                                    # the others
            out[s] = market.quote(s)
        except Exception as e:                  # noqa: BLE001
            out[s] = {"error": str(e)[:200]}
    return out


def _levels_for(market, sym):
    from .dashboards import _levels
    try:
        return _levels(market.chart(sym, "6mo", "1d")), None
    except Exception as e:                      # noqa: BLE001
        return {}, str(e)[:200]


def accounts(conn, repo, env):
    """The card's account list (same labels and live rule as Approvals),
    plus the trigger rule: automatic placement only on paper money."""
    from . import live_trading
    from .dashboards import _approval_account, ordered_accounts
    live = live_trading.status(conn, env or {})
    out = []
    for a in ordered_accounts(conn, env):
        d = _approval_account(a, repo, env or {}, live)
        d["auto_ok"] = price_triggers.auto_refusal(a) is None
        out.append(d)
    return out, live


def _fix_suffixed(conn):
    """Rows stored as "APC.DE" on XETRA (see market.desk_ticker) → "APC"."""
    from .tools.market import desk_ticker
    for r in conn.execute("SELECT ticker, exchange FROM watchlist").fetchall():
        t = desk_ticker(r["ticker"], r["exchange"])
        if t != r["ticker"]:
            conn.execute("UPDATE OR IGNORE watchlist SET ticker=? WHERE"
                         " ticker=? AND exchange=?",
                         (t, r["ticker"], r["exchange"]))
            conn.execute("DELETE FROM watchlist WHERE ticker=? AND"
                         " exchange=?", (r["ticker"], r["exchange"]))


def view(conn, repo, market, *, env=None, clock, cal=None):
    now = int(clock())
    nowdt = datetime.fromtimestamp(now, tz=timezone.utc)
    _fix_suffixed(conn)
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM watchlist ORDER BY exchange, ticker").fetchall()]
    syms = [yahoo_symbol(r["ticker"], r["exchange"]) for r in rows]
    quotes = _quotes(market, sorted(set(syms)))
    names = {}
    if syms and hasattr(market, "metrics"):
        try:
            names = market.metrics(sorted(set(syms)))
        except Exception:                       # noqa: BLE001 — names are a
            names = {}                          # nicety, not a requirement
    # daily bars per name (cached ~15 min by Market); fetched in parallel so
    # a long list does not take one round-trip per row
    with ThreadPoolExecutor(max_workers=4) as pool:
        lv = dict(zip(syms, pool.map(lambda s: _levels_for(market, s),
                                     syms)))
    positions = portfolio.open_positions(conn)
    trig = price_triggers.list_triggers(conn, since=now - 7 * 86400)
    for r, sym in zip(rows, syms):
        q = quotes.get(sym) or {}
        m = names.get(sym) or {}
        inst = conn.execute("SELECT name, currency FROM instruments WHERE"
                            " ticker=? AND exchange=? LIMIT 1",
                            (r["ticker"], r["exchange"])).fetchone()
        r["symbol"] = sym
        r["name"] = m.get("name") or (inst["name"] if inst else None)
        price = q.get("price")
        prev = q.get("previous_close")
        chg = q.get("change_pct")
        if chg is None and price is not None and prev:
            chg = (float(price) / float(prev) - 1) * 100
        r["quote"] = {"price": float(price) if price is not None else None,
                      "currency": q.get("currency") or (
                          inst["currency"] if inst else None),
                      "change_pct": chg, "previous_close": prev,
                      "stale": bool(q.get("stale")), "src": q.get("src"),
                      "error": q.get("error")}
        r["levels"], r["levels_error"] = lv.get(sym, ({}, None))
        from .dashboards import quote_units      # London bars are in pence
        r["levels"] = quote_units(r["levels"], r["quote"]["price"])
        if m.get("day_low") is not None:        # today's range, live source
            r["levels"] = {**r["levels"], "day_low": float(m["day_low"]),
                           "day_high": float(m["day_high"])}
        try:
            r["is_open"] = sessions.is_open(r["exchange"], nowdt, cal)
            r["next_open"] = sessions.next_open(r["exchange"], nowdt,
                                                cal).isoformat()
        except KeyError:
            r["is_open"], r["next_open"] = None, None
        held = []
        for p in positions:
            if (p["ticker"], p["exchange"]) != (r["ticker"], r["exchange"]):
                continue
            fills = portfolio.fills_for(conn, p["account_id"],
                                        p["instrument_id"])
            avg = portfolio.levels_for(conn, p, fills)["avg_cost_native"]
            pl = pl_pct = None
            if avg and price is not None:
                # native currency: what the chart and the quote are in
                pl = (float(price) - avg) * p["qty"]
                pl_pct = (float(price) / avg - 1) * 100 * (
                    1 if p["qty"] > 0 else -1)
            held.append({"account_id": p["account_id"],
                         "broker": p["broker"], "qty": p["qty"],
                         "side": p["side"], "avg_cost": avg, "pl": pl,
                         "pl_pct": pl_pct,
                         "legs": portfolio.legs_for(conn, p["account_id"],
                                                    p["instrument_id"])})
        r["held"] = held
        r["held_total"] = sum(h["qty"] for h in held)
        r["triggers"] = [t for t in trig if t["ticker"] == r["ticker"]
                         and t["exchange"] == r["exchange"]]
    accts, live = accounts(conn, repo, env)
    return {"rows": rows, "accounts": accts,
            "live_armed": bool(live.get("armed")),
            "triggers": trig, "now": now}
