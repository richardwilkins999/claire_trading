# Claire — a LangGraph Multi-Agent Paper-Trading Desk

Build-from-scratch specification. Claire is an AI trading desk: a pipeline of
specialist LLM agents researches a stock, a bull and a bear debate it, an
arbiter issues a typed verdict, and **nothing executes without explicit human
approval verified by a single-use token**. Execution and accounting are
deterministic Python — no LLM ever touches money.

- **Source repo:** `claire_trading` (private, GitHub)
- **Deploy target:** `/opt/Claire` on a single Linux machine, systemd-managed
- **Stack:** Python 3.14, LangGraph + LangChain 1.x, SQLite, stdlib HTTP for
  dashboards. No Node.js anywhere.
- **Scope:** paper/simulation trading only, single user, localhost only.

Everything in this document is either verified on the target machine or a
direct consequence of a lesson learnt building an earlier prototype of this
desk (§19). Nothing speculative.

---

## 0. Verified foundations

Confirmed working on the target machine before this design was frozen:

- **langgraph 1.2.10 / langchain 1.3.14 / langchain-core 1.5.3** install and
  import cleanly on Python 3.14.4, including `StateGraph` and
  `langgraph-checkpoint-sqlite`.
- **The full human-in-the-loop cycle works:** `interrupt()` suspends a graph
  mid-run, state persists to SQLite, the process can die and restart, and
  `Command(resume=...)` continues from the exact node. This is the approval
  gate's foundation, tested end-to-end.
- First-party provider packages exist for every target: `langchain-anthropic`,
  `langchain-openai`, `langchain-deepseek`, `langchain-xai` (Grok),
  `langchain-ollama` (local models), plus `langchain-mcp-adapters` for broker
  MCP servers.
- **Yahoo Finance keyless endpoints** (chart, spark, search, screener) proven
  for live quotes, OHLCV, worldwide symbol search, and full-exchange listings.
- **Three broker MCP servers** proven to launch and respond: the official
  `alpaca-mcp-server` (paper), the community `moomoo-api-mcp` (needs the local
  OpenD gateway), and a custom FastMCP server for the Saxo OpenAPI (SIM only).
- The multi-agent debate pattern (parallel analysts → bull → bear → arbiter)
  demonstrably changes outcomes: in live runs, unanimous bullish analysts were
  overruled to "pullback-only at half size" by bear-case timing evidence.

---

## 1. Design principles (non-negotiable)

1. **Paper/simulation only.** Broker adapters refuse anything but paper/SIM
   environments — in code, not in prompts. The DB schema cannot even represent
   a live account (`CHECK(environment IN ('paper','sim'))`).
2. **Human approval gate, server-authorized.** LangGraph's `interrupt()`
   provides the pause; **our server decides whether a resume is legitimate**
   via a single-use token. The framework will resume for any caller — the
   authorization layer above it is ours.
3. **Deterministic code owns money.** Order placement, position sizing, fees,
   FX, lot matching, and P&L are plain Python. LLMs produce *views*; code
   produces *transactions*.
4. **Adversarial debate before decisions**, enforced by graph edges: bear
   cannot run before bull, the arbiter cannot run before both.
5. **Typed state everywhere.** Every number an agent emits passes through a
   Pydantic schema. Free text in a numeric field is unrepresentable.
6. **Every LLM call is metered** — provider, model, tokens, cost, latency, per
   run, in the database. Model choice becomes an evidence-based decision.
7. **Honest degradation.** Provider outages fall back or fail loudly; missing
   credentials and stale data are reported, never papered over.

---

## 2. Architecture

```
        you ──── chat UI / dashboards / curl ────┐
                                                 ▼
 ┌─────────────────┐  authorizes approvals  ┌────────────────────────────────┐
 │   DASHBOARDS    │───────────────────────▶│      CLAIRE  (/opt/Claire)     │
 │   :7787 stdlib  │  resume w/ token       │      LangGraph app  :7788      │
 │  pages + APIs   │◀───SSE, desk.db────────│                                │
 └─────────────────┘                        │  ┌──────────────────────────┐  │
                                            │  │ Claire chat agent        │  │
 ┌─────────────────┐   breach → sell run    │  │ tools: run_pipeline,     │  │
 │  PRICE WATCHER  │───────────────────────▶│  │ desk queries (read-only) │  │
 │  deterministic  │                        │  └───────────┬──────────────┘  │
 └─────────────────┘                        │              ▼                 │
                                            │  ┌──────────────────────────┐  │
 ┌─────────────────┐   15-min timer         │  │ PIPELINE GRAPH (per run) │  │
 │   CUSTODIAN     │──expiry, reconcile────▶│  │ analysts ∥ → bull → bear │  │
 │  deterministic  │                        │  │ → arbiter → ⏸ interrupt  │  │
 └─────────────────┘                        │  │ → execute → record       │  │
                                            │  └──────────────────────────┘  │
                                            │  per-node LLM provider:        │
                                            │  anthropic│openai│deepseek│    │
                                            │  xai│ollama … (DB-configured)  │
                                            └───────┬───────────┬────────────┘
                                                    ▼           ▼
                                             checkpoints.db   desk.db
                                             (framework)      (business record)
                                                    │
                                       broker MCP: alpaca │ saxo │ moomoo
```

