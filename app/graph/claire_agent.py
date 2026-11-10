"""Claire's conversational layer (DESIGN.md §17): create_agent + READ-ONLY
tools. She can tell you a thesis is waiting and argue its merits; she cannot
act on it — approve/resume/execute/cash tools are deliberately absent, and
pending-approval listings NEVER include tokens.
"""
import json

from langchain_core.tools import tool

from ..providers import registry
from ..tools.market import currency_for, yahoo_symbol

MAX_TURNS_PER_THREAD = 50      # context hygiene (§17): auto-reset, desk.db is
                               # the durable record so nothing of note is lost


# Who is talking to Claire right now, so a run she launches records it
# (the Trading page's "started by"). Set at the start of each chat turn from
# the chat thread and Bob's "[from Bob]" prefix. A plain holder rather than
# a contextvar: the Claude SDK runs her tools on its own threads/loop, where
# a contextvar set here would not be visible.
CURRENT_ASKER = {"who": "Claire's chat"}


def asker_for(text: str, thread: str) -> str:
    if (text or "").lstrip().startswith("[from Bob]"):
        return "Bob, via Claire's chat"
    if thread == "dashboard":
        return "you, via Claire's chat on the dashboard"
    return "Claire's chat (/ask: voice or another assistant)"


def build_tools(conn, repo, desk, market):
    @tool
    def run_pipeline(ticker: str, exchange: str = "NASDAQ") -> str:
        """Launch a full research pipeline on a ticker. Returns the work item
        id to follow. The verdict will await HUMAN approval on the dashboard."""
        from ..graph.state import Instrument
        sym = yahoo_symbol(ticker, exchange)
        ccy = currency_for(sym)
        inst = Instrument(id=f"{exchange}:{ticker}", ticker=ticker,
                          exchange=exchange,
                          currency="GBP" if ccy == "GBp" else ccy,
                          lot_size=100 if exchange == "SGX" else 1)
        return desk.start_run(inst,
                              trigger=f"chat: {CURRENT_ASKER['who']}")

    @tool
    def pipeline_status(work_item_id: str) -> str:
        """State and recent events for a run."""
        row = conn.execute("SELECT id, kind, ticker, state, thesis_json,"
                           " created_at, updated_at FROM work_items WHERE id=?",
                           (work_item_id,)).fetchone()
        if row is None:
            return "unknown work item"
        events = [dict(e) for e in conn.execute(
            "SELECT ts, actor, to_state FROM events WHERE item_id=?"
            " ORDER BY id DESC LIMIT 10", (work_item_id,))]
        return json.dumps({**dict(row), "events": events}, default=str)

    @tool
    def list_pending_approvals() -> str:
        """Theses awaiting human approval (summaries only — approving happens
        on the dashboard, never in chat)."""
        rows = [dict(r) for r in conn.execute(
            "SELECT id, ticker, kind, thesis_json, expires_at"
            " FROM work_items WHERE state='awaiting_approval'")]
        return json.dumps(rows, default=str)

    @tool
    def portfolio_summary() -> str:
        """Positions, average costs, cash per account, from the desk's own
        book. Two currencies per position — never mix them: the price per
        share is in the STOCK's currency (avg_cost_per_share +
        stock_currency, comparable with a broker's average open price), the
        totals are in the ACCOUNT's base currency (cost_total_base +
        base_currency, e.g. SGD for saxo-paper)."""
        out = {}
        for acct in conn.execute("SELECT id, broker, base_currency"
                                 " FROM broker_accounts"):
            positions = []
            for r in conn.execute(
                    "SELECT l.instrument_id, i.currency AS stock_currency,"
                    " SUM(CASE WHEN l.qty_opened<0 THEN -l.qty_remaining"
                    "     ELSE l.qty_remaining END)/1e6 AS qty,"
                    " SUM(l.qty_remaining*e.price_native/1e12) /"
                    "  (SUM(l.qty_remaining)/1e6) AS avg_cost_per_share,"
                    " SUM(l.qty_remaining*l.cost_per_share_base/1e12)"
                    "  AS cost_total_base"
                    " FROM lots l JOIN executions e ON e.id=l.open_execution_id"
                    " JOIN instruments i ON i.id=l.instrument_id"
                    " WHERE l.account_id=? AND l.qty_remaining>0"
                    " GROUP BY l.instrument_id", (acct["id"],)):
                d = dict(r)
                d["avg_cost_per_share"] = round(d["avg_cost_per_share"], 4)
                d["cost_total_base"] = round(d["cost_total_base"], 2)
                d["base_currency"] = acct["base_currency"]
                positions.append(d)
            out[acct["id"]] = {
                "broker": acct["broker"],
                "cash": repo.cash_balance(acct["id"]) / 1e6,
                "currency": acct["base_currency"],
                "positions": positions,
                "realized_pl": repo.realized_pl(acct["id"]) / 1e6}
        return json.dumps(out, default=str)

    @tool
    def share_pl(ticker: str) -> str:
        """Per-share P&L detail: open lots, realized closures, commissions."""
        rows = [dict(r) for r in conn.execute(
            "SELECT l.account_id, l.qty_opened/1e6 AS opened,"
            " l.qty_remaining/1e6 AS remaining,"
            " l.cost_per_share_base/1e6 AS cost_ps,"
            " l.commission_allocated/1e6 AS comm"
            " FROM lots l JOIN instruments i ON i.id=l.instrument_id"
            " WHERE i.ticker=?", (ticker,))]
        closures = [dict(r) for r in conn.execute(
            "SELECT c.qty/1e6 AS qty, c.realized_pl_base/1e6 AS pl,"
            " c.closed_at FROM lot_closures c JOIN lots l ON l.id=c.lot_id"
            " JOIN instruments i ON i.id=l.instrument_id WHERE i.ticker=?",
            (ticker,))]
        return json.dumps({"lots": rows, "closures": closures}, default=str)

    @tool
    def broker_metrics() -> str:
        """Per-venue execution quality: commissions, fills, slippage."""
        rows = [dict(r) for r in conn.execute(
            "SELECT account_id, COUNT(*) AS fills,"
            " SUM(commission)/1e6 AS commissions,"
            " SUM(gross_base)/1e6 AS notional,"
            " AVG(CASE WHEN intended_price IS NOT NULL THEN"
            "  (price_native-intended_price)*1.0/intended_price END)"
            "  AS avg_slippage"
            " FROM executions GROUP BY account_id")]
        return json.dumps(rows, default=str)

    @tool
    def market_quote(symbol: str) -> str:
        """Live quote for a Yahoo symbol."""
        return json.dumps(market.quote(symbol), default=str)

    @tool
    def market_chart(symbol: str, range_: str = "3mo") -> str:
        """OHLCV history (summarised tail) for a symbol."""
        c = market.chart(symbol, range_)
        closes = [x for x in c["close"] if x is not None]
        return json.dumps({"symbol": symbol, "currency": c["currency"],
                           "last_20_closes": closes[-20:]}, default=str)

    @tool
    def market_search(query: str) -> str:
        """Worldwide symbol search."""
        return json.dumps(market.search(query), default=str)

    @tool
    def explain_thesis(work_item_id: str) -> str:
        """The typed thesis plus the run's narrative summary."""
        row = conn.execute("SELECT thesis_json FROM work_items WHERE id=?",
                           (work_item_id,)).fetchone()
        return json.dumps({"thesis": row and row["thesis_json"]}, default=str)

    @tool
    def saxo_account(account: str = "live") -> str:
        """Richard's Saxo accounts, read-only: cash, total value, open
        positions with P/L, and working orders. account="live" is his REAL
        money account (SAXO_LIVE_ACCOUNT_ID); account="sim" is his Saxo paper account
        (SAXO_SIM_ACCOUNT_ID). Both are separate from the desk's own paper book — say
        which one you are describing, and never mix real and simulated
        figures. There is no way to trade from here."""
        from ..tools import saxo_live
        if account not in ("live", "sim"):
            return json.dumps({"error": "account must be 'live' or 'sim'"})
        return json.dumps(saxo_live.summary(account, with_orders=True),
                          default=str)

    return [run_pipeline, pipeline_status, list_pending_approvals,
            portfolio_summary, share_pl, broker_metrics, market_quote,
            market_chart, market_search, explain_thesis, saxo_account]


