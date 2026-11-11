-- desk.db DDL — DESIGN.md §7.
-- MONEY CONVENTION: every money/quantity column is an INTEGER count of
-- micro-units (1 share = 1_000_000 qty units; $1 = 1_000_000 amount units;
-- fx_rate is the rate ×1e6). Python converts to Decimal at the repo boundary;
-- floats never touch the ledger.

-- ── lifecycle ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS work_items (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,                      -- pipeline | sell_review | screen | override
                                           -- | manual (a trade you started: Sell button)
  ticker TEXT NOT NULL,
  state TEXT NOT NULL,                     -- running | awaiting_approval | approved
                                           -- | executing | done | rejected
                                           -- | expired | failed
  thread_id TEXT NOT NULL,                 -- ← LangGraph bridge
  thesis_json TEXT,                        -- Thesis snapshot at arbitration
  approval_token TEXT,                     -- single-use, server-minted, delivered
                                           -- ONLY in the dashboard approval card
  token_state TEXT,                        -- minted | pending_resume | burned
  expires_at INTEGER,                      -- session-aware TTL (§10)
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS events (        -- append-only audit trail
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id TEXT NOT NULL REFERENCES work_items(id),
  ts INTEGER NOT NULL, actor TEXT NOT NULL,
  from_state TEXT, to_state TEXT, payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_item ON events(item_id);

-- ── brokers & instruments ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS broker_accounts (
  id TEXT PRIMARY KEY, broker TEXT NOT NULL,
  -- 'live' is representable ONLY so the switch in app/live_trading.py can
  -- refuse it explicitly; a live account exists only via `bin/claire live setup`
  environment TEXT NOT NULL CHECK(environment IN ('paper','sim','live')),
  base_currency TEXT NOT NULL,
  fee_model TEXT NOT NULL,                 -- JSON: {type, per_trade, pct, min}
  risk_limits TEXT NOT NULL DEFAULT '{}',  -- JSON: max_order_base, max_position_pct,
                                           -- max_open_positions, max_trades_per_day
  external_ref TEXT, opened_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS instruments (
  id TEXT PRIMARY KEY,                     -- "SGX:C07", "NASDAQ:NVDA"
  ticker TEXT NOT NULL, exchange TEXT NOT NULL,
  currency TEXT NOT NULL, name TEXT
);
CREATE TABLE IF NOT EXISTS exchange_sessions (  -- static session calendar (§14a)
  exchange TEXT PRIMARY KEY,               -- NASDAQ, NYSE, LSE, SGX, HKEX, TSE, …
  tz TEXT NOT NULL,                        -- IANA: America/New_York, Asia/Singapore
  open_time TEXT NOT NULL, close_time TEXT NOT NULL,   -- local wall time, "09:30"
  lunch_break TEXT,                        -- HKEX/TSE: "12:00-13:00"
  holidays TEXT NOT NULL DEFAULT '[]'      -- JSON list of ISO dates, reviewed yearly
);

-- ── money: orders → immutable fill executions → lots → derived P&L ───────
CREATE TABLE IF NOT EXISTS orders (        -- broker order lifecycle; FILLS ARE ASYNC
  id TEXT PRIMARY KEY,
  work_item_id TEXT REFERENCES work_items(id),
  account_id TEXT NOT NULL REFERENCES broker_accounts(id),
  instrument_id TEXT NOT NULL REFERENCES instruments(id),
  side TEXT NOT NULL CHECK(side IN ('buy','sell')),
  qty INTEGER NOT NULL CHECK(qty > 0),     -- requested, micro-units
  limit_price INTEGER, stop_loss INTEGER, take_profit INTEGER,
  status TEXT NOT NULL CHECK(status IN ('pending_session','placed','partially_filled',
    'filled','cancelled','expired','rejected')),
  broker_order_id TEXT UNIQUE,
  expires_at INTEGER NOT NULL,             -- order TTL: cancel at broker if unfilled
  placed_at INTEGER, updated_at INTEGER NOT NULL,
  -- the row is written BEFORE the venue sees the order (so a crash can never
  -- leave an untracked live order); client_order_id is what the venue is
  -- given, submitted_at is the single-placer claim (NULL = not yet sent)
  client_order_id TEXT, submitted_at INTEGER,
  created_at INTEGER,                      -- decision time → max_trades_per_day
  reserved_base INTEGER,                   -- cash held back for an open buy, so
                                           -- two approvals can't spend it twice
  fx_rate INTEGER,                         -- instrument ccy → base ×1e6 at decision;
                                           -- the live limits measure in base ccy
  -- protective legs (§11a): a stop-loss / take-profit resting at the venue is
  -- a row of its own, pointing at the entry it protects. Legs skip the risk
  -- caps and the trade count (they reduce risk) and never expire on the
  -- desk's clock (GTC at the venue).
  parent_order_id TEXT,                    -- entry this leg protects
  leg TEXT,                                -- 'stop' | 'target' | NULL (not a leg)
  cancelled_for TEXT,                      -- desk order that cancelled this leg
  protect_state TEXT,                      -- on that desk order: pending | done
                                           -- | not_needed | failed
  protect_note TEXT, protect_alerted_at INTEGER,
  trail_pct REAL                           -- the approved trailing floor %:
                                           -- Saxo gets it as a trailing stop
);
CREATE TABLE IF NOT EXISTS executions (    -- IMMUTABLE; ONE ROW PER FILL —
                                           -- partial fills are multiple rows;
                                           -- corrections are reversal rows
  id TEXT PRIMARY KEY,
  order_id TEXT REFERENCES orders(id),
  account_id TEXT NOT NULL REFERENCES broker_accounts(id),
  instrument_id TEXT NOT NULL REFERENCES instruments(id),
  work_item_id TEXT REFERENCES work_items(id),
  side TEXT NOT NULL CHECK(side IN ('buy','sell')),
  qty INTEGER NOT NULL CHECK(qty > 0),
  price_native INTEGER NOT NULL CHECK(price_native > 0),
  currency TEXT NOT NULL,
  fx_rate INTEGER NOT NULL,                -- rate ×1e6, snapshotted at fill
  commission INTEGER NOT NULL DEFAULT 0,
  other_fees INTEGER NOT NULL DEFAULT 0,
  gross_base INTEGER NOT NULL, net_base INTEGER NOT NULL,
  intended_price INTEGER,                  -- thesis entry → slippage metric
  broker_order_id TEXT, broker_fill_id TEXT,
  executed_at INTEGER NOT NULL, recorded_at INTEGER NOT NULL,
  UNIQUE(broker_order_id, broker_fill_id)  -- idempotency: a fill records once
);
CREATE INDEX IF NOT EXISTS idx_exec_order ON executions(order_id);
CREATE TABLE IF NOT EXISTS lots (          -- one per opening execution;
                                           -- qty_opened < 0 marks a SHORT lot,
                                           -- qty_remaining is always |shares| left
  id TEXT PRIMARY KEY,
  open_execution_id TEXT NOT NULL REFERENCES executions(id),
  account_id TEXT NOT NULL, instrument_id TEXT NOT NULL,
  qty_opened INTEGER NOT NULL,
  qty_remaining INTEGER NOT NULL CHECK(qty_remaining >= 0),
  cost_per_share_base INTEGER NOT NULL,    -- ex-commission (allocated separately)
  commission_allocated INTEGER NOT NULL,   -- this lot's share of the open commission
  opened_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lots_pos ON lots(account_id, instrument_id, opened_at);
CREATE TABLE IF NOT EXISTS lot_closures (  -- realized P&L is born here, nowhere else
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  lot_id TEXT NOT NULL REFERENCES lots(id),
  close_execution_id TEXT NOT NULL REFERENCES executions(id),
  qty INTEGER NOT NULL,
  proceeds_base INTEGER NOT NULL, cost_base INTEGER NOT NULL,
  commission_base INTEGER NOT NULL,        -- both sides' allocated commissions
  realized_pl_base INTEGER NOT NULL,
  holding_days INTEGER, closed_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS cash_transactions (  -- EVERY cash movement; balance = SUM()
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id TEXT NOT NULL REFERENCES broker_accounts(id),
  kind TEXT NOT NULL CHECK(kind IN ('deposit','withdrawal','trade_buy','trade_sell',
    'commission','dividend','withholding_tax','fx_conversion','interest','adjustment')),
  amount_base INTEGER NOT NULL,            -- signed micro-units: +in / −out
  execution_id TEXT REFERENCES executions(id),
  note TEXT, occurred_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cash_acct ON cash_transactions(account_id, occurred_at);
CREATE TABLE IF NOT EXISTS corporate_actions (  -- splits adjust lots as auditable rows
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  instrument_id TEXT NOT NULL,
  kind TEXT NOT NULL,                      -- split | dividend | symbol_change
  ratio REAL, ex_date INTEGER NOT NULL, applied_at INTEGER, detail TEXT
);
CREATE TABLE IF NOT EXISTS broker_snapshots (   -- broker-reported truth, for reconciliation
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id TEXT NOT NULL, taken_at INTEGER NOT NULL,
  cash INTEGER, positions_json TEXT        -- micro-units
);
CREATE TABLE IF NOT EXISTS price_alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  instrument_id TEXT NOT NULL,
  rule TEXT NOT NULL,                      -- below_price | above_price | trail_pct
  threshold REAL NOT NULL, armed INTEGER NOT NULL DEFAULT 1,
  peak_base REAL,                          -- best price since entry:
                                           -- HIGHEST for a long, LOWEST for a short
  account_id TEXT, side INTEGER,           -- trail_pct rules are per position:
                                           -- (instrument, account, +1 long/-1 short);
                                           -- NULL = legacy / applies to any
  last_fired_at INTEGER, fire_count_today INTEGER DEFAULT 0,
  fire_count_date TEXT                     -- YYYY-MM-DD the counter belongs to
);

CREATE TABLE IF NOT EXISTS mcp_servers (    -- broker/tool MCP servers,
  id TEXT PRIMARY KEY,                      -- UI-editable like providers
  broker TEXT,                              -- broker adapter it serves, if any
  command TEXT NOT NULL,                    -- JSON list: launch command
  env_refs TEXT NOT NULL DEFAULT '[]',      -- JSON list of env var NAMES
  note TEXT, enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS schedules (      -- UI-editable job schedules; the
  job TEXT PRIMARY KEY,                     -- claire-api scheduler thread and
  description TEXT NOT NULL,                -- the dashboards watcher read this
  runs_in TEXT NOT NULL,                    -- claire-api | dashboards
  spec TEXT NOT NULL,                       -- JSON: {type:'interval',minutes}
                                            -- | {type:'daily',time,tz,days,
                                            --    region?,require_open?}
  enabled INTEGER NOT NULL DEFAULT 1,
  last_run_at INTEGER, last_result TEXT
);

CREATE TABLE IF NOT EXISTS service_health ( -- every guard publishes its own
  service TEXT NOT NULL,                    -- watcher | custodian | api
  checked_at INTEGER NOT NULL,
  ok TEXT NOT NULL,                         -- ok | degraded | stale
  detail TEXT
);

CREATE TABLE IF NOT EXISTS agent_reports (  -- the typed output each agent
  id INTEGER PRIMARY KEY AUTOINCREMENT,     -- produced, so the record survives
  work_item_id TEXT NOT NULL,               -- checkpoint loss (checkpoints.db
  agent_id TEXT NOT NULL,                   -- is disposable by design, §2)
  kind TEXT NOT NULL,                       -- analyst | debate | thesis
  payload TEXT NOT NULL,                    -- JSON of the validated object
  created_at INTEGER NOT NULL,
  UNIQUE(work_item_id, agent_id)
);
CREATE INDEX IF NOT EXISTS idx_reports_wi ON agent_reports(work_item_id);

CREATE TABLE IF NOT EXISTS watchlist (      -- names YOU always want screened
  ticker TEXT NOT NULL, exchange TEXT NOT NULL,
  note TEXT, added_at INTEGER NOT NULL,
  PRIMARY KEY (ticker, exchange)
);

CREATE TABLE IF NOT EXISTS chat_threads (   -- Claire's conversation memory —
  thread TEXT PRIMARY KEY,                  -- survives service restarts (§17)
  messages TEXT NOT NULL,                   -- JSON list of {role, content}
  updated_at INTEGER NOT NULL
);

-- ── views: derived, never stored (§7) ────────────────────────────────────
CREATE VIEW IF NOT EXISTS v_positions AS
SELECT account_id, instrument_id,
  SUM(CASE WHEN qty_opened<0 THEN -qty_remaining ELSE qty_remaining END)/1e6
    AS qty,
  SUM(CASE WHEN qty_opened<0 THEN 0
      ELSE qty_remaining*cost_per_share_base/1e12 END) AS cost_base,
  SUM(commission_allocated)/1e6 AS commissions
FROM lots WHERE qty_remaining>0 GROUP BY account_id, instrument_id;

CREATE VIEW IF NOT EXISTS v_share_pl AS
SELECT l.account_id, i.ticker, l.instrument_id,
  SUM(CASE WHEN l.qty_opened<0 THEN -l.qty_remaining
      ELSE l.qty_remaining END)/1e6 AS qty_open,
  SUM(l.qty_remaining*l.cost_per_share_base/1e12) AS open_cost,
  SUM(l.commission_allocated)/1e6 AS open_commissions,
  (SELECT COALESCE(SUM(c.realized_pl_base),0)/1e6 FROM lot_closures c
    JOIN lots l2 ON l2.id=c.lot_id
    WHERE l2.account_id=l.account_id AND l2.instrument_id=l.instrument_id)
    AS realized_pl
FROM lots l JOIN instruments i ON i.id=l.instrument_id
GROUP BY l.account_id, l.instrument_id;

CREATE VIEW IF NOT EXISTS v_broker_metrics AS
SELECT account_id, COUNT(*) AS fills, SUM(commission)/1e6 AS commissions,
  SUM(gross_base)/1e6 AS notional,
  AVG(CASE WHEN intended_price IS NOT NULL AND intended_price>0
      THEN (price_native-intended_price)*1.0/intended_price END)
    AS avg_slippage
FROM executions GROUP BY account_id;

CREATE VIEW IF NOT EXISTS v_thesis_outcomes AS  -- did the thesis make money,
SELECT w.id AS work_item_id, w.ticker, w.kind, w.state,  -- and which model
  (SELECT r.model FROM agent_runs r WHERE r.work_item_id=w.id
    AND r.agent_id='arbiter' ORDER BY r.started_at DESC LIMIT 1)
    AS arbiter_model,
  (SELECT COALESCE(SUM(c.realized_pl_base),0)/1e6 FROM lot_closures c
    JOIN executions e ON e.id=c.close_execution_id
    WHERE e.work_item_id=w.id) AS realized_pl,
  (SELECT COALESCE(SUM(cost_usd),0) FROM agent_runs r
    WHERE r.work_item_id=w.id) AS llm_cost
FROM work_items w WHERE w.thesis_json IS NOT NULL;

-- ── providers & agents (the multi-LLM layer) ─────────────────────────────
CREATE TABLE IF NOT EXISTS providers (
  id TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('anthropic','claude_sdk','openai_compatible','custom')),
  base_url TEXT,
  api_key_ref TEXT,                        -- env var NAME — never the secret itself
  capabilities TEXT NOT NULL,              -- JSON: tool_calling, structured_output, …
  cost_per_1k_in REAL, cost_per_1k_out REAL,
  enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS provider_models (
  provider_id TEXT REFERENCES providers(id), model TEXT NOT NULL,
  context_window INTEGER, supports_tools INTEGER, last_seen INTEGER,
  PRIMARY KEY (provider_id, model)
);
CREATE TABLE IF NOT EXISTS agents (
  id TEXT PRIMARY KEY,
  display_name TEXT,
  system_prompt TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
  tools TEXT NOT NULL,                     -- JSON list of tool names
  requires TEXT NOT NULL,                  -- JSON capability requirements
  provider_id TEXT NOT NULL REFERENCES providers(id),
  model TEXT NOT NULL, temperature REAL, max_tokens INTEGER,
  fallback_provider_id TEXT REFERENCES providers(id), fallback_model TEXT,
  enabled INTEGER DEFAULT 1, updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_versions (
  agent_id TEXT, version INTEGER, system_prompt TEXT, changed_at INTEGER,
  PRIMARY KEY(agent_id, version)
);
CREATE TABLE IF NOT EXISTS agent_runs (    -- per-LLM-call telemetry
  id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, work_item_id TEXT,
  provider_id TEXT, model TEXT,
  started_at INTEGER, ended_at INTEGER,
  tokens_in INTEGER, tokens_out INTEGER, cost_usd REAL,
  status TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS provider_health (
  provider_id TEXT, checked_at INTEGER, ok INTEGER, latency_ms INTEGER, error TEXT
);

-- ── live trading switch (DESIGN.md §22) — off by default ──────────────────
CREATE TABLE IF NOT EXISTS live_trading (
  id INTEGER PRIMARY KEY CHECK(id = 1),    -- singleton
  armed INTEGER NOT NULL DEFAULT 0,
  armed_at INTEGER, armed_by TEXT,
  disarmed_at INTEGER, disarm_reason TEXT,
  limits TEXT NOT NULL DEFAULT '{}'        -- JSON, base currency of the account
);
CREATE TABLE IF NOT EXISTS live_trading_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL, actor TEXT NOT NULL, event TEXT NOT NULL, payload TEXT
);
-- every live order that actually reached Saxo — feeds the daily limits
CREATE TABLE IF NOT EXISTS live_order_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL, account_id TEXT NOT NULL REFERENCES broker_accounts(id),
  order_id TEXT NOT NULL, notional_base INTEGER NOT NULL
);

-- ── price triggers (DESIGN.md §15a) — "buy 50 when it falls to 5.00" ─────
-- One-shot rules the owner sets on the Watchlist page. Prices are micro-units
-- in the instrument's currency, qty micro-shares, amount_base micro-units of
-- the account's base currency (a buy is sized EITHER by qty OR by amount).
-- kind is what the rule was at creation: a buy, an EXIT (sell of shares the
-- desk holds on that account) or a SHORT (sell of shares it does not hold).
-- mode 'auto' is a pre-approval given by the human when the rule was made;
-- it is refused for real-money accounts both here (CHECK) and in code.
CREATE TABLE IF NOT EXISTS price_triggers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticker TEXT NOT NULL, exchange TEXT NOT NULL,
  direction TEXT NOT NULL CHECK(direction IN ('buy','sell')),
  kind TEXT NOT NULL CHECK(kind IN ('buy','exit','short')),
  condition TEXT NOT NULL CHECK(condition IN ('lte','gte')),
  trigger_price INTEGER NOT NULL CHECK(trigger_price > 0),
  limit_price INTEGER NOT NULL CHECK(limit_price > 0),
  qty INTEGER CHECK(qty IS NULL OR qty > 0),
  amount_base INTEGER CHECK(amount_base IS NULL OR amount_base > 0),
  stop_loss INTEGER, take_profit INTEGER, trail_pct REAL,
  broker TEXT NOT NULL, account_id TEXT NOT NULL,
  mode TEXT NOT NULL DEFAULT 'ask' CHECK(mode IN ('ask','auto')),
  note TEXT,
  status TEXT NOT NULL DEFAULT 'active'
    CHECK(status IN ('active','fired','cancelled','expired','failed')),
  status_reason TEXT,
  created_at INTEGER NOT NULL, created_by TEXT NOT NULL,
  expires_at INTEGER NOT NULL,
  fired_at INTEGER, fired_price INTEGER, work_item_id TEXT,
  outcome TEXT,                              -- card | placed | refused: …
  last_checked_at INTEGER, last_price INTEGER, last_error TEXT,
  updated_at INTEGER NOT NULL,
  CHECK((qty IS NULL) <> (amount_base IS NULL)),
  CHECK(NOT (mode = 'auto' AND broker = 'saxo_live'))
);
CREATE INDEX IF NOT EXISTS idx_price_triggers_status
  ON price_triggers(status, ticker, exchange);

-- One row per broker account: when the custodian last checked it and last
-- reached its venue (Orders page "broker sync"). Overwritten in place each
-- run — bounded, one row per account, not a log.
CREATE TABLE IF NOT EXISTS broker_sync (
  account_id TEXT PRIMARY KEY REFERENCES broker_accounts(id),
  venue TEXT NOT NULL,                 -- 'Saxo sim' | 'built-in paper sim' …
  checked_at INTEGER NOT NULL,         -- last custodian run that had an adapter
  contact_at INTEGER,                  -- last successful call to the venue
  ok INTEGER NOT NULL,
  orders_checked INTEGER NOT NULL DEFAULT 0,
  error TEXT
);

-- One row per maintainer run (app/maintenance.py, DESIGN.md §23). The
-- maintainer keeps the newest 90 itself.
CREATE TABLE IF NOT EXISTS maintenance_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at INTEGER NOT NULL, finished_at INTEGER NOT NULL,
  trigger TEXT NOT NULL,               -- schedule | dashboard | test
  status TEXT NOT NULL,                -- ok | warn | bad
  headline TEXT NOT NULL,
  checks_json TEXT NOT NULL, actions_json TEXT NOT NULL,
  summary TEXT,                        -- the maintainer agent's note
  summary_error TEXT
);

-- ── tables that used to be created ad hoc by the module that owns them ──
-- (REVIEW-2026-11-11 §3.4). They live here now so one init builds the
-- whole database and bin/claire-reset can see every table; each module
-- still runs its own statement, read from this file by db.table_ddl().

CREATE TABLE IF NOT EXISTS sim_orders (
  id TEXT PRIMARY KEY,
  venue TEXT NOT NULL,               -- which built-in sim (alpaca, moomoo …)
  client_id TEXT,
  book_json TEXT NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS telegram_notices (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  work_item_id TEXT NOT NULL,
  state_key TEXT NOT NULL,           -- 'approval' | 'failed:<failed_at ts>'
  chat_id TEXT, message_id INTEGER, photo_message_id INTEGER,
  status TEXT NOT NULL,              -- sending | sent | error | closed
  attempts INTEGER NOT NULL DEFAULT 0,
  base_html TEXT,                    -- the message as first sent
  options_json TEXT,                 -- accounts, sizes, selection, pending
  thesis_hash TEXT,
  outcome TEXT,
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
  UNIQUE(work_item_id, state_key)
);

CREATE TABLE IF NOT EXISTS arbiter_notes (
  work_item_id TEXT NOT NULL,
  notes_hash TEXT NOT NULL,          -- sha256 of the exact notes explained
  summary TEXT NOT NULL,
  explained_json TEXT NOT NULL,      -- [plain explanation per note, in order]
  created_at INTEGER NOT NULL,
  PRIMARY KEY (work_item_id, notes_hash)
);


-- ── indexes for what the pages and loops actually query (§4.3) ──────────
-- every one of these was a full scan; agent_runs is read every 2 s by the
-- live-update poller
CREATE INDEX IF NOT EXISTS idx_agent_runs_agent ON agent_runs(agent_id, started_at);
CREATE INDEX IF NOT EXISTS idx_agent_runs_wi ON agent_runs(work_item_id);
CREATE INDEX IF NOT EXISTS idx_agent_runs_started ON agent_runs(started_at);
CREATE INDEX IF NOT EXISTS idx_health_service ON service_health(service, checked_at);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_wi ON orders(work_item_id);
CREATE INDEX IF NOT EXISTS idx_orders_position ON orders(account_id, instrument_id);
CREATE INDEX IF NOT EXISTS idx_work_items_state ON work_items(state);
CREATE INDEX IF NOT EXISTS idx_exec_position ON executions(account_id, instrument_id, executed_at);
CREATE INDEX IF NOT EXISTS idx_closures_lot ON lot_closures(lot_id);