| Process | Port | Runtime | Purpose |
|---|---|---|---|
| `claire-api` | 7788 | FastAPI/uvicorn | chat, pipeline control, graph resume |
| `dashboards` | 7787 | stdlib Python | pages, market data, **approval authorization**, watcher |
| `custodian` | — | Python, timer | TTL expiry, reconciliation, orphan detection |

The chat contract (`POST /ask` streaming NDJSON, `GET /status`, `GET /feed`)
is a deliberate integration seam: any future assistant, voice front-end, or
messaging bridge talks to Claire through it without knowing her internals.

**Two databases on purpose.** `checkpoints.db` holds LangGraph's serialized
run state — framework-owned schema, version-churning, disposable (losing it
aborts in-flight runs, nothing more). `desk.db` is the durable business record
with our schema and migrations. Never join across them; the bridge is one
column, `work_items.thread_id`.

---

## 3. Repository and deploy layout

Repo `claire_trading` mirrors the deploy tree:

```
claire_trading/                    →  /opt/Claire/
├── app/
│   ├── graph/
│   │   ├── pipeline.py            the trading StateGraph (§5)
│   │   ├── state.py               Pydantic state + schemas (§6)
│   │   ├── nodes_analysis.py      analyst / bull / bear / arbiter LLM nodes
│   │   ├── nodes_execution.py     deterministic gate / execute / record
│   │   └── claire_agent.py        conversational Claire (create_agent)
│   ├── providers/
│   │   ├── registry.py            DB-backed provider/model resolution (§8)
│   │   └── health.py              probes, capability checks
│   ├── tools/
│   │   ├── market.py              Yahoo chart/quote/screener/fx (§14)
│   │   ├── search.py              web search (Tavily / DuckDuckGo)
│   │   ├── files.py               narrative I/O, jailed to var/narratives
│   │   └── brokers.py             MCP adapters (langchain-mcp-adapters)
│   ├── accounting/
│   │   ├── schema.sql             desk.db DDL (§7)
│   │   ├── repo.py                typed repository — the ONLY desk.db writer
│   │   └── lots.py                FIFO lot matching, realized P&L
│   ├── api/main.py                :7788 endpoints (§10)
│   ├── watcher.py                 price-alert loop (§15)
│   └── custodian.py               reaper + reconciler (§12)
├── web/                           dashboard pages (§13)
├── mcp/saxo_mcp.py                custom Saxo OpenAPI MCP server
├── etc/claire.env.example         template — real claire.env is 0600, never committed
├── systemd/                       claire-api, dashboards, timers
├── tests/                         §16
└── var/                           runtime only, gitignored:
    ├── desk.db  ·  checkpoints.db
    ├── narratives/<work_item>/*.md
    └── logs/
```

Bootstrap (the only root step is the first line):

```sh
sudo mkdir /opt/Claire && sudo chown $USER:$USER /opt/Claire
git clone git@github.com:<user>/claire_trading /opt/Claire
python3.14 -m venv /opt/Claire/venv
/opt/Claire/venv/bin/pip install -r requirements.txt   # PINNED versions
cp etc/claire.env.example etc/claire.env && chmod 600 etc/claire.env
```

**Pin every dependency and upgrade deliberately.** The LangChain ecosystem
moves fast — a checkpoint-library context-manager API changed between releases
*during the pre-design verification for this document*. Upgrades are scheduled
work with the test suite as the gate, never a side effect.

Gitignored: `etc/claire.env` (secrets), all of `var/` (state is data, not
source). Committed: `.env.example` templates only.

---

## 4. The agent roster

| Agent | Kind | LLM? | Default model | Role |
|---|---|---|---|---|
| screener | tool-using agent | yes | claude-sonnet | scan markets, shortlist candidates |
| fundamental | tool-using agent → `AnalystReport` | yes | claude-sonnet | financials, valuation, moat |
| technical | tool-using agent → `AnalystReport` | yes | claude-sonnet | trend/RSI/MACD/S-R from real OHLCV |
| news | tool-using agent → `AnalystReport` | yes | claude-haiku | headlines, catalysts, sentiment |
| bull | one structured call → `DebateCase` | yes | claude-sonnet | strongest case FOR |
| bear | one structured call → `DebateCase` | yes | claude-sonnet | case AGAINST + point-by-point rebuttal of bull |
| arbiter | one structured call → `Thesis` | yes | claude-opus | weighs the debate; **sole producer of theses** |
| claire_chat | `create_agent` + read-only tools | yes | claude-opus | conversation, status, launching runs |
| **executor** | **plain Python node** | **no** | — | places bracket orders |
| **recorder / reconciler** | **plain Python** | mostly no | haiku (prose summary only) | lots, cash, ledger |
| watcher / custodian | plain Python | no | — | price alerts, TTLs, reconciliation |

Two deliberate asymmetries:

- **Everything that touches money has no LLM.** An approved typed thesis
  contains every number needed to place a bracket order, so execution is a
  function, not a conversation.
- **Debate roles are single structured calls**, not tool loops — they read
  prior state and emit one validated object via
  `llm.with_structured_output(...)`. Cheaper, faster, and malformed output is
  retried at the framework layer instead of corrupting state.

Tool-using agents are built with `langchain.agents.create_agent` (the
LangChain 1.x constructor, LangGraph-based underneath), with model, tools, and
prompt loaded from the `agents` table — which is what makes per-agent provider
selection a database update instead of a deploy.

