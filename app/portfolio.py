"""Portfolio assembly: what you own, what you paid, what it is worth now,
and the levels that matter (your average cost, the thesis stop and target,
the watcher's alert).

Everything personal comes from desk.db — the book is the source of truth for
quantity, price paid and P&L; market data only supplies the live price and
the usual trader metrics. Market lookups are best-effort: a throttled source
degrades a card to "price unavailable", it never hides a position you hold.
"""
import json
import time
from decimal import Decimal

from .tools.market import yahoo_symbol

MICRO = Decimal(1_000_000)


def _d(v) -> Decimal:
    return Decimal(v or 0) / MICRO


def open_positions(conn) -> list[dict]:
    """One row per (account, instrument) with the book's own numbers."""
    rows = []
    for p in conn.execute(
            "SELECT l.account_id, l.instrument_id, i.ticker, i.exchange,"
            " i.currency, a.broker, a.base_currency,"
            " SUM(CASE WHEN l.qty_opened<0 THEN -l.qty_remaining"
            "     ELSE l.qty_remaining END) AS qty,"
            # SIGNED cost: a short's entry is money received, so it is
            # negative here; value (price × negative qty) minus this signed
            # cost is then the right P&L for either side (M8)
            " SUM(CASE WHEN l.qty_opened<0 THEN -1 ELSE 1 END"
            "     * l.qty_remaining*l.cost_per_share_base) AS cost_num,"
            " SUM(l.commission_allocated) AS commissions,"
            " MIN(l.opened_at) AS opened_at"
            " FROM lots l"
            " JOIN instruments i ON i.id = l.instrument_id"
            " JOIN broker_accounts a ON a.id = l.account_id"
            " WHERE l.qty_remaining > 0"
            " GROUP BY l.account_id, l.instrument_id"):
        qty = _d(p["qty"])
        if qty == 0:
            continue
        cost_base = Decimal(p["cost_num"] or 0) / MICRO / MICRO
        rows.append({
            "account_id": p["account_id"], "broker": p["broker"],
            "base_currency": p["base_currency"],
            "instrument_id": p["instrument_id"], "ticker": p["ticker"],
            "exchange": p["exchange"], "currency": p["currency"],
            "symbol": yahoo_symbol(p["ticker"], p["exchange"]),
            "qty": float(qty),
            "side": "short" if qty < 0 else "long",
            "cost_base": float(cost_base),
            "avg_cost_base": float(cost_base / qty) if qty else None,
            "commissions": float(_d(p["commissions"])),
            "opened_at": p["opened_at"]})
    return rows


def fills_for(conn, account_id, instrument_id) -> list[dict]:
    """Every fill on this position — what you actually paid, when. These
    become the markers on the chart."""
    return [{"ts": e["executed_at"], "side": e["side"],
             "qty": float(_d(e["qty"])),
             "price_native": float(_d(e["price_native"])),
             "commission": float(_d(e["commission"])),
             "fx_rate": float(_d(e["fx_rate"])),
             "work_item_id": e["work_item_id"]}
            for e in conn.execute(
                "SELECT * FROM executions WHERE account_id=? AND"
                " instrument_id=? ORDER BY executed_at",
                (account_id, instrument_id))]


