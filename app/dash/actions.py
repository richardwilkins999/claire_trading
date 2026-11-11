"""What a POST does: amend and check a trade, manual trades, Saxo sign-in, provider/MCP/agent edits, account cash, starting a run.

Methods of the request handler, as a mixin: `self.ctx` is the server's Ctx
(app/dash/ctx.py), `self._send` and friends come from app/dash/handler.py.
Moved out of the create_server closure in app/dashboards.py unchanged except
that closure variables are now `self.ctx.<name>` (REVIEW-2026-11-11 §3.2)."""
import json
import logging
from html import escape as html_escape
from urllib.parse import urlparse


from .. import approvals
from ..providers import registry
from ..providers.registry import PROVIDER_KEY_REFS
from .common import SAXO_CALLBACK
from .common import PROVIDER_KINDS
from .common import _ID_RE
from .common import _MODEL_RE
from .common import _CCY_RE
from . import claire_api
from .common import HTTPError

log = logging.getLogger(__name__)


class Actions:
    def _amend(self, wi, body):
        """Push edited price levels to claire-api; raises AuthError with
        its reason (e.g. a stop on the wrong side) if refused."""
        cur = json.loads((self.ctx.conn.execute(
            "SELECT thesis_json FROM work_items WHERE id=?", (wi,))
            .fetchone() or {"thesis_json": "{}"})["thesis_json"] or "{}")
        want = {"entry": float(body["entry"]),
                "stop": float(body["stop"]) if body.get("stop") else None,
                "target": float(body["target"]) if body.get("target")
                else None}
        if (cur.get("entry_high"), cur.get("stop_loss"),
                cur.get("take_profit")) == (want["entry"], want["stop"],
                                             want["target"]):
            return                              # unchanged: nothing to do
        try:
            claire_api.post(self.ctx, "/internal/amend",
                            {"work_item_id": wi, "actor": "human", **want},
                            what="amend")
        except HTTPError as e:      # callers of _amend expect an AuthError
            raise approvals.AuthError(e.code, e.message) from e

    def _trade_check(self, body):
        """'Check with Saxo': size the edited order exactly as execution
        would (live quote, whole shares) and ask Saxo's precheck whether
        it would be accepted. Places nothing. Either for a work item
        (work_item_id) or for a trade not yet started — the Sell button
        sends ticker, exchange, direction, entry and qty."""
        from decimal import Decimal

        from ..graph.nodes_execution import FRACTION_STEP, position_qty
        from ..tools.market import yahoo_symbol
        from ..wiring import build_brokers
        wi = str(body.get("work_item_id") or "")
        if wi:
            r = self.ctx.conn.execute("SELECT w.ticker, w.thesis_json, i.exchange"
                             " FROM work_items w LEFT JOIN instruments i"
                             " ON i.ticker=w.ticker WHERE w.id=?",
                             (wi,)).fetchone()
            if r is None:
                raise HTTPError(404, "unknown work item")
            ticker, exchange = r["ticker"], r["exchange"]
            t = json.loads(r["thesis_json"] or "{}")
        else:
            ticker = str(body.get("ticker") or "").upper().strip()
            exchange = str(body.get("exchange") or "").upper().strip()
            self._check_symbol(ticker, exchange)
            if body.get("direction") not in ("buy", "sell"):
                raise HTTPError(400, "direction must be buy or sell")
            t = {"direction": body["direction"]}
        broker = body.get("broker")
        adapter = build_brokers(self.ctx.env or {}, self.ctx.conn).get(broker)
        if adapter is None or not hasattr(adapter, "precheck"):
            return {"ok": None, "note": "this account uses the built-in "
                    "paper sim — there is no broker to ask"}
        side = t.get("direction") or "buy"
        try:
            entry = float(body.get("entry") or t.get("entry_high") or 0)
        except (TypeError, ValueError):
            raise HTTPError(400, "entry must be a number") from None
        if entry <= 0:
            raise HTTPError(400, "an entry price is required")
        sym = yahoo_symbol(ticker, exchange or "NASDAQ")
        if side == "buy" and body.get("qty"):
            # the card's share count: check exactly that many
            qty = Decimal(str(body["qty"]))
            if not getattr(adapter, "fractional", True):
                qty = qty.to_integral_value(rounding="ROUND_FLOOR")
        elif side == "buy":
            q = Decimal(str(self.ctx.market.quote(sym)["price"]))
            qty = position_qty(Decimal(str(body.get("size_base") or 0)),
                               q, 1, FRACTION_STEP if getattr(
                                   adapter, "fractional", True)
                               else Decimal(1))
        else:
            qty = Decimal(str(body.get("qty") or 0))
        if qty <= 0:
            return {"ok": False, "error": "that size buys zero shares"}
        # an exit carries no bracket (execution strips it), so neither
        # does its check
        held = self._desk_held_on(broker, ticker, exchange)
        exit_ = (side == "sell" and held > 0) or (side == "buy" and
                                                   held < 0)
        stop = None if exit_ else (float(body["stop"])
                                   if body.get("stop") else None)
        kw = dict(symbol=sym, side=side, qty=qty, limit=entry,
                  stop_loss=stop,
                  take_profit=None if exit_ else (
                      float(body["target"]) if body.get("target")
                      else None))
        try:
            trail = float(body.get("trail_pct") or 0)
        except (TypeError, ValueError):
            trail = 0
        from ..graph.nodes_execution import _accepts
        if stop and 0 < trail <= 90 and \
                _accepts(adapter, "trail_distance", "precheck"):
            # the exact order execution sends: the stop as a trailing
            # stop, distance = the floor % of the entry
            kw["trail_distance"] = entry * trail / 100
        out = adapter.precheck(**kw)
        if out.get("error") and "RateLimitExceeded" in out["error"]:
            out["error"] = "Saxo asks to slow down — wait a second and " \
                           "check again"
        elif out.get("error") and \
                "SellOrdersAlreadyExistForOwnedContracts" in out["error"] \
                and exit_:
            # the protective stop/target the desk holds for these shares
            # are what Saxo objects to — and execution cancels exactly
            # those first (DESIGN.md §11a). Saxo cannot check past them.
            out = {**out, "ok": None, "legs_block": True, "note":
                   "Saxo cannot fully check this sell while the "
                   "position's protective stop/target are working there "
                   "— on approval the desk cancels them first, then "
                   f"places your sell of {qty} @ {out.get('price')}; any "
                   "shares left are re-protected at the same levels."}
        return {**out, "qty": float(qty)}

    def _manual_trade(self, body):
        """Forward a trade you started to claire-api (/internal/manual),
        which validates it and puts it at the approval gate."""
        allowed = ("ticker", "exchange", "direction", "entry", "stop",
                   "target", "note", "suggested", "source")
        fwd = {k: body.get(k) for k in allowed if body.get(k) is not None}
        if isinstance(fwd.get("ticker"), str) and \
                isinstance(fwd.get("exchange"), str):
            from ..tools.market import desk_ticker
            fwd["ticker"] = desk_ticker(fwd["ticker"].upper().strip(),
                                        fwd["exchange"].upper().strip())
        fwd["actor"] = "human"
        return self._send(200, claire_api.post(
            self.ctx, "/internal/manual", fwd, what="manual trade"))

    def _saxo_login_start(self, body):
        """Begin a Saxo sign-in from the Saxo accounts page. Needs the
        dashboard key (every POST does), so only Richard can start one;
        Saxo then sends the browser back to SAXO_CALLBACK on THIS server.
        Saxo only redirects to the registered host — localhost — so the
        button works in a browser on this PC."""
        from ..tools import saxo_live
        which = body.get("env")
        if which not in saxo_live.ENVS:
            raise HTTPError(400, "env must be sim or live")
        try:
            cfg = saxo_live.app_config(self.ctx.env or {}, which)
        except saxo_live.SaxoError as e:
            raise HTTPError(400, str(e))
        path = urlparse(cfg["redirect_uri"]).path or "/"
        if path != SAXO_CALLBACK:
            raise HTTPError(400, f"the app's redirect URL path is {path}; "
                                 f"the dashboard listens on {SAXO_CALLBACK}")
        redirect = (f"http://localhost:{self.server.server_address[1]}"
                    f"{SAXO_CALLBACK}")
        url = saxo_live.begin_login(which, redirect, env=self.ctx.env or {},
                                    clock=self.ctx.clock)
        return self._send(200, {"url": url})

    def _saxo_login_callback(self, q):
        from ..tools import saxo_live
        try:
            if q.get("error"):
                raise saxo_live.SaxoError(q.get("error_description")
                                          or q["error"])
            which = saxo_live.complete_login(
                q.get("state"), q.get("code"), env=self.ctx.env or {}, clock=self.ctx.clock)
            where = f"/saxo?env={which}&signin=ok"
        except saxo_live.SaxoError as e:
            body = (f"<meta charset=utf-8><link rel=stylesheet "
                    f"href=/style.css><main><h1>Saxo sign-in failed</h1>"
                    f"<p class=bad>{html_escape(str(e))}</p>"
                    f"<p><a href=/saxo>Back to Saxo accounts</a></p></main>")
            return self._send(400, body.encode(), "text/html; charset=utf-8")
        self.send_response(303)
        self.send_header("Location", where)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _upsert_provider(self, body):
        pid = str(body.get("id", "")).strip()
        if not _ID_RE.fullmatch(pid):
            return self._send(400, {"error":
                                    "id must be 2-30 chars [a-z0-9_-]"})
        kind = body.get("kind")
        if kind not in PROVIDER_KINDS:
            return self._send(400, {"error": "kind must be anthropic |"
                                    " claude_sdk | openai_compatible |"
                                    " custom"})
        # C1: the key ref is a NAME from a fixed list, never free-form —
        # otherwise base_url=attacker + api_key_ref=CLAIRE_DASHBOARD_KEY
        # turns one probe into a key-exfiltration
        key_ref = body.get("api_key_ref") or None
        if key_ref is not None and key_ref not in PROVIDER_KEY_REFS:
            return self._send(400, {"error": "api_key_ref must be one of "
                                    + ", ".join(sorted(PROVIDER_KEY_REFS))
                                    + " (or empty)"})
        if kind == "claude_sdk":
            key_ref = None              # the SDK uses the CLI's login
        base_url = body.get("base_url") or None
        if base_url is not None and (
                not isinstance(base_url, str) or len(base_url) > 300 or
                urlparse(base_url).scheme not in ("http", "https") or
                not urlparse(base_url).netloc):
            return self._send(400, {"error":
                                    "base_url must be an http(s) URL"})
        name = body.get("display_name") or pid
        if not isinstance(name, str) or len(name) > 60:
            return self._send(400, {"error":
                                    "display_name must be ≤60 chars"})
        for k in ("cost_per_1k_in", "cost_per_1k_out"):
            v = body.get(k)
            if v is not None and (isinstance(v, bool) or not isinstance(
                    v, (int, float)) or not 0 <= v <= 10):
                return self._send(400, {"error": f"{k} must be 0..10"})
        caps = body.get("capabilities") or {}
        if not isinstance(caps, dict) or not all(
                isinstance(v, bool) for v in caps.values()):
            return self._send(400, {"error": "capabilities must be an"
                                    " object of booleans"})
        self.ctx.conn.execute(
            "INSERT INTO providers (id, display_name, kind, base_url,"
            " api_key_ref, capabilities, cost_per_1k_in, cost_per_1k_out,"
            " enabled) VALUES (?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET display_name=excluded"
            ".display_name, kind=excluded.kind, base_url=excluded.base_url,"
            " api_key_ref=excluded.api_key_ref, capabilities=excluded"
            ".capabilities, cost_per_1k_in=excluded.cost_per_1k_in,"
            " cost_per_1k_out=excluded.cost_per_1k_out,"
            " enabled=excluded.enabled",
            (pid, name, kind, base_url, key_ref,
             json.dumps(caps), body.get("cost_per_1k_in"),
             body.get("cost_per_1k_out"),
             1 if body.get("enabled", True) else 0))
        return self._send(200, {"ok": True, "id": pid})

    def _upsert_mcp(self, body):
        import re
        import shlex
        mid = str(body.get("id", "")).strip()
        if not re.fullmatch(r"[a-z0-9_-]{2,30}", mid):
            return self._send(400, {"error":
                                    "id must be 2-30 chars [a-z0-9_-]"})
        if body.get("action") == "delete":
            self.ctx.conn.execute("DELETE FROM mcp_servers WHERE id=?", (mid,))
            return self._send(200, {"ok": True, "deleted": mid})
        command = body.get("command")
        if isinstance(command, str):
            command = shlex.split(command)
        if not command or not isinstance(command, list) or not all(
                isinstance(c, str) and c for c in command) or \
                len(command) > 32:
            return self._send(400, {"error": "command required"})
        refs = body.get("env_refs") or []
        if isinstance(refs, str):
            refs = [r.strip() for r in refs.split(",") if r.strip()]
        if not isinstance(refs, list) or any(
                not isinstance(r, str) or
                not re.fullmatch(r"[A-Z][A-Z0-9_]*", r) for r in refs):
            return self._send(400, {"error": "env_refs must be UPPER_CASE"
                                    " env var NAMES (values stay in"
                                    " claire.env)"})
        # the desk's own secrets must never be handed to a third-party
        # process, whenever MCP launching lands (same lesson as C1)
        if any(r.startswith("CLAIRE_") or r in PROVIDER_KEY_REFS
               for r in refs):
            return self._send(400, {"error": "env_refs may not name "
                                    "CLAIRE_* or LLM provider keys"})
        broker = body.get("broker") or None
        if broker is not None and (not isinstance(broker, str) or
                                   not _ID_RE.fullmatch(broker)):
            return self._send(400, {"error": "broker must be an id"})
        self.ctx.conn.execute(
            "INSERT INTO mcp_servers (id, broker, command, env_refs,"
            " note, enabled) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET broker=excluded.broker,"
            " command=excluded.command, env_refs=excluded.env_refs,"
            " note=excluded.note, enabled=excluded.enabled",
            (mid, broker, json.dumps(command),
             json.dumps(refs), body.get("note"),
             1 if body.get("enabled", True) else 0))
        return self._send(200, {"ok": True, "id": mid})

    def _account_action(self, body):
        """Top-up / withdraw paper cash — paired browsers only (it moves
        the ledger), every movement is a cash_transactions row."""
        from ..accounting.money import to_micro
        # the same check as every other money route. It used to be skipped
        # when no key was configured; do_POST already refuses that case, but
        # a route that moves ledger cash should not depend on its caller
        if not self._paired():
            raise HTTPError(403, "dashboard not paired")
        account = body.get("account_id", "")
        action = body.get("action")
        try:
            amount = to_micro(str(body.get("amount", 0)))
        except Exception:                    # noqa: BLE001
            return self._send(400, {"error": "amount must be a number"})
        if amount <= 0:
            return self._send(400, {"error": "amount must be positive"})
        from ..accounting.repo import InsufficientCash, LedgerError
        try:
            if action == "deposit":
                self.ctx.repo.deposit(account, amount, ts=int(self.ctx.clock()),
                             note="dashboard top-up")
            elif action == "withdraw":
                self.ctx.repo.withdraw(account, amount, ts=int(self.ctx.clock()),
                              note="dashboard withdrawal")
            else:
                return self._send(400, {"error":
                                        "action must be deposit|withdraw"})
        except InsufficientCash as e:
            return self._send(400, {"error": str(e)})
        except LedgerError as e:
            return self._send(400, {"error": str(e)})
        return self._send(200, {"ok": True,
                                "balance": self.ctx.repo.cash_balance(account) / 1e6})

    def _start_run(self, body):
        """Validated before it leaves for claire-api: the ticker becomes
        part of the work-item id, which every page renders."""
        ticker = body.get("ticker")
        exchange = body.get("exchange")
        self._check_symbol(ticker, exchange)
        if not isinstance(body.get("currency"), str) or \
                not _CCY_RE.fullmatch(body["currency"]):
            raise HTTPError(400, "currency must be a 3-letter code")
        if body.get("kind", "pipeline") not in ("pipeline",
                                                "sell_review"):
            raise HTTPError(400, "kind must be pipeline|sell_review")
        lot = body.get("lot_size", 1)
        if not isinstance(lot, int) or isinstance(lot, bool) or \
                not 1 <= lot <= 10000:
            raise HTTPError(400, "lot_size must be an integer 1..10000")
        fwd = {k: body[k] for k in ("ticker", "exchange", "currency",
                                    "kind", "lot_size", "instrument_id",
                                    "trigger") if k in body}
        return self._send(200, claire_api.post(
            self.ctx, "/api/run", fwd, timeout=10, what="starting the run"))

    def _assign_agent(self, body):
        # model / provider ids end up in pages and in provider API calls:
        # plain identifiers only
        for k in ("id", "provider_id", "fallback_provider_id"):
            v = body.get(k)
            if (v is not None or k != "fallback_provider_id") and (
                    not isinstance(v, str) or not _ID_RE.fullmatch(v)):
                raise HTTPError(400, f"{k} must be 2-30 chars [a-z0-9_-]")
        for k in ("model", "fallback_model"):
            v = body.get(k)
            if (v is not None or k == "model") and (
                    not isinstance(v, str) or not _MODEL_RE.fullmatch(v)):
                raise HTTPError(400, f"{k} must be a plain model name")
        if self.ctx.conn.execute("SELECT 1 FROM agents WHERE id=?",
                        (body["id"],)).fetchone() is None:
            raise HTTPError(400, f"unknown agent {body['id']!r}")
        try:
            registry.assign(self.ctx.conn, body["id"], body["provider_id"],
                            body["model"],
                            fallback_provider_id=body.get(
                                "fallback_provider_id"),
                            fallback_model=body.get("fallback_model"),
                            ts=int(self.ctx.clock()))
            return self._send(200, {"ok": True})
        except registry.CapabilityError as e:
            return self._send(400, {"error": str(e)})