Model-tier rationale: opus-class for judgment (arbiter, chat), sonnet-class
for analysis, haiku-class for retrieval-heavy summarization. All reassignable
per agent from the dashboard (§8).

---

## 5. The pipeline graph

```python
# app/graph/pipeline.py (sketch; the real file adds error edges)
g = StateGraph(PipelineState)

g.add_node("prepare", prepare)                 # instrument, currency, live FX
g.add_node("fundamental", analyst("fundamental"))
g.add_node("technical",   analyst("technical"))
g.add_node("news",        analyst("news"))
g.add_node("bull", bull_node)
g.add_node("bear", bear_node)                  # receives bull's case → rebuts it
g.add_node("arbitrate", arbiter_node)
g.add_node("gate", approval_gate)              # interrupt() lives here
g.add_node("execute", execute_node)            # deterministic (§11)
g.add_node("record", record_node)

g.add_edge(START, "prepare")
for a in ("fundamental", "technical", "news"):
    g.add_edge("prepare", a)                   # parallel fan-out
    g.add_edge(a, "bull")                      # barrier: bull waits for all 3
g.add_edge("bull", "bear")                     # bear STRICTLY after bull
g.add_edge("bear", "arbitrate")
g.add_conditional_edges("arbitrate",
    lambda s: "gate" if s.thesis.direction in ("buy", "sell") else "record")
g.add_conditional_edges("gate",
    lambda s: {"approved": "execute", "rejected": "record",
               "expired": "record"}[s.approval.status])
g.add_edge("execute", "record")
g.add_edge("record", END)

pipeline = g.compile(checkpointer=SqliteSaver(conn))   # var/checkpoints.db
```

**Invariants enforced by structure, not prompts:** all three analysts complete
before debate; bear cannot run before bull; only the arbiter produces a
`Thesis`; a `pass` verdict never reaches the gate; nothing reaches `execute`
except through `gate`.

Each run's `thread_id` **is** its `work_items.id` (e.g.
`wi_2026-08-11_NVDA_a3f2`). Kill the service mid-run and
`pipeline.invoke(None, config)` resumes from the last checkpoint — finished
analysts don't re-run, and no half-done work is silently lost.

The gate node:

```python
def approval_gate(state):
    decision = interrupt({                     # ⏸ suspend; checkpoint persists
        "ticker": state.thesis.ticker, "direction": state.thesis.direction,
        "conviction": state.thesis.conviction,
        "entry_low": state.thesis.entry_low, "entry_high": state.thesis.entry_high,
        "stop_loss": state.thesis.stop_loss, "take_profit": state.thesis.take_profit,
    })
    # Reached ONLY via the authorized resume path (§10) — the payload was
    # token-verified server-side before the graph ever sees it.
    return {"approval": Approval(**decision)}
```

A sell-review run (triggered by the watcher) is the same graph with
`kind=sell_review` — selling requires approval exactly like buying.

---

## 6. Typed state

```python
class AnalystReport(BaseModel):
    ticker: str
    agent: Literal["fundamental", "technical", "news"]
    signal: Literal["bullish", "neutral", "bearish"]
    conviction: float = Field(ge=-1.0, le=1.0)          # 0.0 = abstain
    summary: str = Field(max_length=300)
    narrative_path: str                                 # full prose on disk
    data_asof: datetime
    sources: list[str] = []

class DebateCase(BaseModel):
    side: Literal["bull", "bear"]
    key_points: list[str]
    rebuttals: list[str] = []                           # bear: vs bull, point by point
    conviction: float = Field(ge=-1.0, le=1.0)
    narrative_path: str

class Thesis(BaseModel):
    ticker: str
    direction: Literal["buy", "sell", "pass"]
    conviction: float = Field(ge=-1.0, le=1.0)
    entry_low: float | None = Field(default=None, gt=0)   # a range is two numbers,
    entry_high: float | None = Field(default=None, gt=0)  # never prose
    stop_loss: float | None = Field(default=None, gt=0)
    take_profit: float | None = None
    currency: str
    conditions: list[str] = []                           # caveats live HERE, as text
    narrative_path: str

    @model_validator(mode="after")
    def _guards(self):
        if self.direction == "buy":
            assert self.entry_low and self.stop_loss, "buy thesis needs entry+stop"
            assert self.stop_loss < self.entry_low, "stop must be below entry"
        return self

class Approval(BaseModel):
    status: Literal["approved", "rejected", "expired"]
    size_usd: float | None = Field(default=None, gt=0)
    broker: Literal["alpaca", "saxo", "moomoo"] | None = None
    actor: str                                           # "human" | "reaper"
    token: str
    at: datetime

class PipelineState(BaseModel):
    work_item_id: str
    ticker: str
    instrument: Instrument
    reports: Annotated[list[AnalystReport], operator.add]   # parallel-merge reducer
    bull: DebateCase | None = None
    bear: DebateCase | None = None
    thesis: Thesis | None = None
    approval: Approval | None = None
    execution_ids: list[str] = []
    errors: Annotated[list[str], operator.add] = []
```

Agents still write rich markdown — under `var/narratives/<work_item>/`,
referenced by `narrative_path`. Prose is commentary on the record; it is never
the record. Anything downstream code needs is a typed field, validated at the
moment of production.

---

## 7. desk.db — the business schema

`accounting/repo.py` is the **only writer**. Nodes and API handlers call it;
nothing else touches SQL; no LLM ever writes to this database.