def levels_for(conn, position, fills, fx=None, now=None) -> dict:
    """The lines worth drawing: your average cost, the thesis stop/target
    that justified the trade, and the watcher's alert threshold — all in the
    instrument's own currency. `fx` is native → account base (current); the
    watcher stores its trailing peak in BASE currency, so it is converted
    back here (falling back to the rate at the last opening fill)."""
    out = {"avg_cost_native": None, "stop_loss": None, "take_profit": None,
           "entry_low": None, "entry_high": None, "alert": None,
           "work_item_id": None}
    short = (position or {}).get("qty", 0) < 0
    # the fills that OPENED this position: buys for a long, sells for a short
    opens = [f for f in fills if f["side"] == ("sell" if short else "buy")]
    if opens:
        qty = sum(f["qty"] for f in opens)
        if qty:
            out["avg_cost_native"] = sum(
                f["price_native"] * f["qty"] for f in opens) / qty
    wi_id = next((f["work_item_id"] for f in reversed(opens)
                  if f["work_item_id"]), None)
    if wi_id:
        row = conn.execute("SELECT thesis_json FROM work_items WHERE id=?",
                           (wi_id,)).fetchone()
        if row and row["thesis_json"]:
            try:
                t = json.loads(row["thesis_json"])
                out.update({"stop_loss": t.get("stop_loss"),
                            "take_profit": t.get("take_profit"),
                            "entry_low": t.get("entry_low"),
                            "entry_high": t.get("entry_high"),
                            "work_item_id": wi_id})
            except ValueError:
                pass
    # this position's own rule (instrument, account, side); a legacy row
    # with no account/side applies to any position in the name
    alert = conn.execute(
        "SELECT rule, threshold, peak_base, last_fired_at, fire_count_today,"
        " fire_count_date FROM price_alerts"
        " WHERE instrument_id=? AND armed=1 AND rule='trail_pct'"
        " AND (account_id IS NULL OR account_id=?)"
        " AND (side IS NULL OR side=?)"
        " ORDER BY account_id IS NULL, id DESC LIMIT 1",
        (position["instrument_id"], position.get("account_id"),
         -1 if short else 1)).fetchone()
    if alert:
        # draw the floor where it ACTUALLY sits — trailed from the best
        # price seen, not pinned to entry. Falls back to average cost until
        # the watcher's first tick has set a peak. A short's stop is ABOVE.
        ref = out["avg_cost_native"]
        if alert["peak_base"]:
            rate = float(fx) if fx else next(
                (f.get("fx_rate") for f in reversed(opens)
                 if f.get("fx_rate")), None)
            ref = alert["peak_base"] / rate if rate else None
        if ref:
            sign = 1 if short else -1
            # once it has fired today the watcher waits for a further
            # ESCALATE_PCT before alerting again (watcher._breached): show
            # the level that is LIVE now, not the one already passed
            from .watcher import ESCALATE_PCT, MAX_FIRES_PER_DAY
            today = time.strftime("%Y-%m-%d", time.gmtime(
                now if now is not None else time.time()))
            fired = alert["fire_count_today"] or 0 \
                if alert["fire_count_date"] == today else 0
            live_pct = alert["threshold"] + ESCALATE_PCT * fired
            out["alert"] = ref * (1 + sign * live_pct / 100)
            out["alert_rule"] = (f"trailing {'+' if short else '−'}"
                                 f"{live_pct:.0f}% of {ref:.2f}")
            out["alert_peak"] = alert["peak_base"]
            out["alert_pct"] = float(alert["threshold"])   # the % you set
            out["alert_ref"] = ref        # best price since bought (native)
            if fired:
                out["alert_set_level"] = ref * (1 + sign *
                                                alert["threshold"] / 100)
                out["alert_live_pct"] = float(live_pct)
                out["alert_fired_today"] = fired
                out["alert_done_today"] = fired >= MAX_FIRES_PER_DAY
                out["alert_last_fired"] = alert["last_fired_at"]
                # what the review it started concluded
                rv = conn.execute(
                    "SELECT w.id, w.state, w.thesis_json FROM work_items w"
                    " JOIN instruments i ON i.ticker=w.ticker WHERE"
                    " i.id=? AND w.kind='sell_review' AND w.created_at>=?"
                    " ORDER BY w.created_at DESC LIMIT 1",
                    (position["instrument_id"],
                     (alert["last_fired_at"] or 0) - 120)).fetchone()
                if rv:
                    try:
                        verdict = json.loads(rv["thesis_json"] or "{}").get(
                            "direction")
                    except ValueError:
                        verdict = None
                    out["alert_review"] = {"id": rv["id"], "state": rv["state"],
                                           "verdict": verdict}
    return out


