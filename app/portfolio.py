"""Portfolio assembly: what you own, what you paid, what it is worth now,
and the levels that matter (your average cost, the thesis stop and target,
the watcher's alert).

Everything personal comes from desk.db — the book is the source of truth for
quantity, price paid and P&L; market data only supplies the live price and
the usual trader metrics. Market lookups are best-effort: a throttled source
degrades a card to "price unavailable", it never hides a position you hold.
"""
import json
import os
import time
from decimal import Decimal

from . import capabilities

from .accounting.money import to_micro
from .accounting.repo import Repo
from .tools.market import yahoo_symbol

MICRO = Decimal(1_000_000)


def _d(v) -> Decimal:
    return Decimal(v or 0) / MICRO


def open_positions(conn) -> list[dict]:
    """One row per (account, instrument) with the book's own numbers."""
    rows = []
    for p in conn.execute(
            "SELECT l.account_id, l.instrument_id, i.ticker, i.exchange,"
            " i.currency, a.broker, a.environment, a.base_currency,"
            " a.fee_model,"
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
            "environment": p["environment"],
            "base_currency": p["base_currency"],
            "instrument_id": p["instrument_id"], "ticker": p["ticker"],
            "exchange": p["exchange"], "currency": p["currency"],
            "symbol": yahoo_symbol(p["ticker"], p["exchange"]),
            "qty": float(qty),
            "side": "short" if qty < 0 else "long",
            "cost_base": float(cost_base),
            "avg_cost_base": float(cost_base / qty) if qty else None,
            "commissions": float(_d(p["commissions"])),
            "fee_model": json.loads(p["fee_model"] or "{}"),
            "opened_at": p["opened_at"]})
    return rows


def exit_fee_estimate(fee_model, value_base) -> Decimal:
    """What closing the whole position at this value would cost in fees, by
    the account's own fee model — the same sum the ledger charges a fill.
    An estimate: the real fee is whatever is charged on the day."""
    try:
        return _d(Repo._fee_for(fee_model or {},
                                to_micro(str(abs(Decimal(str(value_base)))))))
    except Exception:                           # noqa: BLE001 — odd model
        return Decimal(0)


def after_fees(p, value, cost):
    """Profit / loss the way it would land if the position were closed now:
    the price gain, less the fees already paid to open it (still attached to
    the open lots), less the estimated fee to close it. This is what the
    ledger's realized P/L will subtract too (proceeds − cost − commission).
    The percentage is of everything paid in: the shares plus the buy fees."""
    paid = Decimal(str(p["commissions"]))
    closing = exit_fee_estimate(p.get("fee_model"), value)
    net = value - cost - paid - closing
    outlay = abs(cost) + paid
    return {"fees_paid_base": float(paid),
            "exit_fee_est_base": float(closing),
            "net_base": float(net),
            "net_pct": float(net / outlay * 100) if outlay else None}


# ── one position, or every position the book holds ──────────────────────
# Portfolio used to ask each question below once per position — about fifty
# statements for six positions, on a page that refreshes itself
# (REVIEW-2026-11-11 §4.3). Each is now ONE statement, whether it is asked
# about a single position (`key` = (account id, instrument id)) or, with no
# key, about every position the book still holds. The functions that take
# one position keep their names and give the answers they always gave.

_HELD = ("(SELECT DISTINCT account_id, instrument_id FROM lots"
         " WHERE qty_remaining > 0) held")


def _scope(alias, key):
    """(JOIN, WHERE, arguments) keeping `alias`'s rows to one position — or,
    with no key, to the positions that still have open lots."""
    if key is not None:
        return ("", f"{alias}.account_id=? AND {alias}.instrument_id=?",
                list(key))
    return (f" JOIN {_HELD} ON held.account_id = {alias}.account_id"
            f" AND held.instrument_id = {alias}.instrument_id", "1", [])


def _by_position(pairs) -> dict:
    out = {}
    for key, value in pairs:
        out.setdefault(key, []).append(value)
    return out


def _select_in(conn, sql, ids, args=()):
    """Rows of `sql` — which has one `{marks}` where the ids go — for any
    number of ids. SQLite caps the variables in a statement (999 on older
    builds), so they go in slices; a desk's positions fit in one."""
    ids, rows = list(ids), []
    for at in range(0, len(ids), 500):
        part = ids[at:at + 500]
        rows += conn.execute(sql.format(marks=",".join("?" * len(part))),
                             [*part, *args]).fetchall()
    return rows