```sql
-- ── lifecycle ─────────────────────────────────────────────────────────────
CREATE TABLE work_items (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,                      -- pipeline | sell_review | screen
  ticker TEXT NOT NULL,
  state TEXT NOT NULL,                     -- running | awaiting_approval | approved
                                           -- | executing | done | rejected
                                           -- | expired | failed
  thread_id TEXT NOT NULL,                 -- ← LangGraph bridge
  thesis_json TEXT,                        -- Thesis snapshot at arbitration
  approval_token TEXT,                     -- single-use, server-minted
  expires_at INTEGER,                      -- TTL
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
);
CREATE TABLE events (                      -- append-only audit trail
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id TEXT NOT NULL REFERENCES work_items(id),
  ts INTEGER NOT NULL, actor TEXT NOT NULL,
  from_state TEXT, to_state TEXT, payload TEXT
);

-- ── brokers & instruments ─────────────────────────────────────────────────
CREATE TABLE broker_accounts (
  id TEXT PRIMARY KEY, broker TEXT NOT NULL,
  environment TEXT NOT NULL CHECK(environment IN ('paper','sim')),  -- no 'live'
  base_currency TEXT NOT NULL,
  fee_model TEXT NOT NULL,                 -- JSON: {type, per_trade, pct, min}
  external_ref TEXT, opened_at INTEGER NOT NULL
);
CREATE TABLE instruments (
  id TEXT PRIMARY KEY,                     -- "SGX:C07", "NASDAQ:NVDA"
  ticker TEXT NOT NULL, exchange TEXT NOT NULL,
  currency TEXT NOT NULL, name TEXT
);

-- ── money: immutable executions → lots → derived P&L ─────────────────────
CREATE TABLE executions (                  -- IMMUTABLE; corrections are reversal rows
  id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL REFERENCES broker_accounts(id),
  instrument_id TEXT NOT NULL REFERENCES instruments(id),
  work_item_id TEXT REFERENCES work_items(id),
  side TEXT NOT NULL CHECK(side IN ('buy','sell')),
  qty REAL NOT NULL CHECK(qty > 0),
  price_native REAL NOT NULL CHECK(price_native > 0),
  currency TEXT NOT NULL,
  fx_rate REAL NOT NULL,                   -- snapshotted at fill; never recomputed
  commission REAL NOT NULL DEFAULT 0,
  other_fees REAL NOT NULL DEFAULT 0,
  gross_base REAL NOT NULL, net_base REAL NOT NULL,
  intended_price REAL,                     -- thesis entry → slippage metric
  broker_order_id TEXT,
  executed_at INTEGER NOT NULL, recorded_at INTEGER NOT NULL
);
CREATE TABLE lots (                        -- one per opening execution
  id TEXT PRIMARY KEY,
  open_execution_id TEXT NOT NULL REFERENCES executions(id),
  account_id TEXT NOT NULL, instrument_id TEXT NOT NULL,
  qty_opened REAL NOT NULL,
  qty_remaining REAL NOT NULL CHECK(qty_remaining >= 0),
  cost_per_share_base REAL NOT NULL,
  commission_allocated REAL NOT NULL,      -- this lot's share of the buy commission
  opened_at INTEGER NOT NULL
);
CREATE TABLE lot_closures (                -- realized P&L is born here, nowhere else
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  lot_id TEXT NOT NULL REFERENCES lots(id),
  close_execution_id TEXT NOT NULL REFERENCES executions(id),
  qty REAL NOT NULL,
  proceeds_base REAL NOT NULL, cost_base REAL NOT NULL,
  commission_base REAL NOT NULL,           -- both sides' allocated commissions
  realized_pl_base REAL NOT NULL,
  holding_days INTEGER, closed_at INTEGER NOT NULL
);
CREATE TABLE cash_transactions (           -- EVERY cash movement; balance = SUM()
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id TEXT NOT NULL REFERENCES broker_accounts(id),
  kind TEXT NOT NULL CHECK(kind IN ('deposit','withdrawal','trade_buy','trade_sell',
    'commission','dividend','withholding_tax','fx_conversion','interest','adjustment')),
  amount_base REAL NOT NULL,               -- signed: +in / −out
  execution_id TEXT REFERENCES executions(id),
  note TEXT, occurred_at INTEGER NOT NULL
);
CREATE TABLE corporate_actions (           -- splits adjust lots as auditable rows
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  instrument_id TEXT NOT NULL,
  kind TEXT NOT NULL,                      -- split | dividend | symbol_change
  ratio REAL, ex_date INTEGER NOT NULL, applied_at INTEGER, detail TEXT
);
CREATE TABLE broker_snapshots (            -- broker-reported truth, for reconciliation
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id TEXT NOT NULL, taken_at INTEGER NOT NULL,
  cash REAL, positions_json TEXT
);
CREATE TABLE price_alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  instrument_id TEXT NOT NULL,
  rule TEXT NOT NULL,                      -- below_price | above_price | drop_pct_from_entry
  threshold REAL NOT NULL, armed INTEGER NOT NULL DEFAULT 1,
  last_fired_at INTEGER, fire_count_today INTEGER DEFAULT 0
);

-- ── providers & agents (the multi-LLM layer) ─────────────────────────────
CREATE TABLE providers (
  id TEXT PRIMARY KEY,                     -- anthropic | openai | deepseek | xai | local …
  display_name TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('anthropic','openai_compatible','custom')),
  base_url TEXT,                           -- the whole trick for adding providers
  api_key_ref TEXT,                        -- env var NAME — never the secret itself
  capabilities TEXT NOT NULL,              -- JSON: tool_calling, structured_output, …
  cost_per_1k_in REAL, cost_per_1k_out REAL,
  enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE provider_models (
  provider_id TEXT REFERENCES providers(id), model TEXT NOT NULL,
  context_window INTEGER, supports_tools INTEGER, last_seen INTEGER,
  PRIMARY KEY (provider_id, model)
);
CREATE TABLE agents (
  id TEXT PRIMARY KEY,                     -- screener | fundamental | technical | news
                                           -- | bull | bear | arbiter | claire_chat
  display_name TEXT,
  system_prompt TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
  tools TEXT NOT NULL,                     -- JSON list of tool names
  requires TEXT NOT NULL,                  -- JSON capability requirements
  provider_id TEXT NOT NULL REFERENCES providers(id),
  model TEXT NOT NULL, temperature REAL, max_tokens INTEGER,
  fallback_provider_id TEXT REFERENCES providers(id), fallback_model TEXT,
  enabled INTEGER DEFAULT 1, updated_at INTEGER NOT NULL
);
CREATE TABLE agent_versions (              -- prompt history → rollback
  agent_id TEXT, version INTEGER, system_prompt TEXT, changed_at INTEGER,
  PRIMARY KEY(agent_id, version)
);
CREATE TABLE agent_runs (                  -- per-LLM-call telemetry
  id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, work_item_id TEXT,
  provider_id TEXT, model TEXT,
  started_at INTEGER, ended_at INTEGER,
  tokens_in INTEGER, tokens_out INTEGER, cost_usd REAL,
  status TEXT, error TEXT
);
CREATE TABLE provider_health (
  provider_id TEXT, checked_at INTEGER, ok INTEGER, latency_ms INTEGER, error TEXT
);
```