class Claire:
    def __init__(self, conn, repo, desk, market, *, env=None):
        self.conn, self.repo, self.desk, self.market = conn, repo, desk, market
        self.env = env
        self._histories = {}                    # cache over chat_threads table
        self._agent = None
        self._sdk_chat = None                   # (model, system, tools) on SDK

    def _load(self, thread):
        if thread not in self._histories:
            row = self.conn.execute(
                "SELECT messages FROM chat_threads WHERE thread=?",
                (thread,)).fetchone()
            self._histories[thread] = json.loads(row["messages"]) if row else []
        return self._histories[thread]

    def _save(self, thread):
        import time
        self.conn.execute(
            "INSERT INTO chat_threads (thread, messages, updated_at)"
            " VALUES (?,?,?) ON CONFLICT(thread) DO UPDATE SET"
            " messages=excluded.messages, updated_at=excluded.updated_at",
            (thread, json.dumps(self._histories[thread]), int(time.time())))

    def _ensure_agent(self):
        if self._agent is None:
            from langchain.agents import create_agent

            from ..providers import claude_sdk
            row = registry.agent_row(self.conn, "claire_chat")
            model = registry.model_for(self.conn, "claire_chat", env=self.env)
            tools = build_tools(self.conn, self.repo, self.desk, self.market)
            if claude_sdk.is_sdk(model):
                # Max subscription: the SDK owns the tool loop
                self._sdk_chat = (model, row["system_prompt"], tools)
                self._agent = model
            else:
                self._agent = create_agent(model=model, tools=tools,
                                           system_prompt=row["system_prompt"])
        return self._agent

    # a refusal by the model's safety filter, not a desk error. Once a long
    # thread trips it, EVERY later call resends that history and trips it
    # again (2026-11-06: 16 of 17 calls failed) — so retry, don't repeat.
    REFUSED = ("safeguards flagged", "usage policy", "aup")

    @classmethod
    def _refused(cls, err) -> bool:
        e = str(err).lower()
        return any(m in e for m in cls.REFUSED)

    def _ask_stream_sdk(self, history, thread):
        """Answer on the subscription. If the model refuses: try the backup
        model; if that refuses too, try once more on a FRESH conversation
        (only this question) and keep that as the thread from now on."""
        from ..providers import claude_sdk
        chat, system, tools = self._sdk_chat
        attempts = [("primary", chat, history)]
        if chat.fallback_model and chat.fallback_model != chat.model:
            attempts.append(("backup", chat._copy(model=chat.fallback_model,
                                                  fallback_model=None),
                             history))
        attempts.append(("fresh", chat._copy(
            model=chat.fallback_model or chat.model, fallback_model=None),
            [history[-1]]))
        last_err = None
        for label, model, msgs_in in attempts:
            msgs = [(m["role"], m["content"]) for m in msgs_in]
            _, prompt = claude_sdk.split_messages(msgs)
            final, streamed = "", False
            try:
                for kind, payload in claude_sdk.stream_chat(model, system,
                                                            prompt, tools):
                    if kind == "tool":
                        streamed = True
                        yield json.dumps({"kind": "tool", **payload},
                                         default=str) + "\n"
                    elif kind == "text":
                        streamed = True
                        yield json.dumps({"kind": "text",
                                          "text": payload}) + "\n"
                    elif kind == "done":
                        final = payload or final
            except Exception as e:              # noqa: BLE001
                last_err = e
                # only a refusal before anything was said is worth another
                # try; anything else (or a half-streamed answer) is final
                if self._refused(e) and not streamed:
                    continue
                break
            if label == "fresh":
                # the old history kept being refused: start over from here
                history[:] = [history[-1]]
                yield json.dumps({"kind": "text", "text":
                                  "\n\n_(I started a fresh conversation — "
                                  "the earlier one kept being refused by "
                                  "the model's safety filter.)_"}) + "\n"
            history.append({"role": "assistant", "content": final})
            self._save(thread)
            yield json.dumps({"kind": "done"}) + "\n"
            return
        history.pop()                           # don't keep an unanswered turn
        yield json.dumps({"kind": "error", "text": str(last_err)[:400]}) + "\n"
        yield json.dumps({"kind": "done"}) + "\n"

    def ask_stream(self, text: str, thread: str = "main"):
        """NDJSON generator: {kind: text|tool|done|error}."""
        try:
            agent = self._ensure_agent()
        except Exception as e:                  # noqa: BLE001 — no key, etc.
            yield json.dumps({"kind": "error", "text": str(e)}) + "\n"
            yield json.dumps({"kind": "done"}) + "\n"
            return
        CURRENT_ASKER["who"] = asker_for(text, thread)
        history = self._load(thread)            # survives restarts (§17)
        if len(history) > MAX_TURNS_PER_THREAD * 2:
            history.clear()                     # §17 auto-reset
        history.append({"role": "user", "content": text})
        if self._sdk_chat is not None:
            yield from self._ask_stream_sdk(history, thread)
            return
        final = ""
        try:
            for update in agent.stream({"messages": list(history)},
                                       config={"recursion_limit": 50},
                                       stream_mode="updates"):
                for node, data in (update or {}).items():
                    for m in (data or {}).get("messages", []):
                        kind = getattr(m, "type", "")
                        if kind == "ai":
                            calls = getattr(m, "tool_calls", None) or []
                            for c in calls:
                                yield json.dumps(
                                    {"kind": "tool", "name": c.get("name"),
                                     "args": c.get("args")},
                                    default=str) + "\n"
                            if isinstance(m.content, str) and m.content:
                                final = m.content
                                yield json.dumps({"kind": "text",
                                                  "text": m.content}) + "\n"
            history.append({"role": "assistant", "content": final})
            self._save(thread)
            yield json.dumps({"kind": "done"}) + "\n"
        except Exception as e:                  # noqa: BLE001
            yield json.dumps({"kind": "error", "text": str(e)[:400]}) + "\n"
            yield json.dumps({"kind": "done"}) + "\n"
