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
        return desk.start_run(inst)

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
        rows = [{k: r[k] for k in ("id", "ticker", "kind", "thesis_json",
                                   "expires_at")}
                for r in conn.execute(
                    "SELECT * FROM work_items WHERE state='awaiting_approval'")]
        return json.dumps(rows, default=str)

    @tool
    def portfolio_summary() -> str:
        """Positions, average costs, cash per account."""
        out = {}
        for acct in conn.execute("SELECT id, broker, base_currency"
                                 " FROM broker_accounts"):
            positions = []
            for r in conn.execute(
                    "SELECT instrument_id,"
                    " SUM(CASE WHEN qty_opened<0 THEN -qty_remaining"
                    "     ELSE qty_remaining END)/1e6 AS qty,"
                    " SUM(qty_remaining*cost_per_share_base/1e12) AS cost"
                    " FROM lots WHERE account_id=? AND qty_remaining>0"
                    " GROUP BY instrument_id", (acct["id"],)):
                positions.append(dict(r))
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

    return [run_pipeline, pipeline_status, list_pending_approvals,
            portfolio_summary, share_pl, broker_metrics, market_quote,
            market_chart, market_search, explain_thesis]


class Claire:
    def __init__(self, conn, repo, desk, market, *, env=None):
        self.conn, self.repo, self.desk, self.market = conn, repo, desk, market
        self.env = env
        self._histories = {}                    # cache over chat_threads table
        self._agent = None

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
            row = registry.agent_row(self.conn, "claire_chat")
            model = registry.model_for(self.conn, "claire_chat", env=self.env)
            self._agent = create_agent(
                model=model,
                tools=build_tools(self.conn, self.repo, self.desk, self.market),
                system_prompt=row["system_prompt"])
        return self._agent

    def ask_stream(self, text: str, thread: str = "main"):
        """NDJSON generator: {kind: text|tool|done|error}."""
        try:
            agent = self._ensure_agent()
        except Exception as e:                  # noqa: BLE001 — no key, etc.
            yield json.dumps({"kind": "error", "text": str(e)}) + "\n"
            yield json.dumps({"kind": "done"}) + "\n"
            return
        history = self._load(thread)            # survives restarts (§17)
        if len(history) > MAX_TURNS_PER_THREAD * 2:
            history.clear()                     # §17 auto-reset
        history.append({"role": "user", "content": text})
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