**Views (derived, never stored):**

- `v_positions` — open lots rolled up per instrument per account
- `v_share_pl` — per share: qty open, average cost, realized P&L (Σ closures),
  unrealized (open lots × live price), dividends net of withholding, total
  commissions, **total P&L across any number of buys and sells**
- `v_broker_metrics` — per venue: commission as % of notional, average
  slippage (`price_native` vs `intended_price`), fill rate, win rate, cash
  drift vs latest `broker_snapshots`
- `v_thesis_outcomes` — work_items → executions → lot_closures: *did this
  thesis make money, and which model wrote it?*

Worked example the accounting tests encode: buy 10 @ $200 (+$1 fee), buy 5 @
$220 (+$1), buy 10 @ $180 (+$1), then sell 12 @ $240 (−$1.50). FIFO consumes
all of lot A and 2 shares of lot B → realized **+$437.10** net of all fees;
13 shares remain at $189.35 average cost. Every figure traces to immutable
rows; nothing is ever hand-computed twice.

---

## 8. The provider layer

```python
# app/providers/registry.py
from langchain.chat_models import init_chat_model

def model_for(agent_id: str):
    a, p = repo.agent(agent_id), repo.provider(repo.agent(agent_id).provider_id)
    kw = dict(model=a.model, temperature=a.temperature, max_tokens=a.max_tokens)
    if p.kind == "anthropic":
        llm = init_chat_model(model_provider="anthropic",
                              api_key=env(p.api_key_ref), **kw)
    else:   # openai_compatible: OpenAI, DeepSeek, Grok/xAI, Ollama, LM Studio,
            # vLLM, OpenRouter, Together — a DB row each, zero code
        llm = init_chat_model(model_provider="openai", base_url=p.base_url,
                              api_key=env(p.api_key_ref) or "local", **kw)
    if a.fallback_provider_id:              # provider outage ≠ desk outage
        llm = llm.with_fallbacks([build(a.fallback_provider_id, a.fallback_model)])
    return llm.with_config(callbacks=[MeterCallback(agent_id)])   # → agent_runs
```

- **Capability gating at assignment time.** Assigning a provider without
  `structured_output` to bull/bear/arbiter, or without `tool_calling` to an
  analyst, is rejected by the API with the reason — not discovered mid-run.
- **Secrets never enter the DB.** `api_key_ref` is an env-var *name*; values
  live only in `etc/claire.env` (0600). `desk.db` stays safe to back up,
  inspect, and share.
- **Adding a provider — including a future local LLM — is one row:**
  `id=local, kind=openai_compatible, base_url=http://127.0.0.1:11434/v1`,
  plus a key in `claire.env` if needed. A health probe must pass before it
  becomes selectable. No code, no deploy.
- **Prompts don't port for free.** A prompt tuned on one model family behaves
  differently on another. Reassign low-stakes agents first (news, bull/bear)
  and judge from `agent_runs` × `v_thesis_outcomes` — the metering exists
  precisely so this is a measurement, not a vibe.

---

## 9. Tools layer

| Need | Implementation |
|---|---|
| web search | `search.py`: **Tavily** if `TAVILY_API_KEY` set, else DuckDuckGo (free, no key, noticeably weaker). Ship DDG default; recommend Tavily for news/fundamental quality. |
| page fetch | `httpx` + readability extraction, size-capped |
| file I/O | `files.py` — **jailed to `var/narratives/<work_item>/`**. Agents cannot roam the filesystem, by construction. |
| technical math | a sandboxed `run_python` tool: numpy/pandas over OHLCV the tool itself fetches — no shell access for any agent |
| brokers | `langchain-mcp-adapters` wrapping the three MCP servers |
| market data | `market.py` (§14) exposed as tools: quote, chart, screener, fx |

