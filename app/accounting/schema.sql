-- desk.db DDL — DESIGN.md §7.
-- MONEY CONVENTION: every money/quantity column is an INTEGER count of
-- micro-units (1 share = 1_000_000 qty units; $1 = 1_000_000 amount units;
-- fx_rate is the rate ×1e6). Python converts to Decimal at the repo boundary;
-- floats never touch the ledger.

-- ── lifecycle ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS work_items (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,                      -- pipeline | sell_review | screen
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
  environment TEXT NOT NULL CHECK(environment IN ('paper','sim')),  -- no 'live'
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
  placed_at INTEGER, updated_at INTEGER NOT NULL
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
  rule TEXT NOT NULL,                      -- below_price | above_price | drop_pct_from_entry
  threshold REAL NOT NULL, armed INTEGER NOT NULL DEFAULT 1,
  last_fired_at INTEGER, fire_count_today INTEGER DEFAULT 0,
  fire_count_date TEXT                     -- YYYY-MM-DD the counter belongs to
);

-- ── providers & agents (the multi-LLM layer) ─────────────────────────────
CREATE TABLE IF NOT EXISTS providers (
  id TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('anthropic','openai_compatible','custom')),
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
