"""Read-only views of runs and approvals: the run list and story, the approval cards and their archive.

Methods of the request handler, as a mixin: `self.ctx` is the server's Ctx
(app/dash/ctx.py), `self._send` and friends come from app/dash/handler.py.
Moved out of the create_server closure in app/dashboards.py unchanged except
that closure variables are now `self.ctx.<name>` (REVIEW-2026-11-11 §3.2)."""
import json
import logging
import re
from datetime import datetime
from datetime import timezone


from .. import approvals
from .. import exchanges as desk_exchanges
from .. import sessions
from ..accounts import _approval_account
from ..accounts import ordered_accounts
from ..levels import _levels
from ..levels import _range
from ..levels import quote_units
from ..logsetup import swallowed
from ..tools import tradingview
from ..tools.files import JailError
from .common import session_facts
from ..accounting import holdings

log = logging.getLogger(__name__)


class RunViews:
    def _reports_for(self, wi):
        """Every stored agent report for a run, in pipeline order."""
        out = []
        for rep in self.ctx.conn.execute(
                "SELECT agent_id, kind, payload FROM agent_reports"
                " WHERE work_item_id=? ORDER BY id", (wi,)):
            try:
                out.append({"agent": rep["agent_id"], "kind": rep["kind"],
                            "data": json.loads(rep["payload"])})
            except ValueError:
                pass
        return out

    def _approvals(self):
        paired = self._paired() or not self.ctx.dash_key
        cal = self.ctx.cal_or_default()
        nowdt = datetime.fromtimestamp(int(self.ctx.clock()), tz=timezone.utc)
        from .. import live_trading
        live = live_trading.status(self.ctx.conn, self.ctx.env or {})
        accounts = [_approval_account(a, self.ctx.repo, self.ctx.env or {}, live)
                    for a in ordered_accounts(self.ctx.conn, self.ctx.env)]
        if not any(a["environment"] == "live" for a in accounts):
            # always SHOW the real account, so it is obvious where it
            # is and why it cannot be chosen yet (§22)
            lid = (self.ctx.env or {}).get("SAXO_LIVE_ACCOUNT_ID") or ""
            accounts.append({
                "id": "saxo-live", "broker": "saxo_live", "ccy": "",
                "environment": "live", "cash": 0, "fee_model": {},
                "label": f"Saxo LIVE {lid} — real money".strip(),
                "disabled": "not set up yet (bin/claire live setup)"})
        cards = []
        now = int(self.ctx.clock())
        for r in self.ctx.conn.execute(
                "SELECT * FROM work_items WHERE state='awaiting_approval'"
                " ORDER BY created_at DESC"):
            if r["expires_at"] and now >= r["expires_at"]:
                continue                     # expiry on every read path §12
            inst = self.ctx.conn.execute(
                "SELECT exchange FROM instruments WHERE ticker=? LIMIT 1",
                (r["ticker"],)).fetchone()
            exchange = inst["exchange"] if inst else None
            session = {}
            if exchange and exchange in cal:
                session = {"exchange": exchange,
                           "is_open": sessions.is_open(exchange, nowdt,
                                                       cal),
                           "next_open": sessions.next_open(
                               exchange, nowdt, cal).isoformat()}
            # an override ran no analysts of its own — its evidence is the
            # verdict it overturned, which is exactly what must be read
            # before approving one
            # an override inherits the verdict it overturned; a sell
            # review inherits the analysis that opened the position. Both
            # must be readable before approving.
            reports = self._reports_for(r["id"])
            inherited = r["override_of"] or r["prior_run"]
            if inherited:
                reports = self._reports_for(inherited) + reports
            costs = self.ctx.conn.execute(
                "SELECT COALESCE(SUM(cost_usd),0) c, COUNT(*) n FROM"
                " agent_runs WHERE work_item_id=?", (r["id"],)).fetchone()
            cards.append({
                "id": r["id"], "kind": r["kind"], "ticker": r["ticker"],
                "exchange": exchange, "trigger": r["trigger"],
                "reports": reports, "override_of": r["override_of"],
                "prior_run": r["prior_run"],
                "llm_cost": costs["c"], "llm_calls": costs["n"],
                "created_at": r["created_at"],
                "thesis": json.loads(r["thesis_json"] or "{}"),
                # tokens ONLY for a paired browser (§10 rev2)
                "token": r["approval_token"] if paired else None,
                "locked": not paired,
                "session": session,
                "expires_at": r["expires_at"],
                # a manual trade's prefill (qty / size / account)
                "suggested": json.loads(r["suggested_json"] or "null"),
                # what the desk holds per account: a sell card prefills
                # its share count from it, and it says exit vs short
                "held": {a: float(q) for a, q in holdings.by_account(
                    self.ctx.conn, ticker=r["ticker"]).items()}})
        # a PASS never reaches the gate, so an empty Approvals page used
        # to be the only sign a run had concluded at all
        decided = []
        for r in self.ctx.conn.execute(
                "SELECT id, ticker, state, thesis_json, updated_at,"
                " trigger, override_of FROM work_items"
                " WHERE thesis_json IS NOT NULL"
                " AND state IN ('done','rejected','expired')"
                " AND updated_at > ? ORDER BY updated_at DESC LIMIT 12",
                (int(self.ctx.clock()) - 7 * 86400,)):
            try:
                t = json.loads(r["thesis_json"])
            except ValueError:
                continue
            # already overturned? then the override owns the decision and
            # offering a second one would double-buy the same thesis
            ovr = self.ctx.conn.execute(
                "SELECT id, state FROM work_items WHERE override_of=?"
                " AND state NOT IN ('rejected','expired','failed',"
                "'cancelled') ORDER BY created_at DESC LIMIT 1", (r["id"],)).fetchone()
            inst = self.ctx.conn.execute(
                "SELECT exchange FROM instruments WHERE ticker=? LIMIT 1",
                (r["ticker"],)).fetchone()
            decided.append({
                "id": r["id"], "ticker": r["ticker"], "state": r["state"],
                "exchange": inst["exchange"] if inst else None,
                "direction": t.get("direction"),
                "conviction": t.get("conviction"),
                "currency": t.get("currency"),
                "conditions": t.get("conditions") or [],
                # the same evidence the approval cards show, so a PASS can
                # be read on its merits rather than taken on trust
                "reports": self._reports_for(r["id"]),
                "overridable": (t.get("direction") == "pass"
                                and not r["override_of"] and not ovr),
                "overridden_by": ovr["id"] if ovr else None,
                "trigger": r["trigger"], "at": r["updated_at"]})
        failed = approvals.failed_approvals(self.ctx.conn, clock=self.ctx.clock)
        return {"cards": cards, "accounts": accounts, "decided": decided,
                "failed": failed,
                "live": {"armed": live["armed"],
                         "summary": live["summary"],
                         "limits": live["limits"],
                         "today": live["today"]}}

    def _runs(self):
        rows = []
        for r in self.ctx.conn.execute(
                "SELECT * FROM work_items WHERE archived_at IS NULL"
                " ORDER BY created_at DESC LIMIT 50"):
            d = dict(r)
            d.pop("approval_token", None)    # tokens live on cards only
            try:                             # plain-language facts for
                d["story"] = self._run_story(d)   # the Trading page;
            except Exception as e:           # noqa: BLE001 — never
                d["story"] = {"error": str(e)[:200]}  # break the list
            rows.append(d)
        return rows

    def _run_detail(self, wi):
        row = self.ctx.conn.execute("SELECT * FROM work_items WHERE id=?",
                           (wi,)).fetchone()
        if row is None:
            return {"error": "unknown work item"}
        d = dict(row)
        d.pop("approval_token", None)
        d["thesis"] = json.loads(d.pop("thesis_json") or "null")
        d["events"] = [dict(e) for e in self.ctx.conn.execute(
            "SELECT ts, actor, from_state, to_state, payload FROM events"
            " WHERE item_id=? ORDER BY id", (wi,))]
        d["agent_runs"] = [dict(e) for e in self.ctx.conn.execute(
            "SELECT agent_id, model, status, cost_usd, tokens_in,"
            " tokens_out, started_at, ended_at, error FROM agent_runs"
            " WHERE work_item_id=? ORDER BY started_at", (wi,))]
        d["orders"] = [dict(o) for o in self.ctx.conn.execute(
            "SELECT id, side, qty/1e6 qty, status, broker_order_id,"
            " expires_at, leg, parent_order_id FROM orders"
            " WHERE work_item_id=?", (wi,))]
        try:
            d["narratives"] = self.ctx.narratives.list(wi) if self.ctx.narratives else []
        except JailError:                   # malformed id → no files
            d["narratives"] = []
        if self.ctx.narratives and "summary.md" in d["narratives"]:
            d["summary"] = self.ctx.narratives.read(wi, "summary")
        # plain-language layer for the Trading page (additive: every raw
        # field above stays for the "details" disclosure)
        d["story"] = self._run_story(d)
        d["steps"] = self._run_steps(wi, d["narratives"], d["agent_runs"])
        d["timeline"] = self._run_timeline(d["events"], d["story"])
        return d

    # ── Trading page: runs as human-readable stories (read-only) ─────
    _STEP_AGENTS = (("fundamental", "Research: fundamentals"),
                    ("technical", "Research: technicals"),
                    ("news", "Research: news"),
                    ("bull", "Bull case"), ("bear", "Bear case"),
                    ("arbiter", "Verdict"))

    @staticmethod
    def _friendly_error(msg):
        """A broker / provider error as one sentence a non-developer can
        act on; the raw text is kept alongside by every caller."""
        raw = str(msg or "")
        m = raw.lower()
        if "credit balance is too low" in m:
            return "the AI provider account ran out of credit"
        if "not a whole multiple" in m and "lot size" in m:
            return ("the broker only accepts whole shares, and the "
                    "order asked for a fraction of one")
        if "toofarfromentryorder" in m:
            return ("the broker refused the stop/target: too far from "
                    "the entry price")
        if "insufficient" in m and "cash" in m:
            return "not enough cash in the account"
        if "rate limit" in m or "429" in m:
            return "the AI provider was rate-limiting requests"
        if "levels amended" in m:
            return "you changed the levels; needs a retry to send"
        inner = re.search(r"'message': '([^']+)'", raw)
        text = inner.group(1) if inner else raw
        text = re.sub(r"^(broker rejected order: )", "broker refused: ",
                      text)
        return text[:220]

    def _approvals_archive(self, days):
        """Approvals you have already dealt with — they leave the
        Approvals page and are kept here: what you decided and what
        happened next."""
        since = int(self.ctx.clock()) - max(1, min(days, 365)) * 86400
        out = []
        for r in self.ctx.conn.execute(
                "SELECT w.id, w.ticker, w.kind, w.state, w.trigger,"
                " w.thesis_json, w.updated_at, w.created_at FROM work_items w"
                " WHERE w.updated_at >= ? AND w.state NOT IN"
                " ('running','awaiting_approval')"
                " AND EXISTS (SELECT 1 FROM events e WHERE e.item_id=w.id"
                "  AND e.to_state='awaiting_approval')"
                " ORDER BY w.updated_at DESC LIMIT 200", (since,)):
            try:
                th = json.loads(r["thesis_json"] or "{}")
            except ValueError:
                th = {}
            dec = self.ctx.conn.execute(
                "SELECT to_state, ts, payload FROM events WHERE item_id=?"
                " AND to_state IN ('approved','rejected','expired')"
                " ORDER BY id LIMIT 1", (r["id"],)).fetchone()
            try:
                dp = json.loads(dec["payload"] or "{}") if dec else {}
            except ValueError:
                dp = {}
            fill = self.ctx.conn.execute(
                "SELECT SUM(e.qty)/1e6 q, SUM(1.0*e.qty*e.price_native)"
                "/SUM(e.qty)/1e6 px FROM executions e JOIN orders o"
                " ON o.id=e.order_id WHERE o.work_item_id=? AND"
                " o.leg IS NULL", (r["id"],)).fetchone()
            out.append({
                "id": r["id"], "ticker": r["ticker"], "kind": r["kind"],
                "state": r["state"], "trigger": r["trigger"],
                "direction": th.get("direction"),
                "entry": th.get("entry_high"),
                "stop": th.get("stop_loss"),
                "target": th.get("take_profit"),
                "currency": th.get("currency"),
                "decision": dec["to_state"] if dec else None,
                "decided_at": dec["ts"] if dec else None,
                "broker": dp.get("broker"),
                "size": dp.get("size_base"), "qty": dp.get("qty"),
                "filled_qty": fill["q"] if fill else None,
                "fill_price": fill["px"] if fill else None,
                "updated_at": r["updated_at"]})
        return {"items": out}

    def _run_story(self, d):
        """Who started a run, what it concluded and what happened to
        the order afterwards — derived, read-only, never money logic."""
        wi = d["id"]
        th = d.get("thesis")
        if th is None and d.get("thesis_json"):
            try:
                th = json.loads(d["thesis_json"])
            except ValueError:
                th = None
        th = th if isinstance(th, dict) else {}
        inst = self.ctx.conn.execute(
            "SELECT name, exchange, currency FROM instruments WHERE"
            " ticker=? ORDER BY (name IS NULL), id LIMIT 1",
            (d["ticker"],)).fetchone()
        trig = d.get("trigger") or ""
        kind = d.get("kind")
        reason = None
        if kind == "override" or d.get("override_of"):
            origin = "override"
        elif kind == "sell_review" or trig.startswith("watcher:"):
            origin = "price_alert"
            reason = trig.split(": ", 1)[1] if ": " in trig else trig
        elif trig.startswith("event scan"):
            origin = "volume_scan"
            reason = trig.split(": ", 1)[1] if ": " in trig else None
        elif trig.startswith("deterministic fallback"):
            origin = "daily_screen"
            reason = ("near the top of the most-active list (the "
                      "screener agent was unavailable)")
        elif trig.startswith("screener") or kind == "screen":
            origin = "daily_screen"
            reason = trig.split(": ", 1)[1] if ": " in trig else None
        elif trig.startswith("launched from the Trading page"):
            origin, reason = "you", "the Launch form on the Trading page"
        elif trig.startswith("launched from the Markets page"):
            origin, reason = "you", "Research on the Markets page"
        elif trig.startswith("manual "):
            origin, reason = "you", trig
        elif trig.startswith("chat: "):
            origin, reason = "chat", trig[len("chat: "):]
        elif not trig:
            # runs launched before 2026-11-06 recorded no source
            origin = "unrecorded"
        else:
            origin = "other"
            reason = trig
        conv = th.get("conviction")
        word = None
        if isinstance(conv, (int, float)):
            word = ("low" if conv < 0.4 else
                    "medium" if conv < 0.7 else "high")
        reasons = [c for c in (th.get("conditions") or [])
                   if isinstance(c, str) and
                   not c.startswith("HUMAN OVERRIDE")][:3]

        evs = [dict(e) for e in self.ctx.conn.execute(
            "SELECT ts, actor, to_state, payload FROM events"
            " WHERE item_id=? ORDER BY id", (wi,))]

        def payload(e):
            try:
                p = json.loads(e["payload"] or "null")
            except ValueError:
                p = None
            return p if isinstance(p, dict) else {}
        accts = {r["id"]: dict(r) for r in self.ctx.conn.execute(
            "SELECT id, broker, environment, base_currency"
            " FROM broker_accounts")}
        orders = [dict(o) for o in self.ctx.conn.execute(
            "SELECT id, account_id, side, qty/1e6 qty,"
            " limit_price/1e6 limit_price, stop_loss/1e6 stop_loss,"
            " take_profit/1e6 take_profit, status, broker_order_id,"
            " placed_at, updated_at, leg FROM orders WHERE work_item_id=?"
            " ORDER BY COALESCE(created_at, updated_at)", (wi,))]
        failed_rows = [o for o in orders if o["status"] == "rejected"
                       and not o["broker_order_id"]]
        real = [o for o in orders if o not in failed_rows]
        for o in real:
            a = accts.get(o["account_id"]) or {}
            o["account"] = self._account_label(a.get("broker"),
                                               a.get("environment"))
            if o["status"] in ("pending_session", "placed",
                               "partially_filled") and not o["leg"] \
                    and inst is not None:
                # an outstanding order: say what has to happen for it
                # to go out / fill — with today's price beside it
                from ..tools.market import yahoo_symbol
                o["exchange"] = inst["exchange"]
                o.update(session_facts(inst["exchange"],
                                       self.ctx.cal_or_default(), int(self.ctx.clock())))
                try:
                    o["last"] = float(self.ctx.market.quote(yahoo_symbol(
                        d["ticker"], inst["exchange"]))["price"])
                except Exception:           # noqa: BLE001 — best effort
                    o["last"] = None
                o["filled"] = self.ctx.conn.execute(
                    "SELECT COALESCE(SUM(qty),0)/1e6 FROM executions"
                    " WHERE order_id=?", (o["id"],)).fetchone()[0]
                o["expires_at"] = self.ctx.conn.execute(
                    "SELECT expires_at FROM orders WHERE id=?",
                    (o["id"],)).fetchone()[0]
                o["submitted_at"] = self.ctx.conn.execute(
                    "SELECT submitted_at FROM orders WHERE id=?",
                    (o["id"],)).fetchone()[0]
        approval = None
        appr = [e for e in evs if e["to_state"] in
                ("approved", "approve_retry")]
        if appr:
            p = payload(appr[-1])
            broker = p.get("broker")
            acct = (accts.get(real[-1]["account_id"]) if real else None) \
                or next((a for a in accts.values()
                         if a["broker"] == broker), None) or {}
            approval = {"ts": appr[0]["ts"], "last_ts": appr[-1]["ts"],
                        "size": p.get("size_base"), "qty": p.get("qty"),
                        "currency": acct.get("base_currency"),
                        "account": self._account_label(
                            acct.get("broker") or broker,
                            acct.get("environment"))}
        tries, seen = [], {}
        for e in evs:
            if e["to_state"] != "resume_failed":
                continue
            err = payload(e).get("error") or ""
            if "levels amended" in err:
                continue
            tries.append(e["ts"])
            f = self._friendly_error(err)
            if f not in seen:
                seen[f] = {"reason": f, "raw": err[:300], "count": 0}
            seen[f]["count"] += 1
        failure = None
        for e in reversed(evs):
            if e["to_state"] == "failed":
                p = payload(e)
                errs = p.get("errors") or ([p["error"]] if p.get("error")
                                           else [])
                fr = list(dict.fromkeys(self._friendly_error(x)
                                        for x in errs))
                failure = {"reason": "; ".join(fr) or "unknown error",
                           "raw": [str(x)[:400] for x in errs[:6]]}
                break
        ended = next((dict(payload(e), ts=e["ts"]) for e in reversed(evs)
                      if e["to_state"] in ("rejected", "expired", "cancelled")),
                     None)
        fills = [dict(f) for f in self.ctx.conn.execute(
            "SELECT side, SUM(qty)/1e6 qty,"
            " SUM(1.0*qty*price_native)/SUM(qty)/1e6 avg_price, currency,"
            " MAX(executed_at) at, account_id FROM executions"
            " WHERE work_item_id=? GROUP BY side, currency, account_id",
            (wi,))]
        for f in fills:
            a = accts.get(f["account_id"]) or {}
            f["account"] = self._account_label(a.get("broker"),
                                               a.get("environment"))
        overrides = [dict(r) for r in self.ctx.conn.execute(
            "SELECT id, state, created_at FROM work_items"
            " WHERE override_of=? ORDER BY created_at", (wi,))]
        return {
            "name": inst["name"] if inst else None,
            "exchange": inst["exchange"] if inst else None,
            "origin": origin, "origin_detail": reason,
            "override_of": d.get("override_of"),
            "overridden_by": overrides,
            "verdict": th.get("direction"),
            "conviction": conv, "conviction_word": word,
            "currency": th.get("currency"),
            "levels": {k: th.get(k) for k in (
                "entry_low", "entry_high", "stop_loss", "take_profit")},
            "reasons": reasons,
            "approval": approval,
            "orders": real,
            "fills": fills,
            "failed_attempts": {
                "rows": len(failed_rows), "tries": len(tries),
                "first_ts": tries[0] if tries else None,
                "last_ts": tries[-1] if tries else None,
                "reasons": sorted(seen.values(),
                                  key=lambda r: -r["count"])[:5]},
            "failure": failure,
            "ended": ended,
        }

    def _run_steps(self, wi, files, agent_runs):
        """The pipeline as readable steps: each agent's one-line
        conclusion and signal, parsed from its report.md."""
        from ..narrative_format import format_narrative
        out = []
        for agent, label in self._STEP_AGENTS:
            runs = [a for a in agent_runs if a["agent_id"] == agent]
            text = None
            if self.ctx.narratives and f"{agent}.report.md" in (files or []):
                try:
                    text = format_narrative(
                        self.ctx.narratives.read(wi, f"{agent}.report"))
                except (OSError, JailError):
                    text = None
            if text is None and not runs:
                continue
            step = {"agent": agent, "label": label, "signal": None,
                    "conviction": None, "summary": None, "points": [],
                    "has_report": text is not None,
                    "calls": len(runs),
                    "status": ("error" if runs and all(
                        a["status"] == "error" for a in runs) else "ok"),
                    "error": None}
            errs = [a.get("error") for a in runs if a.get("error")]
            if step["status"] == "error" and errs:
                step["error"] = self._friendly_error(errs[-1])
            for line in (text or "").splitlines():
                s = line.strip()
                m = re.match(r"\*\*(?:Signal|Verdict): ([\w-]+)\*\*.*?"
                             r"conviction ([\d.]+)", s) or \
                    re.match(r"\*\*(Bull|Bear) case\*\*.*?conviction "
                             r"([\d.]+)", s)
                if m:
                    step["signal"] = m.group(1).lower()
                    try:
                        step["conviction"] = float(m.group(2))
                    except ValueError:
                        pass
                elif s.startswith("- "):
                    if len(step["points"]) < 3:
                        step["points"].append(s[2:][:400])
                elif s and not s.startswith(("#", "**", "_", "·")) \
                        and step["summary"] is None:
                    step["summary"] = s[:500]
            if agent in ("bull", "bear") and step["signal"]:
                step["signal"] = None        # "bull case" is not a call
            out.append(step)
        return out

    def _run_timeline(self, events, story):
        """The audit trail as a few sentences; a retry loop's hundred
        identical rows collapse into one line."""
        out = []

        def p(e):
            try:
                v = json.loads(e.get("payload") or "null")
            except ValueError:
                v = None
            return v if isinstance(v, dict) else {}
        appr = story.get("approval") or {}
        i = 0
        while i < len(events):
            e = events[i]
            st, pl = e["to_state"], p(e)
            if st in ("approve_retry", "resume_failed") and not (
                    st == "resume_failed" and "levels amended" in
                    str(pl.get("error", ""))):
                j, fails, retries, errs = i, 0, 0, {}
                while j < len(events) and events[j]["to_state"] in (
                        "approve_retry", "resume_failed"):
                    q = p(events[j])
                    if "levels amended" in str(q.get("error", "")):
                        break
                    if events[j]["to_state"] == "resume_failed":
                        fails += 1
                        f = self._friendly_error(q.get("error"))
                        errs[f] = errs.get(f, 0) + 1
                    else:
                        retries += 1
                    j += 1
                if fails:
                    why = "; ".join(
                        f"{k} (×{v})" if len(errs) > 1 and v > 1 else k
                        for k, v in errs.items())
                    out.append({"ts": e["ts"], "tone": "bad", "text":
                                f"Sending the order failed {fails} "
                                f"time{'s' if fails > 1 else ''} — "
                                + why})
                elif retries:
                    out.append({"ts": e["ts"], "tone": "", "text":
                                "You retried sending the order"})
                i = max(j, i + 1)
                continue
            text, tone = None, ""
            if st == "running" and e.get("from_state") is None:
                text = "Run started"
            elif st == "override":
                text = (f"You overrode the verdict: "
                        f"{str(pl.get('direction') or '').upper()} at "
                        f"{pl.get('entry')}, stop {pl.get('stop')}, "
                        f"target {pl.get('target')}")
            elif st == "awaiting_approval":
                text, tone = "Sent to you for approval", "warn"
            elif st == "approved":
                size = pl.get("size_base")
                size = (f"{size:,.0f}" if isinstance(size, (int, float))
                        else "")
                text = (f"You approved {size}"
                        f" {appr.get('currency') or ''} on "
                        f"{appr.get('account') or pl.get('broker')}")
                text, tone = " ".join(text.split()), "ok"
            elif st == "amended":
                a, b = pl.get("from") or {}, pl.get("to") or {}
                text = ("You changed the levels: " + ", ".join(
                    f"{k} {a.get(k)} → {b.get(k)}" for k in
                    ("entry", "stop", "target")
                    if a.get(k) != b.get(k)))
            elif st == "resume_failed":
                text = None              # "levels amended": said above
            elif st == "executing":
                text, tone = "Order accepted by the broker", "ok"
            elif st == "done":
                text, tone = "Finished", "ok"
                fl = story.get("fills") or []
                if fl:
                    def px(v):
                        return f"{v:,.4f}".rstrip("0").rstrip(".")
                    text = "Finished — " + "; ".join(
                        ("bought" if f["side"] == "buy" else "sold")
                        + f" {f['qty']:g} shares at "
                        f"{px(f['avg_price'])} {f['currency']}"
                        for f in fl)
            elif st == "failed":
                f = story.get("failure") or {}
                text = f"Failed: {f.get('reason') or 'see details'}"
                tone = "bad"
            elif st == "rejected":
                text = "You rejected the trade"
                if pl.get("reason"):
                    text += f" ({pl['reason']})"
            elif st == "expired":
                text = "Expired" + (f": {pl['reason']}"
                                    if pl.get("reason") else "")
            elif st and not st.startswith("authorize:"):
                text = str(st).replace("_", " ").capitalize()
            if text:
                out.append({"ts": e["ts"], "text": text, "tone": tone})
            i += 1
        return out

    def _override_context(self, wi):
        """What the human needs on screen to author an override: the
        live quote, the levels that matter (moving averages, recent
        swing low/high, typical daily range) and what the desk holds —
        which decides whether a sell is an exit or a short."""
        from ..tools.market import yahoo_symbol
        r = self.ctx.conn.execute("SELECT w.ticker, i.exchange, i.currency FROM"
                         " work_items w LEFT JOIN instruments i ON"
                         " i.ticker = w.ticker WHERE w.id=?",
                         (wi,)).fetchone()
        if r is None:
            return {"error": "unknown work item"}
        sym = yahoo_symbol(r["ticker"], r["exchange"] or "NASDAQ")
        out = {"ticker": r["ticker"], "symbol": sym,
               "currency": r["currency"], "exchange": r["exchange"],
               # board lot (mirrors wiring.prepare): a buy smaller than
               # one lot is refused after approval, so the card warns
               "lot": desk_exchanges.lot_size(r["exchange"])}
        fx = {}
        for (ccy,) in self.ctx.conn.execute(
                "SELECT DISTINCT base_currency FROM broker_accounts"):
            try:
                fx[ccy] = float(self.ctx.market.fx(r["currency"] or ccy, ccy))
            except Exception as e:          # noqa: BLE001 — hint only
                swallowed(log, "override form: FX hint unavailable",
                          e, every=600)
                pass
        out["fx"] = fx          # 1 unit of the stock's ccy in each account ccy
        try:
            q = self.ctx.market.quote(sym)
            out["quote"] = {k: q.get(k) for k in (
                "price", "change_pct", "previous_close", "volume",
                "currency", "stale", "src")}
            if out["quote"]["price"] is not None:
                out["quote"]["price"] = float(out["quote"]["price"])
        except Exception as e:              # noqa: BLE001 — shown, not fatal
            out["quote_error"] = str(e)[:200]
        try:
            c = self.ctx.market.chart(sym, "6mo", "1d")
            out["levels"] = _levels(c)
            try:                # context only: never blocks the card
                out["range2y"] = _range(self.ctx.market.chart(sym, "2y", "1wk"))
            except Exception:               # noqa: BLE001
                out["range2y"] = None
        except Exception as e:              # noqa: BLE001
            out["levels_error"] = str(e)[:200]
        if not (out.get("levels") or {}).get("atr14"):
            # Yahoo rate-limits this machine; without ATR the
            # "use suggestion" buttons have nothing to work from.
            try:
                lv = tradingview.levels(sym)
                if lv:
                    out["levels"] = lv
                    out.pop("levels_error", None)
            except Exception as e:          # noqa: BLE001
                out.setdefault("levels_error", str(e)[:200])
        out["held"] = {a: float(q) for a, q in holdings.by_account(
            self.ctx.conn, ticker=r["ticker"]).items()}
        out["held_total"] = sum(out["held"].values())
        # London bars are in pence, the quote in pounds: same units
        px = (out.get("quote") or {}).get("price")
        # what the shares still held cost per share (the stock's own
        # currency) — a sell's summary shows the profit or loss on them
        out["avg_cost"] = {
            c["account_id"]: quote_units({"v": c["v"]}, px, ("v",))["v"]
            for c in self.ctx.conn.execute(
                "SELECT l.account_id, SUM(1.0*l.qty_remaining*e.price_native)"
                " /SUM(l.qty_remaining)/1e6 AS v FROM lots l"
                " JOIN instruments i ON i.id = l.instrument_id"
                " JOIN executions e ON e.id = l.open_execution_id"
                " WHERE i.ticker=? AND l.qty_remaining > 0"
                " AND l.qty_opened > 0 GROUP BY l.account_id",
                (r["ticker"],)) if c["v"]}
        out["levels"] = quote_units(out.get("levels") or {}, px)
        out["range2y"] = quote_units(out.get("range2y"), px,
                                     ("high", "low"))
        return out