---

## 10. The approval flow, end to end

```
1. graph reaches gate → interrupt() → checkpoint persists
   claire-api: work_item → awaiting_approval
               token = secrets.token_hex(16); expires_at = now + 24h
               SSE event → dashboard approval card
2. human reviews the TYPED thesis on the dashboard, sets size + broker
   POST :7787/api/thesis-action {work_item_id, action:"approve", size_usd, broker}
3. dashboards server AUTHORIZES — all of:
     token matches ∧ unexpired ∧ unused ∧ state == awaiting_approval
   → burns the token, appends events row (actor: human)
   → POST :7788/internal/resume {status:"approved", size_usd, broker, token, actor}
4. claire-api: pipeline.invoke(Command(resume=payload), {"thread_id": wi})
   → gate returns → execute node runs → record → done
```

**The security property, stated plainly:** `Command(resume=…)` will resume for
*any* caller — LangGraph performs no authorization. Therefore
`/internal/resume` binds to localhost, requires a shared secret from
`claire.env`, and is **not** a tool any agent can call. The dashboards server
is the only component that converts a human click into a resume, and only
after token verification. No LLM — any provider, any prompt injection — has a
path to approving a trade. A chat message saying "approved" is just a chat
message.

Rejection is the same flow with `status:"rejected"`. Expiry is the custodian
calling the same endpoint with `status:"expired", actor:"reaper"` — the graph
finishes through `record`, so expired runs **terminate cleanly** instead of
leaking suspended checkpoints forever.

Reconciliation guard: the custodian flags any `approved` item with no
execution after 10 minutes and any `executing` item stuck past 30 — approvals
must never silently vanish.

---

## 11. Deterministic execution

```python
# nodes_execution.py — no LLM anywhere in this file
def execute_node(state):
    ap, th = state.approval, state.thesis
    px  = quote(state.instrument)                          # live price
    qty = position_qty(ap.size_usd, px, state.instrument)  # FX + lot-size floor
    check_cash(ap.broker, ap.size_usd)                     # refuse over-spend
    order = broker(ap.broker).place_bracket(               # paper/sim enforced here
        instrument=state.instrument, side=th.direction, qty=qty,
        limit=th.entry_high, stop_loss=th.stop_loss, take_profit=th.take_profit)
    ex = repo.record_execution(order, work_item=state.work_item_id,
                               intended_price=th.entry_low)
    repo.record_cash(ex)                                   # trade + commission rows
    return {"execution_ids": [ex.id]}
```

- **Bracket order is atomic** — entry and stop placed together. If a venue
  cannot attach the stop, the position is recorded as UNPROTECTED and surfaced
  on the dashboard; the resting stop at the broker is the primary protection,
  the watcher (§15) is the backstop.
- Fee models mirror the real venues so paper results stay honest:
  alpaca flat $0 · saxo 0.08% min $5 · moomoo flat $0.99 — stored per account
  in `fee_model`, applied by `repo`, never by a model.
- `record_execution` opens a lot (buy) or FIFO-matches into `lot_closures`
  (sell). Shorts are explicit: sell-to-open opens a negative lot,
  buy-to-cover closes it.
- All of this runs under a single writer lock; cash can never go negative and
  the same execution can never be recorded twice (idempotency key =
  `broker_order_id`).

---

## 12. The custodian

`custodian.py`, systemd timer every 15 minutes, deterministic, read-mostly:

- **Expire:** `awaiting_approval` past `expires_at` (default 24 h) → resume
  with `expired`; `approved` but unexecuted past 60 min → same, token burned.
  Expiry is enforced twice: by this reaper **and** on every read path — an
  expired item is never served as actionable even if the reaper is down.
- **Reconcile:** latest `broker_snapshots` vs the book — positions and cash.
  Drift is *flagged*, never silently corrected; the broker is presumed right.
- **Orphans:** executions without lots, lots with negative remainder,
  work-items `running` > 2 h, suspended checkpoints with no work_item.
- **Archive:** terminal items > 30 days; nightly `VACUUM INTO` backup of
  desk.db.
- Publishes a health row the dashboard renders as a tile — the safety net
  must not be able to fail silently.

The custodian **cannot** approve or execute: `/internal/resume` accepts only
`status:"expired"` from `actor:"reaper"`.

---

## 13. Dashboards (:7787, stdlib Python, no build step)

| Page | Purpose |
|---|---|
| `/` Mission Control | live graph of agents + broker MCP nodes, provider/model shown per agent, pulsing = currently executing (from `astream_events` → SSE); tiles: pending approvals, watcher health, custodian health, per-broker connectivity |
| `/control` | per-agent cards: status, live feed, dispatch, last run cost |
| `/markets` | 10 exchanges (NASDAQ, NYSE, LSE, SGX, HKEX, TSE, ASX, XETRA, Paris, NSE); curated list **and** full listing via screener, server-side sort by price/mcap, paginated; candlestick chart + SMA/RSI/volume; worldwide search; **send-to-pipeline** button |
| `/trading` | pipeline visualised per run: each agent's signal/conviction, debate cases, arbiter verdict, live feed |
| `/portfolio` | positions from `v_positions`, per-share P&L from `v_share_pl` (multi-lot detail), equity curve, cash + top-up/withdraw per account, alerts panel, broker-metrics comparison |
| `/agent/<id>` | drill-down: reports, run history with cost; **Model & Provider panel** — provider dropdown (incompatible greyed with reason), model list refreshed from the provider, fallback, temperature, a **Test button** (canned prompt → latency, cost, structured-output pass/fail *before* committing), prompt editor with version rollback |
| `/providers` | add/enable providers, key present/absent (never the value), health, cost config |
| `/approvals` (cards also on `/` and `/agent/arbiter`) | the typed thesis, **editable size with live share-count + fee + FX preview**, broker choice, ✔ Approve / ✖ Reject |