def _fills(conn, key=None) -> list:
    """[(position key, fill)], oldest first."""
    join, where, args = _scope("e", key)
    return [((e["account_id"], e["instrument_id"]),
             {"ts": e["executed_at"], "side": e["side"],
              "qty": float(_d(e["qty"])),
              "price_native": float(_d(e["price_native"])),
              "commission": float(_d(e["commission"])),
              "fx_rate": float(_d(e["fx_rate"])),
              "work_item_id": e["work_item_id"]})
            for e in conn.execute(
                f"SELECT e.* FROM executions e{join} WHERE {where}"
                # two fills in the same second stay in the order they were
                # recorded — what the one-position lookup always returned,
                # because it walks the position's index
                " ORDER BY e.executed_at, e.rowid", args)]


def fills_for(conn, account_id, instrument_id) -> list[dict]:
    """Every fill on this position — what you actually paid, when. These
    become the markers on the chart."""
    return [f for _, f in _fills(conn, (account_id, instrument_id))]


def fills_by_position(conn) -> dict:
    """fills_for, for every position still held, in one statement:
    {(account id, instrument id): [fill, …]}."""
    return _by_position(_fills(conn))


def _held_avgs(conn, key=None) -> dict:
    """{(account id, instrument id, is it a short): average price paid for
    the shares STILL HELD, in the instrument's own currency}. None where a
    lot's fill has no FX rate to convert its cost back with."""
    where, args = ("l.account_id=? AND l.instrument_id=?", list(key)) \
        if key is not None else ("1", [])
    sums = {}
    for r in conn.execute(
            "SELECT l.account_id, l.instrument_id, l.qty_opened < 0 AS short,"
            " l.qty_remaining AS q, l.cost_per_share_base AS c,"
            " e.fx_rate AS fx FROM lots l JOIN executions e"
            f" ON e.id = l.open_execution_id WHERE {where}"
            " AND l.qty_remaining > 0"
            # oldest lot first within a position, as the one-position
            # lookup read them
            " ORDER BY l.account_id, l.instrument_id, l.opened_at, l.rowid",
            args):
        s = sums.setdefault((r["account_id"], r["instrument_id"],
                             bool(r["short"])),
                            {"value": Decimal(0), "qty": Decimal(0),
                             "ok": True})
        if not r["fx"]:
            s["ok"] = False                 # cannot convert back: keep fills
            continue
        # cost_per_share_base is native price × the fill's FX rate
        s["value"] += Decimal(r["q"]) * Decimal(r["c"]) / Decimal(r["fx"])
        s["qty"] += Decimal(r["q"])
    return {k: (float(s["value"] / s["qty"]) if s["ok"] and s["qty"]
                else None) for k, s in sums.items()}


def open_lots_avg_native(conn, position, short=False):
    """Average price paid, in the instrument's own currency, for the shares
    STILL HELD — from the open lots, not from every opening fill. None when
    the position has no open lots on record (e.g. a chart for a name the
    desk does not hold)."""
    if not position or not position.get("account_id"):
        return None
    avgs = _held_avgs(conn, (position["account_id"],
                             position["instrument_id"]))
    return next((avg for (_, _, is_short), avg in avgs.items()
                 if is_short == bool(short)), None)


def _opening_fills(position, fills):
    """(is it a short, the fills that OPENED this position): buys for a
    long, sells for a short."""
    short = (position or {}).get("qty", 0) < 0
    return short, [f for f in fills
                   if f["side"] == ("sell" if short else "buy")]


def _thesis_run(opens):
    """The run behind the latest opening fill that had one."""
    return next((f["work_item_id"] for f in reversed(opens)
                 if f["work_item_id"]), None)


def _alert_for(alerts, position, short):
    """This position's own rule (instrument, account, side); a legacy row
    with no account/side applies to any position in the name. Its own rule
    comes before a legacy one, and the newest before an older."""
    acct, side = position.get("account_id"), -1 if short else 1
    mine = [a for a in alerts
            if a["instrument_id"] == position["instrument_id"]
            and (a["account_id"] is None or a["account_id"] == acct)
            and (a["side"] is None or a["side"] == side)]
    return min(mine, key=lambda a: (a["account_id"] is None, -a["id"]),
               default=None)


def _today(now=None) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(
        now if now is not None else time.time()))


def _fired_today(alert, today) -> int:
    return alert["fire_count_today"] or 0 \
        if alert["fire_count_date"] == today else 0


