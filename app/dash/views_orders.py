"""Read-only views of money: portfolio, orders, trades, broker sync and the Saxo account page.

Methods of the request handler, as a mixin: `self.ctx` is the server's Ctx
(app/dash/ctx.py), `self._send` and friends come from app/dash/handler.py.
Moved out of the create_server closure in app/dashboards.py unchanged except
that closure variables are now `self.ctx.<name>` (REVIEW-2026-11-11 §3.2)."""
import json
import logging
import re


from ..accounts import sim_label
from .common import HTTPError
from .common import session_facts

log = logging.getLogger(__name__)


class OrderViews:
    def _portfolio(self):
        from .. import portfolio
        data = portfolio.build(self.ctx.conn, self.ctx.market, clock=self.ctx.clock, cal=self.ctx.cal)
        data["alerts"] = [dict(r) for r in self.ctx.conn.execute(
            "SELECT * FROM price_alerts ORDER BY instrument_id")]
        for a in data["accounts"]:
            a["realized_pl"] = self.ctx.repo.realized_pl(a["id"]) / 1e6
        return data

    def _orders(self):
        """Everything still outstanding: entry orders (queued by the desk
        or working at the broker), the protective stop/target legs that
        guard open positions, and price triggers — for the Orders page."""
        from .. import price_triggers
        from ..tools.market import yahoo_symbol
        accts = {a["id"]: dict(a) for a in self.ctx.conn.execute(
            "SELECT id, broker, environment FROM broker_accounts")}
        rows = [dict(r) for r in self.ctx.conn.execute(
            "SELECT o.id, o.work_item_id, o.account_id, o.side,"
            " o.qty/1e6 qty, o.limit_price/1e6 limit_price,"
            " o.stop_loss/1e6 stop_loss, o.take_profit/1e6 take_profit,"
            " o.status, o.broker_order_id, o.created_at, o.placed_at,"
            " o.expires_at, o.leg, o.parent_order_id, o.submitted_at,"
            " o.instrument_id, i.ticker, i.exchange, i.currency,"
            " o.trail_pct order_trail_pct,"
            " (SELECT COALESCE(SUM(e.qty),0)/1e6 FROM executions e"
            "  WHERE e.order_id=o.id) filled"
            " FROM orders o JOIN instruments i ON i.id=o.instrument_id"
            " WHERE o.status IN ('pending_session','placed',"
            " 'partially_filled') ORDER BY o.created_at DESC")]
        for r in rows:
            a = accts.get(r["account_id"]) or {}
            r["broker"] = a.get("broker")
            # a buy's trailing alert (a watcher rule on the position, not
            # part of the order): shown and editable on the Orders page
            ta = self.ctx.conn.execute(
                "SELECT threshold FROM price_alerts WHERE rule='trail_pct'"
                " AND instrument_id=? AND account_id=? AND side=1",
                (r["instrument_id"], r["account_id"])).fetchone() \
                if r["side"] == "buy" and not r["leg"] else None
            r["trail_pct"] = float(ta[0]) if ta else None
            r["account"] = self._account_label(a.get("broker"),
                                               a.get("environment"))
            r["symbol"] = yahoo_symbol(r["ticker"], r["exchange"])
            # is the stop/target held AT SAXO? Otherwise (the built-in
            # sims, and the markets saxo-paper simulates locally — SGX:
            # Saxo SIM lists those only as CFDs) Claire's sim holds it
            # and the custodian fills it against the live price
            from ..tools.saxo_broker import SIM_LOCAL_SUFFIXES
            suf = r["symbol"].rsplit(".", 1)[1] if "." in r["symbol"] \
                else ""
            r["legs_at_broker"] = bool(
                (r["broker"] or "").startswith("saxo") and not (
                    r["broker"] == "saxo" and suf in SIM_LOCAL_SUFFIXES))
            r["where"] = ("queued — sent when the market opens"
                          if r["status"] == "pending_session" and
                          not r["submitted_at"] else
                          "being sent to the broker"
                          if r["status"] == "pending_session" else
                          "part filled at the broker"
                          if r["status"] == "partially_filled" else
                          "working at the broker")
            # Claire: where Saxo holds the stop, the trail % is a real
            # trailing stop (the entry's order row carries it, and its
            # stop leg trails by trail % x the entry's limit)
            r["trail_is_stop"] = False
            if r["leg"] == "stop" and r["parent_order_id"] and \
                    r["legs_at_broker"]:
                from ..graph.nodes_execution import trail_distance
                p = self.ctx.conn.execute("SELECT trail_pct, stop_loss,"
                                 " limit_price FROM orders WHERE id=?",
                                 (r["parent_order_id"],)).fetchone()
                d = trail_distance(p) if p else None
                if d:
                    r.update(trail_pct=p["trail_pct"], trails=float(d),
                             trail_is_stop=True)
            elif not r["leg"] and r["order_trail_pct"] and \
                    r["legs_at_broker"] and r["stop_loss"]:
                r.update(trail_pct=r["order_trail_pct"],
                         trail_is_stop=True)
        trig = price_triggers.list_triggers(
            self.ctx.conn, since=int(self.ctx.clock()) - 7 * 86400, limit=200)
        syms = sorted({r["symbol"] for r in rows} |
                      {yahoo_symbol(t["ticker"], t["exchange"])
                       for t in trig if t["status"] == "active"})
        quotes = {}
        if syms:
            try:
                quotes = {s: {"price": float(v["price"]),
                              "stale": v.get("stale")}
                          for s, v in self.ctx.market.spark(syms).items()
                          if v.get("price") is not None}
            except Exception:               # noqa: BLE001 — best effort
                quotes = {}
        for t in trig:
            t["symbol"] = yahoo_symbol(t["ticker"], t["exchange"])
            a = accts.get(t["account_id"]) or {}
            t["account"] = self._account_label(a.get("broker"),
                                               a.get("environment"))
        for r in rows:
            q = quotes.get(r["symbol"]) or {}
            r["last"] = q.get("price")
            r.update(session_facts(r["exchange"], self.ctx.cal_or_default(),
                                   int(self.ctx.clock())))
            if r["leg"] and r["parent_order_id"]:
                # what the shares cost, so the page can say what the
                # leg would make or lose if it fills
                f = self.ctx.conn.execute(
                    "SELECT SUM(1.0*qty*price_native)/SUM(qty)/1e6"
                    " FROM executions WHERE order_id=?",
                    (r["parent_order_id"],)).fetchone()
                r["entry_price"] = f[0] if f else None
        return {"entries": [r for r in rows if not r["leg"]],
                "legs": [r for r in rows if r["leg"]],
                "triggers": trig, "quotes": quotes, "now": int(self.ctx.clock()),
                "sync": self._broker_sync()}

    def _broker_sync(self):
        """When the custodian last checked each broker account, and last
        reached its venue — the Orders page's sync line."""
        last = self.ctx.conn.execute(
            "SELECT checked_at, ok FROM service_health WHERE"
            " service='custodian' ORDER BY checked_at DESC LIMIT 1"
        ).fetchone()
        sched = self.ctx.conn.execute(
            "SELECT spec, enabled FROM schedules WHERE job='custodian'"
        ).fetchone()
        every = None
        if sched:
            try:
                every = json.loads(sched["spec"]).get("minutes")
            except (TypeError, ValueError):
                pass
        accounts = []
        for a in self.ctx.conn.execute(
                "SELECT a.id, a.broker, a.environment, s.venue,"
                " s.checked_at, s.contact_at, s.ok, s.orders_checked,"
                " s.error FROM broker_accounts a LEFT JOIN broker_sync s"
                " ON s.account_id=a.id ORDER BY a.id"):
            d = dict(a)
            d["label"] = self._account_label(a["broker"],
                                             a["environment"])
            accounts.append(d)
        return {"custodian_at": last["checked_at"] if last else None,
                "custodian_ok": last["ok"] if last else None,
                "every_min": every,
                "enabled": bool(sched["enabled"]) if sched else None,
                "accounts": accounts}

    def _trades(self, days):
        """Every fill in the book — buys and sells, including the ones a
        stop or target made at the broker while nobody was looking — with
        the profit or loss and why the trade happened (Trades page)."""
        from ..tools.market import yahoo_symbol
        since = int(self.ctx.clock()) - max(1, min(days, 3650)) * 86400
        accts = {a["id"]: dict(a) for a in self.ctx.conn.execute(
            "SELECT id, broker, environment, base_currency"
            " FROM broker_accounts")}
        rows = [dict(r) for r in self.ctx.conn.execute(
            "SELECT e.id, e.order_id, e.account_id, e.side,"
            " e.qty/1e6 qty, e.price_native/1e6 price, e.currency,"
            " e.commission/1e6 fees, e.other_fees/1e6 other_fees,"
            " e.gross_base/1e6 gross, e.net_base/1e6 net,"
            " e.executed_at, e.work_item_id, i.ticker, i.exchange,"
            " i.name, o.leg, o.limit_price/1e6 order_limit,"
            " o.stop_loss/1e6 order_stop, o.parent_order_id"
            " FROM executions e JOIN instruments i ON i.id=e.instrument_id"
            " LEFT JOIN orders o ON o.id=e.order_id"
            " WHERE e.executed_at >= ? ORDER BY e.executed_at DESC",
            (since,))]
        work = {}

        def item(wi):
            if wi and wi not in work:
                r = self.ctx.conn.execute("SELECT * FROM work_items WHERE id=?",
                                 (wi,)).fetchone()
                work[wi] = dict(r) if r else None
            return work.get(wi) if wi else None

        def thesis(w):
            try:
                return json.loads((w or {}).get("thesis_json") or "{}")
            except ValueError:
                return {}

        def analysts(wi):
            out = []
            for rep in self.ctx.conn.execute(
                    "SELECT agent_id, payload FROM agent_reports WHERE"
                    " work_item_id=? AND kind='analyst' ORDER BY id",
                    (wi,)):
                try:
                    s = json.loads(rep["payload"]).get("summary")
                except (ValueError, AttributeError):
                    s = None
                if s:
                    out.append(f"{rep['agent_id']}: {s}")
            return out

        def verdict(wi):
            r = self.ctx.conn.execute(
                "SELECT payload FROM agent_reports WHERE work_item_id=?"
                " AND agent_id='arbiter' ORDER BY id DESC LIMIT 1",
                (wi,)).fetchone()
            try:
                return json.loads(r["payload"]) if r else {}
            except ValueError:
                return {}

        def approved_at(wi):
            r = self.ctx.conn.execute(
                "SELECT MIN(ts) FROM events WHERE item_id=? AND"
                " to_state='approved'", (wi,)).fetchone()
            return r[0] if r else None

        def reason(t):
            w = item(t["work_item_id"]) or {}
            th, kind = thesis(w), w.get("kind")
            why, details = "", []
            if t["leg"] == "stop":
                why = ("Stop-loss hit — the protective stop "
                       + (f"at {t['order_stop']:g} " if t["order_stop"]
                          else "")
                       + "sold the shares at the broker automatically")
            elif t["leg"] == "target":
                why = ("Target reached — the take-profit order "
                       + (f"at {t['order_limit']:g} " if t["order_limit"]
                          else "")
                       + "sold the shares at the broker automatically")
            elif kind == "pipeline":
                conv = th.get("conviction")
                why = ("Claire's desk recommended this "
                       f"{th.get('direction') or t['side']}"
                       + (f" ({conv:.0%} conviction)" if conv else "")
                       + " and you approved it")
                details = analysts(w["id"])
            elif kind == "override":
                origin = w.get("override_of")
                v = verdict(origin) if origin else {}
                why = ("The desk passed on it"
                       + (f" ({v['conviction']:.0%} conviction)"
                          if v.get("conviction") else "")
                       + " — you overrode it and approved the trade")
                ev = self.ctx.conn.execute(
                    "SELECT payload FROM events WHERE item_id=? AND"
                    " to_state='override'", (w["id"],)).fetchone()
                try:
                    note = json.loads(ev["payload"] or "{}").get("note") \
                        if ev else None
                except ValueError:
                    note = None
                if note:
                    details.append(f"your note: {note}")
                details += analysts(origin) if origin else []
            elif kind == "sell_review":
                why = ("Sell review after a price alert, approved by you"
                       + (f" — {w['trigger']}" if w.get("trigger")
                          else ""))
                details = analysts(w["id"])
            elif kind == "manual":
                cond = (th.get("conditions") or [""])[0]
                why = (re.sub(r"^MANUAL (\w+)", lambda m: "Your manual "
                              + m.group(1).lower(), cond) if cond else
                       (w.get("trigger") or "your manual trade"))
                why += " — approved by you"
            elif w:
                why = w.get("trigger") or kind or ""
            else:
                why = "not linked to a Claire run (booked from the broker)"
            if t["leg"]:
                parent = item(t["work_item_id"]) or {}
                details.append("protecting the buy from run "
                               + (parent.get("id") or "?"))
            return why, details, approved_at(w["id"]) if w else None

        # profit / loss: sells from their lot closures, buys from their lot
        open_syms = set()
        for t in rows:
            a = accts.get(t["account_id"]) or {}
            t["account"] = self._account_label(a.get("broker"),
                                               a.get("environment"))
            t["base_currency"] = a.get("base_currency")
            t["symbol"] = yahoo_symbol(t["ticker"], t["exchange"])
            t["reason"], t["details"], t["approved_at"] = reason(t)
            cl = self.ctx.conn.execute(
                "SELECT COUNT(*) n, SUM(realized_pl_base)/1e6 pl,"
                " SUM(cost_base)/1e6 cost, SUM(qty)/1e6 q,"
                " MAX(holding_days) held FROM lot_closures"
                " WHERE close_execution_id=?", (t["id"],)).fetchone()
            lot = self.ctx.conn.execute(
                "SELECT id, qty_opened/1e6 opened,"
                " qty_remaining/1e6 remaining,"
                " cost_per_share_base/1e6 cost FROM lots"
                " WHERE open_execution_id=?", (t["id"],)).fetchone()
            t["closes"] = None
            t["lot"] = None
            if cl and cl["n"]:
                t["closes"] = {"pl": cl["pl"], "cost": cl["cost"],
                               "qty": cl["q"], "held_days": cl["held"],
                               "pct": (cl["pl"] / cl["cost"] * 100
                                       if cl["cost"] else None)}
            if lot:
                rz = self.ctx.conn.execute(
                    "SELECT SUM(realized_pl_base)/1e6, SUM(qty)/1e6,"
                    " GROUP_CONCAT(close_execution_id) FROM lot_closures"
                    " WHERE lot_id=?", (lot["id"],)).fetchone()
                t["lot"] = {"opened": abs(lot["opened"]),
                            "remaining": lot["remaining"],
                            "cost": lot["cost"],
                            "realized": rz[0], "closed_qty": rz[1],
                            "closed_by": (rz[2] or "").split(",")
                            if rz[2] else []}
                if lot["remaining"] > 0:
                    open_syms.add(t["symbol"])
        quotes = {}
        if open_syms:
            try:
                quotes = {s: float(v["price"]) for s, v in
                          self.ctx.market.spark(sorted(open_syms)).items()
                          if v.get("price") is not None}
            except Exception:               # noqa: BLE001 — best effort
                quotes = {}
        # money is totalled per ACCOUNT currency, never across them:
        # one "realized P/L" used to add SGD and USD trades (B5)
        summary = {"by_ccy": [], "wins": 0, "losses": 0, "buys": 0,
                   "sells": 0, "priced": True}
        money = {}

        def tot(t):
            ccy = t["base_currency"] or t["currency"] or "?"
            return money.setdefault(ccy, {
                "ccy": ccy, "realized": 0.0, "unrealized": 0.0,
                "fees": 0.0})
        for t in rows:
            tot(t)["fees"] += (t["fees"] or 0) + (t["other_fees"] or 0)
            summary["buys" if t["side"] == "buy" else "sells"] += 1
            if t["closes"]:
                pl = t["closes"]["pl"] or 0
                tot(t)["realized"] += pl
                if pl:                  # break-even is neither
                    summary["wins" if pl > 0 else "losses"] += 1
            lot = t["lot"]
            if lot and lot["remaining"] > 0:
                last = quotes.get(t["symbol"])
                lot["last"] = last
                if last is not None:
                    try:
                        fx = float(self.ctx.market.fx(t["currency"],
                                             t["base_currency"] or
                                             t["currency"]))
                    except Exception:       # noqa: BLE001
                        fx = 1.0 if t["currency"] == \
                            t["base_currency"] else None
                    if fx is not None:
                        sign = 1 if t["side"] == "buy" else -1
                        lot["unrealized"] = sign * (
                            last * fx - lot["cost"]) * lot["remaining"]
                        tot(t)["unrealized"] += lot["unrealized"]
                else:
                    summary["priced"] = False
        summary["by_ccy"] = [money[c] for c in sorted(money)]
        return {"trades": rows, "summary": summary, "days": days,
                "now": int(self.ctx.clock())}

    @staticmethod
    def _account_label(broker, env):
        if broker == "saxo":
            return "Saxo SIM (paper)"
        if broker in ("alpaca", "moomoo") and env != "live":
            return sim_label(broker)
        name = {"saxo_live": "Saxo", "alpaca": "Alpaca",
                "moomoo": "moomoo"}.get(broker, broker or "broker")
        if env == "live":
            return f"{name} LIVE (real money)"
        return f"{name} {env or ''}".strip()

    def _desk_held_on(self, broker, ticker, exchange):
        """Shares the desk's book holds on the account behind `broker`."""
        r = self.ctx.conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN l.qty_opened<0 THEN"
            " -l.qty_remaining ELSE l.qty_remaining END),0) q FROM lots l"
            " JOIN instruments i ON i.id=l.instrument_id JOIN"
            " broker_accounts a ON a.id=l.account_id WHERE a.broker=? AND"
            " i.ticker=? AND i.exchange=? AND l.qty_remaining>0",
            (broker, ticker, exchange)).fetchone()
        return (r["q"] or 0) / 1e6

    def _position_floor(self, body):
        from ..watcher import set_position_floor
        inst = str(body.get("instrument_id") or "")
        acct = str(body.get("account_id") or "")
        side = -1 if body.get("side") == "short" else 1
        try:
            pct = float(body.get("pct"))
        except (TypeError, ValueError):
            raise HTTPError(422, "the trailing alert must be a number")
        if not 0.5 <= pct <= 90:
            raise HTTPError(422, "the trailing alert must be between"
                                 " 0.5% and 90%")
        held = self.ctx.conn.execute(
            "SELECT COUNT(*) FROM lots WHERE instrument_id=? AND"
            " account_id=? AND qty_remaining > 0 AND"
            " (qty_opened > 0) = ?", (inst, acct, side > 0)).fetchone()[0]
        if not held:
            raise HTTPError(404, "the desk holds no such position")
        before = set_position_floor(self.ctx.conn, inst, acct, pct, side=side)
        return {"ok": True, "pct": pct, "before": before}

    def _saxo_account(self, which):
        """One Saxo account straight from Saxo (read-only), plus each
        position's share of gross exposure for the allocation bars."""
        from ..tools import saxo_live
        out = saxo_live.summary(which, with_orders=True, env=self.ctx.env or {})
        out["signin"] = saxo_live.status(which, clock=self.ctx.clock)
        pos = out.get("positions") or []
        from ..tools.saxo_broker import desk_symbol
        for p in pos:
            # the desk's market-data symbol, so the page can chart it
            p["chart_symbol"] = desk_symbol(p.get("symbol"))
        self._fill_missing_prices(pos, (out.get("balance") or {})
                                  .get("Currency"))
        gross = sum(abs(p.get("exposure_base") or 0) for p in pos)
        for p in pos:
            p["weight_pct"] = (round(abs(p.get("exposure_base") or 0)
                                     / gross * 100, 1) if gross else None)
        # the Sell button is offered only for what the DESK bought on
        # this very account; the rest is Richard's own holding
        broker = "saxo_live" if which == "live" else "saxo"
        from ..portfolio import open_positions
        # saxo-paper only trades AT Saxo SIM when SIM execution is on;
        # otherwise its book lives in the in-process sim, not here
        at_saxo = which == "live" or (
            (self.ctx.env or {}).get("SAXO_SIM_EXECUTION") or "").lower() \
            == "enabled"
        mine = {(q["symbol"]): q for q in open_positions(self.ctx.conn)
                if q["broker"] == broker} if at_saxo else {}
        for p in pos:
            d = mine.get(p.get("chart_symbol"))
            p["desk"] = {"held": d["qty"], "account_id": d["account_id"],
                         "broker": broker, "ticker": d["ticker"],
                         "exchange": d["exchange"]} if d else None
        out["desk_broker"] = broker
        return out

    def _fill_missing_prices(self, pos, acct_ccy):
        """Saxo sends CurrentPrice 0 where the account has no market data
        for that exchange (the live account and ASX, 2026-11-06: PRO
        shown at 0.00, a "-100%" loss, while Saxo's own total valued it
        at the real ~0.065). A zero price is no price: use the desk's own
        quote, and re-work P/L from it — marked, never passed off as
        Saxo's."""
        for p in pos:
            if p.get("current_price") or not p.get("amount") or \
                    not p.get("chart_symbol"):
                continue
            try:
                q = self.ctx.market.spark([p["chart_symbol"]]).get(
                    p["chart_symbol"]) or {}
                cur = float(q["price"])
            except Exception:               # noqa: BLE001 — leave as is
                p["price_missing"] = True
                continue
            avg, amt = p.get("avg_open_price") or 0, p["amount"]
            # Is this a quote for the SAME share? A source that does not
            # know the symbol can answer for a namesake on another exchange
            # (a US "PRO" for ASX's PRO.AX): 20,000 shares of a 6-cent stock
            # priced at over a hundred dollars showed a quarter-million
            # "profit" on a 2,000 account. A different currency is not
            # believed; nor is a price more than fifty times away from what
            # was paid (a real holding here is down 13× — that must still
            # show — but 150× is another company).
            q_ccy, p_ccy = q.get("currency"), p.get("currency")
            wrong_ccy = bool(q_ccy and p_ccy and str(q_ccy).upper()
                             != str(p_ccy).upper())
            far = bool(avg) and cur > 0 and not (0.02 <= cur / avg <= 50)
            if cur <= 0 or wrong_ccy or far:
                p["price_missing"] = True
                p["price_note"] = (
                    f"the desk's quote ({cur:g} {q_ccy or ''}) does not look "
                    "like this share — not used").replace("  ", " ")
                continue
            p["price_missing"] = False
            p["current_price"] = cur
            p["market_value"] = round(amt * cur, 2)
            p["price_src"] = q.get("src") or "desk quote"
            p["pnl"] = round((cur - avg) * amt, 2) if avg else None
            p["pnl_pct"] = (round((cur - avg) / avg * 100 *
                                  (1 if amt >= 0 else -1), 2)
                            if avg else None)
            try:
                fx = float(self.ctx.market.fx(p.get("currency") or acct_ccy,
                                     acct_ccy)) if acct_ccy else None
            except Exception:               # noqa: BLE001
                fx = None
            if fx and p["pnl"] is not None:
                p["pnl_base"] = round(p["pnl"] * fx, 2)
                p["exposure_base"] = round(abs(amt * cur * fx), 2)
