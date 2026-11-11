"""Read-only views of the desk itself: health, agents, providers, schedules' neighbours, exchanges, the system map.

Methods of the request handler, as a mixin: `self.ctx` is the server's Ctx
(app/dash/ctx.py), `self._send` and friends come from app/dash/handler.py.
Moved out of the create_server closure in app/dashboards.py unchanged except
that closure variables are now `self.ctx.<name>` (REVIEW-2026-11-11 §3.2)."""
import json
import logging
from datetime import datetime
from datetime import timezone


from .. import exchanges as desk_exchanges
from .. import scheduler
from .. import sessions
from ..accounts import venue_adapter
from ..providers import health as provider_health
from ..tools.files import JailError
from .usage import usage_report
from .common import PIPELINE_EDGES
from .common import background_loops

log = logging.getLogger(__name__)


class SystemViews:
    def _narrative(self, agent_id, work_item):
        """One run's output, split the way a reader wants it: the REPORT
        (the conclusion, and the only thing passed downstream) separate
        from the TRANSCRIPT (how it got there)."""
        from ..narrative_format import format_narrative
        if not self.ctx.narratives or not work_item:
            return None

        def read(name):
            try:
                return self.ctx.narratives.read(work_item, name)
            except (OSError, JailError):    # bad id/name → not found
                return None

        report, transcript = read(f"{agent_id}.report"), \
            read(f"{agent_id}.transcript")
        if report is None and transcript is None:
            return None
        return {"work_item": work_item, "agent": agent_id,
                "report": format_narrative(report)[:8000]
                if report else None,
                "transcript": format_narrative(transcript)[:12000]
                if transcript else None}

    def _exchanges(self):
        """The world-markets strip: pure session-calendar data — no Yahoo,
        immune to rate limits, always instant."""
        cal = self.ctx.cal_or_default()
        now = datetime.fromtimestamp(int(self.ctx.clock()), tz=timezone.utc)
        out = []
        for ex in ("SGX", "HKEX", "TSE", "ASX", "NSE", "LSE", "XETRA",
                   "PARIS", "NYSE", "NASDAQ"):
            s = cal.get(ex)
            if s is None:
                continue
            is_open = sessions.is_open(ex, now, cal)
            lunch = False
            if s.lunch_break and not is_open:
                nxt = sessions.next_open(ex, now, cal)
                lunch = nxt.date() == now.astimezone(nxt.tzinfo).date() \
                    and nxt.time().isoformat()[:5] == \
                    s.lunch_break.split("-")[1]
            out.append({"exchange": ex, "tz": s.tz, "is_open": is_open,
                        # what the exchange IS (app/exchanges.py)
                        "currency": desk_exchanges.currency(ex),
                        "suffix": desk_exchanges.SUFFIX.get(ex, ""),
                        "lot": desk_exchanges.lot_size(ex),
                        "at_lunch": lunch,
                        "open_time": s.open_time,
                        "close_time": s.close_time,
                        "lunch_break": s.lunch_break,
                        "next_open": sessions.next_open(ex, now,
                                                        cal).isoformat(),
                        "session_close": sessions.close_of(
                            ex, now, cal).isoformat()})
        return out

    def _agent_activity(self, agent_id):
        """What is this agent doing? Recent LLM runs, the work items they
        belong to, and which of them left a narrative to read."""
        runs = [dict(r) for r in self.ctx.conn.execute(
            "SELECT r.*, w.ticker, w.state AS wi_state, w.trigger"
            " FROM agent_runs r"
            " LEFT JOIN work_items w ON w.id = r.work_item_id"
            " WHERE r.agent_id=? ORDER BY r.started_at DESC LIMIT 5",
            (agent_id,))]
        current = next((r for r in runs if r["status"] == "running"), None)
        for r in runs:
            files = (self.ctx.narratives.list(r["work_item_id"])
                     if r["work_item_id"] and self.ctx.narratives else [])
            r["has_narrative"] = any(
                f in files for f in (f"{agent_id}.report.md",
                                     f"{agent_id}.transcript.md"))
        narrative = None
        for r in runs:
            if r.get("has_narrative"):
                narrative = self._narrative(agent_id, r["work_item_id"])
                break
        (cost_today,) = self.ctx.conn.execute(
            "SELECT COALESCE(SUM(cost_usd),0) FROM agent_runs"
            " WHERE agent_id=? AND started_at > ?",
            (agent_id, int(self.ctx.clock()) - 86400)).fetchone()
        reports = {}
        for rep in self.ctx.conn.execute(
                "SELECT work_item_id, payload FROM agent_reports"
                " WHERE agent_id=?", (agent_id,)):
            try:
                reports[rep["work_item_id"]] = json.loads(rep["payload"])
            except ValueError:
                pass
        for r in runs:
            r["report"] = reports.get(r["work_item_id"])
        return {"agent_id": agent_id, "current": current, "runs": runs,
                "narrative": narrative, "cost_24h": cost_today}

    # ── views ────────────────────────────────────────────────────────
    def _overview(self):
        states = {r["state"]: r["n"] for r in self.ctx.conn.execute(
            "SELECT state, COUNT(*) n FROM work_items GROUP BY state")}
        healthrows = {}
        for r in self.ctx.conn.execute(
                "SELECT service, ok, MAX(checked_at) AS checked_at"
                " FROM service_health GROUP BY service"):
            stale = self.ctx.clock() - r["checked_at"] > 1800
            healthrows[r["service"]] = {
                "ok": "stale" if stale else r["ok"],
                "checked_at": r["checked_at"]}
        cash = [{"account": a["id"],
                 "cash": self.ctx.repo.cash_balance(a["id"]) / 1e6,
                 "ccy": a["base_currency"]}
                for a in self.ctx.conn.execute("SELECT * FROM broker_accounts")]
        up = self.ctx.api_ok()
        flags = []
        if not up:
            flags.append("claire-api :7788 is down")
        kinds = {r[0] for r in self.ctx.conn.execute(
            "SELECT p.kind FROM agents a JOIN providers p"
            " ON p.id=a.provider_id WHERE a.enabled=1")}
        if "anthropic" in kinds and not self.ctx.env.get("ANTHROPIC_API_KEY"):
            flags.append("ANTHROPIC_API_KEY missing — LLM runs will fail"
                         " (add to etc/claire.env, restart claire-api)")
        if "claude_sdk" in kinds:
            from ..providers import claude_sdk
            why = claude_sdk.not_ready()
            if why:
                flags.append(f"Claude (Max subscription): {why}")
        for pid, h in provider_health.latest(self.ctx.conn).items():
            if h and not h["ok"]:
                flags.append(f"provider {pid} failing its health probe")
        for svc, h in healthrows.items():
            if h["ok"] != "ok":
                flags.append(f"{svc} is {h['ok']}")
        now = int(self.ctx.clock())
        day_ago = now - 86400
        acks = {r["key"]: r["acked_at"] for r in self.ctx.conn.execute(
            "SELECT key, acked_at FROM dashboard_acks")}
        # a warning's key is its text, so a CHANGED warning is new; a
        # cleared one returns after a day if it is still true
        items = [{"key": "flag:" + f[:110], "text": f} for f in flags]
        visible = [i for i in items
                   if acks.get(i["key"], 0) < day_ago]
        cleared = [dict(i, acked_at=acks[i["key"]]) for i in items
                   if i not in visible]
        # failed runs: clearing acknowledges the ones so far; only a
        # failure AFTER that brings the warning back
        seen = max(day_ago, acks.get("failed_runs", 0))
        (nfail,) = self.ctx.conn.execute(
            "SELECT COUNT(*) FROM work_items WHERE state='failed'"
            " AND updated_at > ?", (seen,)).fetchone()
        if nfail:
            visible.append({"key": "failed_runs", "text":
                            f"{nfail} failed run(s) in the last 24h"
                            + (" since you last cleared this"
                               if seen > day_ago else "")
                            + " — see Trading", "link": "/trading"})
        return {"work_items": states, "health": healthrows, "cash": cash,
                "claire_api": up,
                "flags": [i["text"] for i in visible],
                "flag_items": visible, "cleared": cleared,
                "pending": states.get("awaiting_approval", 0)}

    def _usage(self, days):
        """GET /api/usage?days=N — token usage per agent / source /
        model / day (read-only; see usage_report)."""
        try:
            n = int(days)
        except (TypeError, ValueError):
            n = 7
        return usage_report(self.ctx.conn, now=self.ctx.clock(), days=n)

    def _agents(self):
        out = []
        for a in self.ctx.conn.execute("SELECT * FROM agents"):
            run = self.ctx.conn.execute(
                "SELECT model, cost_usd, tokens_in, tokens_out, status,"
                " started_at, ended_at FROM agent_runs WHERE agent_id=?"
                " ORDER BY started_at DESC LIMIT 1", (a["id"],)).fetchone()
            out.append({"id": a["id"], "display_name": a["display_name"],
                        "provider_id": a["provider_id"], "model": a["model"],
                        "temperature": a["temperature"],
                        "fallback_provider_id": a["fallback_provider_id"],
                        "enabled": a["enabled"],
                        "tools": json.loads(a["tools"]),
                        "requires": json.loads(a["requires"]),
                        "system_prompt": a["system_prompt"],
                        "last_run": dict(run) if run else None})
        return out

    def _maintenance(self):
        import sqlite3

        from .. import maintenance
        try:
            out = maintenance.latest(self.ctx.conn)
        except sqlite3.OperationalError:    # claire-api not restarted yet
            out = {"latest": None, "history": []}
        job = next((r for r in scheduler.rows_with_next(self.ctx.conn, clock=self.ctx.clock)
                    if r.get("job") == "maintenance"), None)
        out["schedule"] = ({k: job.get(k) for k in (
            "enabled", "spec", "next_run_at", "last_run_at")}
            if job else None)
        out["retention"] = {
            "log_rotate_mb": maintenance.LOG_ROTATE_BYTES // 2 ** 20,
            "log_keep": maintenance.LOG_KEEP,
            "backup_keep": maintenance.BACKUP_KEEP,
            "checkpoint_days": maintenance.CHECKPOINT_DAYS,
            "health_days": maintenance.HEALTH_DAYS,
            "narrative_days": maintenance.NARRATIVE_DAYS,
            "sdk_days": maintenance.SDK_TRANSCRIPT_DAYS,
            "runs_keep": maintenance.RUNS_KEEP}
        return out

    def _nav_state(self):
        """What every page's menu shows: how many decisions are waiting
        and whether real money is switched on. Two reads, no writes —
        every open tab polls it."""
        from .. import live_trading
        (pending,) = self.ctx.conn.execute(
            "SELECT COUNT(*) FROM work_items WHERE"
            " state='awaiting_approval'").fetchone()
        row = self.ctx.conn.execute(
            "SELECT armed FROM live_trading WHERE id=1").fetchone()
        enabled = live_trading.env_enabled(self.ctx.env or {})
        (live_accounts,) = self.ctx.conn.execute(
            "SELECT COUNT(*) FROM broker_accounts WHERE"
            " environment='live'").fetchone()
        return {"pending": pending,
                "live_armed": bool(row and row["armed"]) and enabled,
                "live_enabled": enabled, "live_accounts": live_accounts}

    def _live_status(self):
        from .. import live_trading
        st = live_trading.status(self.ctx.conn, self.ctx.env or {}, clock=self.ctx.clock)
        # why arming is not possible right now, in the order to fix it
        if not st["enabled_in_env"]:
            why = (f"{live_trading.ENV_FLAG}=enabled is not set in "
                   "etc/claire.env (then bin/claire restart)")
        elif not st["accounts"]:
            why = "no live account yet: run bin/claire live setup"
        elif not self._from_this_pc():
            why = ("arming is only accepted from a browser on the desk's "
                   "own PC (http://127.0.0.1:7787)")
        else:
            why = None
        return {**st, "metrics": live_trading.metrics(self.ctx.conn, clock=self.ctx.clock),
                "arm_phrase": live_trading.ARM_PHRASE,
                "can_arm_here": why is None, "arm_blocked": why}

    def _agent_graph(self):
        """Every node carries a status the UI renders identically:
        active (running now) · ok (configured and proven) · warn
        (degraded) · error (cannot run / failing) · idle (configured,
        never run) · disabled."""
        now = int(self.ctx.clock())
        provs = {p["id"]: p for p in self._providers()}
        from ..providers import claude_sdk
        sdk_why = claude_sdk.not_ready()
        agents = {}
        for a in self._agents():
            lr = a["last_run"] or {}
            p = provs.get(a["provider_id"], {})
            active = (lr.get("status") == "running" and
                      now - (lr.get("started_at") or 0) < 600)
            if not a["enabled"]:
                state, detail = "disabled", "disabled"
            elif active:
                state, detail = "active", "running now"
            elif p.get("key_ref") and not p.get("key_present"):
                # configured on paper but cannot actually run
                state, detail = "error", f"{p['key_ref']} not set"
            elif p.get("kind") == "claude_sdk" and sdk_why:
                state, detail = "error", sdk_why
            elif not p.get("enabled", 1):
                state, detail = "error", f"provider {a['provider_id']} off"
            elif lr.get("status") == "error":
                state, detail = "error", (lr.get("error") or
                                          "last run failed")[:60]
            elif lr:
                state, detail = "ok", "ready"
            else:
                state, detail = "idle", "never run"
            agents[a["id"]] = {**a, "state": state, "detail": detail}
        brokers = []
        for acct in self.ctx.conn.execute("SELECT * FROM broker_accounts"):
            (open_orders,) = self.ctx.conn.execute(
                "SELECT COUNT(*) FROM orders WHERE account_id=? AND"
                " status IN ('pending_session','placed',"
                "'partially_filled')", (acct["id"],)).fetchone()
            mcp = self.ctx.conn.execute(
                "SELECT * FROM mcp_servers WHERE broker=? AND enabled=1",
                (acct["broker"],)).fetchone()
            refs = json.loads(mcp["env_refs"]) if mcp else []
            creds = bool(refs) and all(self.ctx.env.get(r) for r in refs)
            # where orders REALLY go — the same rule as
            # wiring.build_brokers (claire-api builds the adapters). An
            # MCP row is configuration only: no MCP server ever places.
            adapter = venue_adapter(acct, self.ctx.env or {})
            if adapter == "saxo-sim":
                bstate, bdetail = "ok", ("orders go to Saxo SIM — real "
                                         "Saxo order handling, "
                                         "simulated money; SGX shares "
                                         "are simulated by the desk "
                                         "(Saxo SIM lists them only as "
                                         "CFDs)")
            elif adapter == "saxo-live":
                bstate, bdetail = "warn", ("REAL MONEY at Saxo — only "
                                           "while live trading is armed")
            elif mcp and refs and not creds:
                bstate, bdetail = "warn", ("Claire's paper sim · MCP "
                                           "configured, creds missing "
                                           "(unused)")
            elif mcp:
                bstate, bdetail = "ok", ("Claire's paper sim · MCP "
                                         "configured but not used for "
                                         "orders")
            else:
                bstate, bdetail = "idle", "Claire's paper sim"
            brokers.append({
                "broker": acct["broker"], "account": acct["id"],
                "environment": acct["environment"],
                "cash": self.ctx.repo.cash_balance(acct["id"]) / 1e6,
                "ccy": acct["base_currency"],
                "open_orders": open_orders,
                "state": bstate, "detail": bdetail,
                "adapter": adapter,
                "mcp": {"command": " ".join(json.loads(mcp["command"])),
                        "env_refs": refs,
                        "creds_present": creds,
                        "used_for_execution": False,
                        "note": mcp["note"]} if mcp else None})
        # a guard that stopped reporting is an ERROR, not a shrug: a dead
        # safety net is worse than no safety net (v1 lesson §19.10)
        state_of = {"ok": "ok", "degraded": "warn", "stale": "error"}
        health = {}
        for svc in ("watcher", "custodian"):
            r = self.ctx.conn.execute(
                "SELECT ok, MAX(checked_at) AS checked_at FROM"
                " service_health WHERE service=?", (svc,)).fetchone()
            if r is None or r["checked_at"] is None:
                health[svc] = {"ok": "never ran", "state": "idle",
                               "checked_at": None}
                continue
            ok = "stale" if self.ctx.clock() - r["checked_at"] > 1800 else r["ok"]
            health[svc] = {"ok": ok, "state": state_of.get(ok, "warn"),
                           "checked_at": r["checked_at"]}
        (open_orders,) = self.ctx.conn.execute(
            "SELECT COUNT(*) FROM orders WHERE status IN"
            " ('pending_session','placed','partially_filled')").fetchone()
        (positions,) = self.ctx.conn.execute(
            "SELECT COUNT(DISTINCT instrument_id) FROM lots"
            " WHERE qty_remaining > 0").fetchone()
        (pending,) = self.ctx.conn.execute(
            "SELECT COUNT(*) FROM work_items WHERE"
            " state='awaiting_approval'").fetchone()
        primary = self.ctx.market.primary_name() if hasattr(self.ctx.market, "primary_name") \
            else None
        stale = len(self.ctx.market._stale_keys) if hasattr(self.ctx.market, "_stale_keys") \
            else 0
        return {"agents": agents, "edges": PIPELINE_EDGES,
                "loops": background_loops(self.ctx.env or {}),
                "brokers": brokers, "claire_api": self.ctx.api_ok(),
                "mcps": self._mcps(), "health": health,
                "book": {"open_orders": open_orders,
                         "positions": positions, "pending": pending},
                "market_data": {
                    "source": primary or "yahoo",
                    "fallbacks": " · ".join(
                        ["tradingview", "yahoo"]
                        + (["saxo charts"] if getattr(
                            self.ctx.market, "_chart_fallback", None) else [])
                        + ["ecb fx"]),
                    "stale_keys": stale,
                    "state": "warn" if stale else "ok",
                    "detail": (f"{stale} symbol(s) served stale"
                               if stale else
                               ("keyed provider" if primary
                                else "free source"))}}

    def _exchange_info(self, exchange):
        cal = self.ctx.cal_or_default()
        s = cal.get(exchange)
        if s is None:
            return {"error": f"unknown exchange {exchange}"}
        now = datetime.fromtimestamp(int(self.ctx.clock()), tz=timezone.utc)
        info = {"exchange": exchange, "tz": s.tz,
                "open_time": s.open_time, "close_time": s.close_time,
                "lunch_break": s.lunch_break,
                "holidays": sorted(s.holidays),
                "is_open": sessions.is_open(exchange, now, cal),
                "next_open": sessions.next_open(exchange, now,
                                                cal).isoformat(),
                "session_close": sessions.close_of(exchange, now,
                                                   cal).isoformat()}
        info.update(self.ctx.market.exchange_metrics(exchange))
        return info

    def _providers(self):
        latest = provider_health.latest(self.ctx.conn)
        out = []
        for p in self.ctx.conn.execute("SELECT * FROM providers"):
            h = latest.get(p["id"])
            out.append({"id": p["id"], "display_name": p["display_name"],
                        "kind": p["kind"], "base_url": p["base_url"],
                        "key_ref": p["api_key_ref"],
                        "key_present": bool(
                            p["api_key_ref"] and
                            (self.ctx.env or {}).get(p["api_key_ref"])),
                        "capabilities": json.loads(p["capabilities"]),
                        "cost_per_1k_in": p["cost_per_1k_in"],
                        "cost_per_1k_out": p["cost_per_1k_out"],
                        "enabled": p["enabled"],
                        "health": dict(h) if h else None})
        return out

    def _data_sources(self):
        """Market-data source chain status — configure by adding the env
        vars to etc/claire.env and restarting both services."""
        primary = self.ctx.market.primary_name() if hasattr(self.ctx.market,
                                                   "primary_name") else None
        return [
            {"id": "ibkr", "name": "Interactive Brokers",
             "role": "primary when enabled (licensed exchange data)",
             "configured": bool((self.ctx.env or {}).get("IBKR_ENABLED")),
             "active": primary == "ibkr",
             "how": "IBKR_ENABLED=1 + IBKR_HOST/IBKR_PORT in claire.env; "
                    "IB Gateway running (paper ok) + pip install "
                    "ib-insync + per-exchange data subscriptions"},
            {"id": "twelvedata", "name": "Twelve Data",
             "role": "primary when keyed (global quotes/charts/FX)",
             "configured": bool((self.ctx.env or {}).get("TWELVEDATA_API_KEY")),
             "active": primary == "twelvedata",
             "how": "TWELVEDATA_API_KEY in claire.env (free tier "
                    "~800 credits/day)"},
            {"id": "yahoo", "name": "Yahoo Finance",
             "role": "free default (paced + host-rotated; throttles)",
             "configured": True, "active": primary is None,
             "how": "always on"},
            {"id": "tradingview", "name": "TradingView scanner",
             "role": "quote fallback + recommendations",
             "configured": True, "active": True, "how": "always on"},
            {"id": "ecb", "name": "ECB / Frankfurter",
             "role": "FX fallback (daily rates)",
             "configured": True, "active": True, "how": "always on"},
        ]

    def _mcps(self):
        out = []
        for m in self.ctx.conn.execute("SELECT * FROM mcp_servers ORDER BY id"):
            refs = json.loads(m["env_refs"])
            out.append({"id": m["id"], "broker": m["broker"],
                        "command": json.loads(m["command"]),
                        "env_refs": refs,
                        "creds_present": bool(refs) and
                        all((self.ctx.env or {}).get(r) for r in refs),
                        "missing_refs": [r for r in refs
                                         if not (self.ctx.env or {}).get(r)],
                        "note": m["note"],
                        "enabled": bool(m["enabled"])})
        return out