def legs_for(conn, account_id, instrument_id) -> list[dict]:
    """The protective orders resting at the venue for this position (the
    bracket's stop-loss / take-profit, DESIGN.md §11a) — what will actually
    sell it if the price gets there, as opposed to the thesis levels.
    A stop whose entry carried a trail % on a venue that holds stops (Saxo)
    is a TRAILING stop there: `trails` is its distance."""
    from .graph.nodes_execution import trail_distance

    def trails(r):
        if r["leg"] != "stop" or not r["parent_order_id"] or \
                not str(r["broker_order_id"] or "").isdigit():
            return None                     # sim legs ("sim-…") never trail
        p = conn.execute("SELECT trail_pct, stop_loss, limit_price FROM"
                         " orders WHERE id=?", (r["parent_order_id"],)
                         ).fetchone()
        d = trail_distance(p) if p else None
        return {"trails": float(d), "trail_pct": p["trail_pct"]} if d \
            else None
    return [{"leg": r["leg"], "side": r["side"],
             "qty": float(_d(r["qty"])),
             "price": float(_d(r["stop_loss"] if r["leg"] == "stop"
                               else r["limit_price"])),
             "status": r["status"], "venue_order": r["broker_order_id"],
             **(trails(r) or {})}
            for r in conn.execute(
                "SELECT * FROM orders WHERE account_id=? AND instrument_id=?"
                " AND leg IS NOT NULL AND status IN ('placed',"
                "'partially_filled') ORDER BY leg", (account_id,
                                                     instrument_id))]


def realized_for(conn, account_id, instrument_id) -> float:
    (pl,) = conn.execute(
        "SELECT COALESCE(SUM(c.realized_pl_base),0) FROM lot_closures c"
        " JOIN lots l ON l.id = c.lot_id WHERE l.account_id=? AND"
        " l.instrument_id=?", (account_id, instrument_id)).fetchone()
    return float(_d(pl))


def build(conn, market, *, clock, cal=None) -> dict:
    """The whole portfolio view: positions grouped by exchange, each with
    book numbers, live market metrics and the levels for its chart."""
    from datetime import datetime, timezone

    from . import sessions
    positions = open_positions(conn)
    symbols = sorted({p["symbol"] for p in positions})
    quotes = market.metrics(symbols) if symbols else {}
    now = datetime.fromtimestamp(int(clock()), tz=timezone.utc)

    total_value = Decimal(0)
    for p in positions:
        q = quotes.get(p["symbol"]) or {}
        price = q.get("price")
        p["market"] = {k: q.get(k) for k in
                       ("name", "price", "currency", "change_pct", "day_open",
                        "day_high", "day_low", "volume", "avg_volume",
                        "week52_high", "week52_low", "rsi", "mcap", "src")}
        p["price_unavailable"] = price is None
        p["fx_unavailable"] = False
        fx = Decimal(1)
        if price is not None and q.get("currency") and \
                q["currency"] != p["base_currency"]:
            try:
                fx = Decimal(str(market.fx(q["currency"], p["base_currency"])))
            except Exception:                   # noqa: BLE001
                # no rate → no value. Pricing a ¥ position at 1.0 showed it
                # ~150× too large (H6); "unavailable" is honest, wrong isn't
                fx = None
                p["fx_unavailable"] = True
        if price is not None and fx is not None:
            value = Decimal(str(price)) * fx * Decimal(str(p["qty"]))
            cost = Decimal(str(p["cost_base"]))     # signed: short < 0
            p["value_base"] = float(value)
            p["unrealized_base"] = float(value - cost)
            p["unrealized_pct"] = (float((value - cost) / abs(cost) * 100)
                                   if cost else None)
            total_value += value
        else:
            p["value_base"] = None
            p["unrealized_base"] = None
            p["unrealized_pct"] = None
        p["realized_base"] = realized_for(conn, p["account_id"],
                                          p["instrument_id"])
        p["fills"] = fills_for(conn, p["account_id"], p["instrument_id"])
        p["levels"] = levels_for(conn, p, p["fills"], fx=fx)
        p["legs"] = legs_for(conn, p["account_id"], p["instrument_id"])
        p["holding_days"] = (int(clock()) - (p["opened_at"] or
                                             int(clock()))) // 86400
        try:
            p["is_open"] = sessions.is_open(p["exchange"], now, cal)
            p["next_open"] = sessions.next_open(p["exchange"], now,
                                                cal).isoformat()
        except KeyError:
            p["is_open"], p["next_open"] = None, None

    # weights are of GROSS exposure: a short's value is negative, and a
    # net denominator would let longs and shorts cancel into nonsense %.
    # Per account currency: value_base is in each account's OWN currency,
    # so a weight across SGD and USD accounts would divide dollars by a sum
    # of dollars and Singapore dollars.
    gross = {}
    for p in positions:
        if p.get("value_base") is not None:
            gross[p["base_currency"]] = gross.get(p["base_currency"], 0) + \
                abs(Decimal(str(p["value_base"])))
    for p in positions:
        g = gross.get(p["base_currency"])
        p["weight_pct"] = (float(abs(Decimal(str(p["value_base"]))) / g * 100)
                           if p.get("value_base") and g else None)

    exchanges = {}
    for p in positions:
        exchanges.setdefault(p["exchange"], []).append(p)
    for ex, items in exchanges.items():
        items.sort(key=lambda x: -abs(x.get("value_base") or 0))

    closed = [dict(r) for r in conn.execute(
        "SELECT i.ticker, i.exchange, c.qty/1e6 AS qty,"
        " c.realized_pl_base/1e6 AS realized, c.closed_at, c.holding_days"
        " FROM lot_closures c JOIN lots l ON l.id=c.lot_id"
        " JOIN instruments i ON i.id=l.instrument_id"
        " WHERE c.closed_at > ? ORDER BY c.closed_at DESC LIMIT 20",
        (int(clock()) - 30 * 86400,))]

    accounts = []
    for a in conn.execute("SELECT * FROM broker_accounts"):
        (cash,) = conn.execute(
            "SELECT COALESCE(SUM(amount_base),0) FROM cash_transactions"
            " WHERE account_id=?", (a["id"],)).fetchone()
        held = sum(p["value_base"] or 0 for p in positions
                   if p["account_id"] == a["id"])
        accounts.append({"id": a["id"], "broker": a["broker"],
                         "ccy": a["base_currency"],
                         "cash": float(_d(cash)),
                         "positions_value": held,
                         "equity": float(_d(cash)) + held})
    return {"exchanges": exchanges, "closed": closed, "accounts": accounts,
            "totals": totals_by_currency(positions, accounts),
            # one number only when there is one currency to put it in
            "total_value": (float(total_value)
                            if len({p["base_currency"] for p in positions})
                            <= 1 else None),
            "position_count": len(positions)}


