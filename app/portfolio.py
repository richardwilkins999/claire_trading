"""Portfolio assembly: what you own, what you paid, what it is worth now,
and the levels that matter (your average cost, the thesis stop and target,
the watcher's alert).

Everything personal comes from desk.db — the book is the source of truth for
quantity, price paid and P&L; market data only supplies the live price and
the usual trader metrics. Market lookups are best-effort: a throttled source
degrades a card to "price unavailable", it never hides a position you hold.
"""
import json
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
            " SUM(l.qty_remaining*l.cost_per_share_base) AS cost_num,"
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
             "work_item_id": e["work_item_id"]}
            for e in conn.execute(
                "SELECT * FROM executions WHERE account_id=? AND"
                " instrument_id=? ORDER BY executed_at",
                (account_id, instrument_id))]


def levels_for(conn, position, fills) -> dict:
    """The lines worth drawing: your average cost, the thesis stop/target
    that justified the trade, and the watcher's alert threshold."""
    out = {"avg_cost_native": None, "stop_loss": None, "take_profit": None,
           "entry_low": None, "entry_high": None, "alert": None,
           "work_item_id": None}
    buys = [f for f in fills if f["side"] == "buy"]
    if buys:
        qty = sum(f["qty"] for f in buys)
        if qty:
            out["avg_cost_native"] = sum(
                f["price_native"] * f["qty"] for f in buys) / qty
    wi_id = next((f["work_item_id"] for f in reversed(buys)
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
    alert = conn.execute(
        "SELECT rule, threshold, peak_base FROM price_alerts"
        " WHERE instrument_id=? AND armed=1 AND rule='trail_pct' LIMIT 1",
        (position["instrument_id"],)).fetchone()
    if alert:
        # draw the floor where it ACTUALLY sits — trailed up from the best
        # price seen, not pinned to entry. Falls back to average cost until
        # the watcher's first tick has set a peak.
        ref = alert["peak_base"] or out["avg_cost_native"]
        if ref:
            out["alert"] = ref * (1 - alert["threshold"] / 100)
            out["alert_rule"] = (f"trailing −{alert['threshold']:.0f}% "
                                 f"of {ref:.2f}")
            out["alert_peak"] = alert["peak_base"]
    return out


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
        fx = Decimal(1)
        if price is not None and q.get("currency") and \
                q["currency"] != p["base_currency"]:
            try:
                fx = market.fx(q["currency"], p["base_currency"])
            except Exception:                   # noqa: BLE001
                fx = Decimal(1)
        if price is not None:
            value = Decimal(str(price)) * fx * Decimal(str(p["qty"]))
            p["value_base"] = float(value)
            p["unrealized_base"] = float(value - Decimal(str(p["cost_base"])))
            p["unrealized_pct"] = (float(
                (value - Decimal(str(p["cost_base"]))) /
                Decimal(str(p["cost_base"])) * 100)
                if p["cost_base"] else None)
            total_value += value
        else:
            p["value_base"] = None
            p["unrealized_base"] = None
            p["unrealized_pct"] = None
        p["realized_base"] = realized_for(conn, p["account_id"],
                                          p["instrument_id"])
        p["fills"] = fills_for(conn, p["account_id"], p["instrument_id"])
        p["levels"] = levels_for(conn, p, p["fills"])
        p["holding_days"] = (int(clock()) - (p["opened_at"] or
                                             int(clock()))) // 86400
        try:
            p["is_open"] = sessions.is_open(p["exchange"], now, cal)
            p["next_open"] = sessions.next_open(p["exchange"], now,
                                                cal).isoformat()
        except KeyError:
            p["is_open"], p["next_open"] = None, None

    for p in positions:
        p["weight_pct"] = (float(Decimal(str(p["value_base"])) /
                                 total_value * 100)
                           if p.get("value_base") and total_value else None)

    exchanges = {}
    for p in positions:
        exchanges.setdefault(p["exchange"], []).append(p)
    for ex, items in exchanges.items():
        items.sort(key=lambda x: -(x.get("value_base") or 0))

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
            "total_value": float(total_value),
            "position_count": len(positions)}


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