def _level_rows(conn, asked, today, key=None, held=True) -> dict:
    """What levels need from the database for these positions — `asked` is
    [(position, its fills)] — in four statements at most, however many
    positions there are: the average of the open lots, the thesis of each
    position's run, the armed trailing alerts, and the sell reviews started
    by the alerts that have fired today. `key` narrows it to one position."""
    alerts = conn.execute(
        "SELECT id, instrument_id, account_id, side, rule, threshold,"
        " peak_base, last_fired_at, fire_count_today, fire_count_date"
        " FROM price_alerts WHERE armed=1 AND rule='trail_pct'"
        + (" AND instrument_id=?" if key is not None else ""),
        [key[1]] if key is not None else []).fetchall()
    runs, fired = set(), {}
    for position, fills in asked:
        short, opens = _opening_fills(position, fills)
        run = _thesis_run(opens)
        if run:
            runs.add(run)
        alert = _alert_for(alerts, position, short)
        if alert and _fired_today(alert, today):
            # the earliest moment a review of this name could count for any
            # of its positions; each position applies its own below
            since = (alert["last_fired_at"] or 0) - 120
            iid = position["instrument_id"]
            fired[iid] = min(since, fired.get(iid, since))
    return {
        "held": _held_avgs(conn, key) if held else {},
        "theses": {r["id"]: r["thesis_json"] for r in _select_in(
            conn, "SELECT id, thesis_json FROM work_items"
                  " WHERE id IN ({marks})", sorted(runs))},
        "alerts": alerts,
        "reviews": _select_in(
            conn, "SELECT i.id AS instrument_id, w.id AS id, w.state,"
                  " w.thesis_json, w.created_at FROM work_items w"
                  " JOIN instruments i ON i.ticker=w.ticker WHERE"
                  " i.id IN ({marks}) AND w.kind='sell_review'"
                  " AND w.created_at>=?"
                  # newest first; of two started in the same second, the
                  # one recorded first — as the one-position lookup chose
                  " ORDER BY w.created_at DESC, w.rowid",
            sorted(fired), [min(fired.values())]) if fired else []}


def _levels(position, fills, fx, today, rows) -> dict:
    """levels_for, from rows already fetched (_level_rows) — no SQL here."""
    out = {"avg_cost_native": None, "stop_loss": None, "take_profit": None,
           "entry_low": None, "entry_high": None, "alert": None,
           "work_item_id": None}
    # the fills that OPENED this position: buys for a long, sells for a short
    short, opens = _opening_fills(position, fills)
    if opens:
        qty = sum(f["qty"] for f in opens)
        if qty:
            out["avg_cost_native"] = sum(
                f["price_native"] * f["qty"] for f in opens) / qty
    # ...but that is the average of everything ever bought. Once part of the
    # position has been sold, the shares still held are the LATER lots (FIFO)
    # and their average is what "you paid" means; the line used to stay at
    # the all-time average.
    held = rows["held"].get((position.get("account_id"),
                             position["instrument_id"], short))
    if held is not None:
        out["avg_cost_native"] = held
    wi_id = _thesis_run(opens)
    if wi_id:
        thesis = rows["theses"].get(wi_id)
        if thesis:
            try:
                t = json.loads(thesis)
                out.update({"stop_loss": t.get("stop_loss"),
                            "take_profit": t.get("take_profit"),
                            "entry_low": t.get("entry_low"),
                            "entry_high": t.get("entry_high"),
                            "work_item_id": wi_id})
            except ValueError:
                pass
    alert = _alert_for(rows["alerts"], position, short)
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
            fired = _fired_today(alert, today)
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
                since = (alert["last_fired_at"] or 0) - 120
                rv = next((r for r in rows["reviews"]
                           if r["instrument_id"] == position["instrument_id"]
                           and r["created_at"] >= since), None)
                if rv:
                    try:
                        verdict = json.loads(rv["thesis_json"] or "{}").get(
                            "direction")
                    except ValueError:
                        verdict = None
                    out["alert_review"] = {"id": rv["id"], "state": rv["state"],
                                           "verdict": verdict}
    return out