def totals_by_currency(positions, accounts) -> list:
    """Market value, unrealized P/L, cash and equity — one row per account
    currency, never added across them. The page used to show a single
    "market value" and "equity" that summed SGD and USD accounts as if they
    were the same money (REVIEW-2026-11-11 B5)."""
    rows = {}

    def row(ccy):
        return rows.setdefault(ccy, {
            "ccy": ccy, "value": Decimal(0), "unrealized": Decimal(0),
            "cash": Decimal(0), "equity": Decimal(0), "positions": 0,
            "unpriced": 0, "accounts": 0})
    for p in positions:
        r = row(p["base_currency"])
        r["positions"] += 1
        if p.get("value_base") is None:
            r["unpriced"] += 1          # its value is missing from the sums
            continue
        r["value"] += Decimal(str(p["value_base"]))
        r["unrealized"] += Decimal(str(p["unrealized_base"]))
    for a in accounts:
        r = row(a["ccy"])
        r["accounts"] += 1
        r["cash"] += Decimal(str(a["cash"]))
        r["equity"] += Decimal(str(a["equity"]))
    return [{k: (float(v) if isinstance(v, Decimal) else v)
             for k, v in rows[c].items()} for c in sorted(rows)]


def chart_for(conn, market, instrument_id, *, range_="5d", interval="60m"):
    """Price history plus everything of yours worth drawing on it."""
    inst = conn.execute("SELECT * FROM instruments WHERE id=?",
                        (instrument_id,)).fetchone()
    if inst is None:
        return {"error": "unknown instrument"}
    symbol = yahoo_symbol(inst["ticker"], inst["exchange"])
    out = {"instrument_id": instrument_id, "symbol": symbol,
           "ticker": inst["ticker"], "exchange": inst["exchange"],
           "range": range_, "interval": interval}
    try:
        out["chart"] = market.chart(symbol, range_, interval)
    except Exception as e:                      # noqa: BLE001 — honest blank
        out["chart"] = None
        out["chart_error"] = str(e)[:140]
    pos = next((p for p in open_positions(conn)
                if p["instrument_id"] == instrument_id), None)
    fills = fills_for(conn, pos["account_id"], instrument_id) if pos else []
    out["fills"] = fills
    out["levels"] = levels_for(conn, pos, fills) if pos else {}
    return out