Charts: lightweight-charts from vendored JS (single file, no CDN dependency at
runtime). All pages read `desk.db` through the API — numbers come from typed
columns, never parsed out of prose.

---

## 14. Market data (proven specifics worth keeping)

Yahoo Finance keyless endpoints, from code proven in production use:

- **Chart:** `query1.finance.yahoo.com/v8/finance/chart/<sym>?range=&interval=`
  → OHLCV. **Spark (batch quotes):** v8 spark returns a *flat* dict
  `{SYM: {close, previousClose}}` — not the nested `spark.result` shape older
  docs suggest; when a market is closed `close` can be null → fall back to
  `previousClose`.
- **Search:** v1 search for worldwide symbol lookup.
- **Full-exchange listings:** the screener API requires the cookie+crumb
  dance (hit yahoo.com for cookies → `/v1/test/getcrumb` → pass `crumb=` on
  POST). Exchange codes: NMS/NYQ/LSE/SES/HKG/JPX/ASX/GER/PAR/NSI. Sort fields:
  `intradayprice`, `intradaymarketcap`. Sort server-side and paginate ~100 —
  full exchanges run to thousands of rows (HKEX ≈ 9,000+).
- **FX:** `<FROM><TO>=X` as a chart symbol; cache ~5 min.
- **Currency by suffix:** `.SI`→SGD, `.HK`→HKD, `.T`→JPY, `.AX`→AUD,
  `.DE`/`.PA`→EUR, `.NS`→INR, **`.L`→GBp — pence, 1/100 of GBP**; divide by
  100 before any FX math or every LSE position is wrong by 100×. Default USD.
- Set a real `User-Agent`; these endpoints are unofficial and can change —
  keep all Yahoo access in `market.py` so a breakage is one file.

---

## 15. The price watcher

Deterministic loop in the dashboards process (no LLM cost), every 10 minutes:

- Every open position automatically gets a **down-8%-from-entry** rule; users
  add `below_price` / `above_price` / `drop_pct_from_entry` rules per
  instrument.
- **Escalating re-alerts:** fire on first breach, then only after each further
  3% decline, capped at 6/instrument/day — a worsening position keeps nudging
  without spamming.
- On breach → launch a `sell_review` pipeline run → the arbiter's sell thesis
  lands as `awaiting_approval` like any other. **Selling requires the same
  human approval as buying.**
- Publishes health (`ok | degraded | stale`) — `degraded` if any position
  cannot be priced, `stale` if the loop stops beating. Rendered as a Mission
  Control tile.

Fire-and-forget HTTP from the watcher uses a **short timeout** (~10 s): a
long-timeout call from an alert loop piles up blocked threads when several
alerts land together.

---

## 16. Scheduling & services (systemd user units)

| Unit | Schedule | Purpose |
|---|---|---|
| `claire-api.service` | always | graph runtime + chat (7788) |
| `claire-dashboards.service` | always | pages + APIs + watcher (7787) |
| `claire-custodian.timer` | every 15 min | reaper + reconciler |
| `claire-analysis.timer` | Mon–Fri 21:00 | autonomous run: review open positions, screen, full pipeline on 1–2 candidates → verdicts land `awaiting_approval` |
| `claire-reconcile.timer` | daily 09:00 | broker snapshots → drift check |

All: `EnvironmentFile=/opt/Claire/etc/claire.env`, `Restart=always`.
Autonomous runs are started by a CLI (`claire-run --autonomous`), not by
prompting a chat model — starting work needs no LLM.

---

## 17. Claire's conversational layer

`claire_agent.py` — `create_agent`, thread-per-conversation checkpointing (so
conversation memory survives service restarts), model from the `agents` table,
and **read-only tools**:

`run_pipeline(ticker)` · `pipeline_status(id)` · `list_pending_approvals()` ·
`portfolio_summary()` · `share_pl(ticker)` · `broker_metrics()` ·
`market_quote/chart/search` · `explain_thesis(id)`

Deliberately absent: approve, resume, execute, and cash tools. Claire can tell
you a thesis is waiting and argue its merits; she cannot act on it.

Context hygiene for the persistent chat: auto-reset the thread after ~50 turns
(durable state lives in desk.db, so nothing of record is lost) and cap turns
per request as a runaway guard.

Served at `POST /ask` (streaming NDJSON: `{kind:"text"|"tool"|"done"|"error"}`),
`GET /status`, `GET /feed?since=` — the stable seam future front-ends
(voice, messaging, other assistants) integrate against.

---

## 18. Testing

Five layers, run by `tests/run.sh`; the money and authorization layers must be
runnable offline in under a second.