def levels_for(conn, position, fills, fx=None, now=None) -> dict:
    """The lines worth drawing: your average cost, the thesis stop/target
    that justified the trade, and the watcher's alert threshold — all in the
    instrument's own currency. `fx` is native → account base (current); the
    watcher stores its trailing peak in BASE currency, so it is converted
    back here (falling back to the rate at the last opening fill)."""
    today = _today(now)
    acct = (position or {}).get("account_id")
    key = (acct, position["instrument_id"])
    # no account on the position: nothing to say which lots are its own
    rows = _level_rows(conn, [(position, fills)], today, key,
                       held=bool(acct))
    # these are this position's lots whatever the caller's ids look like:
    # file them under the ids it gave
    rows["held"] = {(*key, short): avg
                    for (_, _, short), avg in rows["held"].items()}
    return _levels(position, fills, fx, today, rows)


def _legs(conn, key=None) -> list:
    """[(position key, leg)] — stops before targets, and each kind in the
    order it was placed. The entry each leg protects comes along in the same
    statement (it used to be looked up once per stop)."""
    from .graph.nodes_execution import trail_distance

    def trails(r):
        if r["leg"] != "stop" or not r["parent_order_id"] or \
                not str(r["broker_order_id"] or "").isdigit():
            return None                     # sim legs ("sim-…") never trail
        if r["parent_id"] is None:
            return None                     # the entry's row is gone
        d = trail_distance({"trail_pct": r["parent_trail_pct"],
                            "stop_loss": r["parent_stop_loss"],
                            "limit_price": r["parent_limit_price"]})
        return {"trails": float(d), "trail_pct": r["parent_trail_pct"]} if d \
            else None
    join, where, args = _scope("o", key)
    return [((r["account_id"], r["instrument_id"]),
             {"leg": r["leg"], "side": r["side"],
              "qty": float(_d(r["qty"])),
              "price": float(_d(r["stop_loss"] if r["leg"] == "stop"
                                else r["limit_price"])),
              "status": r["status"], "venue_order": r["broker_order_id"],
              **(trails(r) or {})})
            for r in conn.execute(
                "SELECT o.*, p.id AS parent_id,"
                " p.trail_pct AS parent_trail_pct,"
                " p.stop_loss AS parent_stop_loss,"
                " p.limit_price AS parent_limit_price"
                f" FROM orders o{join}"
                " LEFT JOIN orders p ON p.id = o.parent_order_id"
                f" WHERE {where} AND o.leg IS NOT NULL AND o.status IN"
                " ('placed','partially_filled') ORDER BY o.leg, o.rowid",
                args)]


def legs_for(conn, account_id, instrument_id) -> list[dict]:
    """The protective orders resting at the venue for this position (the
    bracket's stop-loss / take-profit, DESIGN.md §11a) — what will actually
    sell it if the price gets there, as opposed to the thesis levels.
    A stop whose entry carried a trail % on a venue that holds stops (Saxo)
    is a TRAILING stop there: `trails` is its distance."""
    return [leg for _, leg in _legs(conn, (account_id, instrument_id))]


def legs_by_position(conn) -> dict:
    """legs_for, for every position still held, in one statement:
    {(account id, instrument id): [leg, …]}."""
    return _by_position(_legs(conn))


def _realized(conn, key=None) -> dict:
    """{position key: realized P/L so far, in micro-units}; a position that
    has closed nothing is left out."""
    join, where, args = _scope("l", key)
    return {(r[0], r[1]): r[2] for r in conn.execute(
        "SELECT l.account_id, l.instrument_id,"
        " COALESCE(SUM(c.realized_pl_base),0) FROM lot_closures c"
        f" JOIN lots l ON l.id = c.lot_id{join} WHERE {where}"
        " GROUP BY l.account_id, l.instrument_id", args)}


def realized_for(conn, account_id, instrument_id) -> float:
    return float(_d(sum(_realized(conn, (account_id,
                                         instrument_id)).values())))


def realized_by_position(conn) -> dict:
    """realized_for, for every position still held, in one statement:
    {(account id, instrument id): realized P/L}; none closed → not listed."""
    return {key: float(_d(pl)) for key, pl in _realized(conn).items()}


def build(conn, market, *, clock, cal=None, env=None) -> dict:
    """The whole portfolio view: positions grouped by exchange, each with
    book numbers, live market metrics and the levels for its chart."""
    from datetime import datetime, timezone

    from . import sessions
    positions = open_positions(conn)
    symbols = sorted({p["symbol"] for p in positions})
    quotes = market.metrics(symbols) if symbols else {}
    now = datetime.fromtimestamp(int(clock()), tz=timezone.utc)

    # each kind of row once for the whole page, handed out below — not once
    # per position (REVIEW-2026-11-11 §4.3)
    realized = realized_by_position(conn)
    fills = fills_by_position(conn)
    legs = legs_by_position(conn)
    today = _today()
    level_rows = _level_rows(
        conn, [(p, fills.get((p["account_id"], p["instrument_id"]), []))
               for p in positions], today)

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
            p.update(after_fees(p, value, cost))
            total_value += value
        else:
            p["value_base"] = None
            p["unrealized_base"] = None
            p["unrealized_pct"] = None
            p.update(fees_paid_base=p["commissions"], exit_fee_est_base=None,
                     net_base=None, net_pct=None)
        p.pop("fee_model", None)
        key = (p["account_id"], p["instrument_id"])
        p["realized_base"] = realized.get(key, 0.0)
        p["fills"] = fills.get(key, [])
        p["levels"] = _levels(p, p["fills"], fx, today, level_rows)
        p["legs"] = legs.get(key, [])
        # which parts of its protection may be moved now that it is held —
        # the broker's row in capabilities.CAN_CHANGE; the pages offer an
        # edit only where this says yes, and the server checks it again
        p["can_change"] = capabilities.for_position(
            p, p["exchange"], env if env is not None else os.environ,
            p["legs"])
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

    # every account's cash in one statement (it was one per account)
    cash_by = dict(conn.execute(
        "SELECT account_id, COALESCE(SUM(amount_base),0)"
        " FROM cash_transactions GROUP BY account_id").fetchall())
    accounts = []
    for a in conn.execute("SELECT * FROM broker_accounts"):
        cash = cash_by.get(a["id"], 0)
        held = sum(p["value_base"] or 0 for p in positions
                   if p["account_id"] == a["id"])
        accounts.append({"id": a["id"], "broker": a["broker"],
                         "ccy": a["base_currency"],
                         "cash": float(_d(cash)),
                         "positions_value": held,
                         "equity": float(_d(cash)) + held})
    return {"exchanges": exchanges, "closed": closed, "accounts": accounts,
            "totals": totals_by_currency(positions, accounts),
            "by_share_currency": totals_by_share_currency(positions),
            # one number only when there is one currency to put it in
            "total_value": (float(total_value)
                            if len({p["base_currency"] for p in positions})
                            <= 1 else None),
            "position_count": len(positions)}


def totals_by_share_currency(positions) -> list:
    """The same holdings, in the currency each share is TRADED in — one row
    per share currency, whatever account holds them. `totals` is per account
    currency, so shares in Paris held by an SGD account are in its SGD line
    and there is no euro figure anywhere; this is where it is.

    `value` = price now × shares; `cost` = what was paid × shares; `gain` =
    the difference — the move in the share price alone, before fees and
    before any change in the exchange rate (those are in the account's
    currency, in `totals`). A holding with no price counts in `positions`
    and `unpriced`; one with no known cost leaves `cost` and `gain` alone."""
    rows = {}
    for p in positions:
        ccy = p.get("currency") or "?"
        r = rows.setdefault(ccy, {"ccy": ccy, "positions": 0, "unpriced": 0,
                                  "value": Decimal(0), "cost": Decimal(0),
                                  "gain": Decimal(0)})
        r["positions"] += 1
        price = (p.get("market") or {}).get("price")
        if price is None:
            r["unpriced"] += 1
            continue
        qty = Decimal(str(p["qty"]))
        value = Decimal(str(price)) * qty
        r["value"] += value
        avg = (p.get("levels") or {}).get("avg_cost_native")
        if avg is not None:
            cost = Decimal(str(avg)) * qty
            r["cost"] += cost
            r["gain"] += value - cost
    return [{k: (float(v) if isinstance(v, Decimal) else v)
             for k, v in rows[c].items()} for c in sorted(rows)]


def totals_by_currency(positions, accounts) -> list:
    """Market value, unrealized P/L (before fees, and `net` after them —
    see after_fees), cash and equity — one row per account
    currency, never added across them. The page used to show a single
    "market value" and "equity" that summed SGD and USD accounts as if they
    were the same money (REVIEW-2026-11-11 B5)."""
    rows = {}

    def row(ccy):
        return rows.setdefault(ccy, {
            "ccy": ccy, "value": Decimal(0), "unrealized": Decimal(0),
            "net": Decimal(0),
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
        r["net"] += Decimal(str(p.get("net_base", p["unrealized_base"])))
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