1. **Accounting (most important):** the §7 worked example as a fixture —
   exact closure rows, FIFO order, commission allocation both sides, shorts,
   splits via `corporate_actions`, and the invariant *cash balance == Σ
   cash_transactions* after every operation. Property-style: random
   buy/sell sequences must never produce negative lots or unbalanced cash.
2. **Graph:** compile with a `FakeChatModel` scripted per node — analysts fan
   out and all complete before bull; bear receives bull's case; `pass` never
   reaches the gate; interrupt fires with the typed payload; each resume
   status reaches the right terminal node; a killed-and-restarted run resumes
   without re-running finished nodes. No keys, no network.
3. **Authorization:** spoofed resume without token → 403; reused token → 409;
   expired → 410; `/internal/resume` refuses non-localhost / bad secret; no
   registered agent tool reaches resume.
4. **Providers:** capability gate rejects incompatible assignments; fallback
   fires on simulated provider failure; the meter records tokens and cost.
5. **Live smoke (auto-skips when services are down):** every page serves,
   `/ask` streams, a pipeline launches and interrupts against mocked brokers,
   FX is plausible, full-exchange listing is genuinely sorted.

---

## 19. Lessons learnt (hard-won; each shaped this design)

1. **Never let an agent maintain a shared data file.** An LLM keeping a JSON
   ledger invented new keys ad hoc — including one named
   `integrity_check_<date>`, data encoded in a key name — and raced other
   writers into corrupt state. Hence: one repository layer, one writer lock,
   LLMs write nothing of record.
2. **Free text in numeric fields is a live money bug.** A thesis field like
   `entry: none — re-entry above 30.50` gets a number scraped out of it by
   downstream code, and the number is the wrong one (a trigger, not an
   entry). Position sizing then uses it. Hence: typed schemas at the moment
   of production, prose quarantined to `conditions` and narratives.
3. **An approval must be unforgeable by any agent.** In an early prototype an
   agent copied a thesis template *including its approval field* — a trade
   was one step from executing with nobody having approved it. A chat message
   or file field can never be authorization; only a server-minted single-use
   token verified server-side counts.
4. **Sequencing by prompt fails eventually; sequencing by structure cannot.**
   "Run bear after bull" as an instruction is a request; as a graph edge it
   is a law. Same for "analysts in parallel" and "nothing executes except
   through the gate".
5. **FX must be snapshotted at fill time.** Recomputing historical P&L with
   today's rates silently rewrites history for every non-USD position.
6. **LSE quotes are in pence.** Treat `.L` prices as GBp and divide by 100
   before FX, or every UK position is wrong by two orders of magnitude.
7. **Persistent chat sessions grow without bound.** Auto-reset on a turn
   budget, with durable state on disk, keeps cost flat with zero memory loss
   that matters.
8. **Fire-and-forget HTTP needs short timeouts.** A 60-minute timeout in an
   alert path pinned every worker thread the first time three alerts fired
   together.
9. **Escalating re-alerts beat both extremes.** Alert-once means a collapsing
   position goes quiet; alert-always means alarm fatigue. Re-fire per further
   3% decline, capped daily, matched real usage.
10. **The safety net must publish its own health.** A watcher that dies
    silently is worse than no watcher — you *believe* you're protected.
    Every guard component exports `ok|degraded|stale` to the dashboard.
11. **Framework churn is real.** A checkpoint-library API changed between
    releases during this design's own verification. Pin everything; upgrades
    are scheduled, tested work.
12. **Meter every model call from day one.** Provider comparisons, cost
    control, and "which model writes profitable theses" are all queries —
    but only if the telemetry existed from the first run.

---

## 20. Build order

1. Repo scaffold, venv, pinned requirements, both DBs created from
   `schema.sql`, `etc/claire.env.example`.
2. **Accounting core + its tests** — repo layer, lot matcher, cash invariants.
   It must be bulletproof before anything can trade, and it has no LangGraph
   dependency.
3. Typed state models; provider registry with Anthropic only; meter callback.
4. Pipeline graph against `FakeChatModel` → graph tests green.
5. Approval gate + token authorization across both servers → authorization
   tests green.
6. Market data module (§14) + tools layer (search choice made here).
7. Real analyst/debate/arbiter nodes; first live pipeline run to an
   interrupt; approve it from the dashboard against a mocked broker.
8. Deterministic executor against mocked brokers, then the three MCP adapters.
9. Claire chat agent + `/ask` streaming contract.
10. Dashboards, Model & Provider panel, `/providers` page.
11. Watcher, custodian, timers.
12. Additional providers (OpenAI/DeepSeek/xAI rows), assigned to low-stakes
    agents first, judged from `agent_runs` × `v_thesis_outcomes`.

---

## 21. Known limitations

- Localhost-only, no authentication — must gain auth before any network
  exposure. The internal-resume secret is defense-in-depth, not a substitute.
- `checkpoints.db` is disposable by design: losing it aborts in-flight runs
  (they restart); the business record in desk.db is unaffected.
- Yahoo endpoints are unofficial; all access is isolated in one module.
- DuckDuckGo search (the keyless default) is materially weaker than a paid
  search API for news work; a Tavily key is the recommended upgrade.
- Corporate-action *detection* is manual at first (the schema and adjustment
  path exist; the automatic split-watcher ships later).
- Saxo SIM tokens expire every 24 h and need manual refresh until OAuth
  refresh is added.
