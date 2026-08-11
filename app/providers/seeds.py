"""Default provider + agent roster (DESIGN.md §4/§8). Idempotent seeding —
INSERT OR IGNORE, so operator edits in the DB survive restarts."""
import json
import time

CAPS_FULL = json.dumps({"tool_calling": True, "structured_output": True})

PROVIDERS = [
    # id, display, kind, base_url, api_key_ref, capabilities, cost_in, cost_out
    ("anthropic", "Anthropic", "anthropic", None, "ANTHROPIC_API_KEY",
     CAPS_FULL, 0.003, 0.015),
    ("openai", "OpenAI", "openai_compatible", "https://api.openai.com/v1",
     "OPENAI_API_KEY", CAPS_FULL, 0.0025, 0.01),
    ("deepseek", "DeepSeek", "openai_compatible", "https://api.deepseek.com/v1",
     "DEEPSEEK_API_KEY", CAPS_FULL, 0.00027, 0.0011),
    ("xai", "xAI Grok", "openai_compatible", "https://api.x.ai/v1",
     "XAI_API_KEY", CAPS_FULL, 0.003, 0.015),
    ("local", "Local (Ollama)", "openai_compatible", "http://127.0.0.1:11434/v1",
     None, CAPS_FULL, 0.0, 0.0),
]

_P = """You are the {role} on Claire's trading desk, analysing {{ticker}}.
{brief}
Use your tools to gather REAL data — never invent numbers. Write your full
reasoning as markdown with the narrative tool, then emit the structured report.
Conviction is strength only (0..1); your signal/side carries direction.
State data timestamps; say plainly when data is missing rather than guessing."""

AGENTS = [
    # id, model, temperature, tools, requires, brief
    ("screener", "claude-sonnet-5", 0.3,
     ["market_screener", "market_quote", "market_search", "write_narrative"],
     ["tool_calling"],
     "Scan the requested markets and shortlist 1-3 liquid candidates with a "
     "one-line reason each. Favour names with fresh catalysts."),
    ("fundamental", "claude-sonnet-5", 0.2,
     ["market_quote", "market_chart", "web_search", "fetch_page", "write_narrative"],
     ["tool_calling", "structured_output"],
     "Assess financial health, valuation vs peers and history, moat, and "
     "capital allocation."),
    ("technical", "claude-sonnet-5", 0.2,
     ["market_chart", "run_python", "write_narrative"],
     ["tool_calling", "structured_output"],
     "Compute trend, RSI, MACD, and support/resistance from real OHLCV via "
     "run_python. Never eyeball numbers a computation can produce."),
    ("news", "claude-haiku-4-5", 0.3,
     ["web_search", "fetch_page", "write_narrative"],
     ["tool_calling", "structured_output"],
     "Find headlines, catalysts, and sentiment from the last two weeks; date "
     "every claim."),
    ("bull", "claude-sonnet-5", 0.4, [], ["structured_output"],
     "Read the analyst reports and make the STRONGEST honest case FOR the "
     "trade."),
    ("bear", "claude-sonnet-5", 0.4, [], ["structured_output"],
     "Read the analyst reports AND the bull case; rebut it point by point and "
     "make the case AGAINST. Timing and risk arguments count."),
    ("arbiter", "claude-opus-5", 0.2, [], ["structured_output"],
     "Weigh the debate. You are the SOLE producer of theses. Every number in "
     "your thesis must be defensible from the record; pass when unconvinced. "
     "Direction sell requires an exit/short rationale."),
    ("claire_chat", "claude-opus-5", 0.5,
     ["run_pipeline", "pipeline_status", "list_pending_approvals",
      "portfolio_summary", "share_pl", "broker_metrics", "market_quote",
      "market_chart", "market_search", "explain_thesis"],
     ["tool_calling"],
     "You are Claire, the desk's voice. Answer from desk.db via tools; launch "
     "research when asked. You can DESCRIBE pending approvals but hold no "
     "approve/execute/cash tools — approvals happen on the dashboard only."),
    ("recorder", "claude-haiku-4-5", 0.2, [], [],
     "Summarise a completed run's outcome in three factual sentences."),
]


def seed(conn, *, ts=None):
    from .registry import supports_temperature
    ts = ts or int(time.time())
    for row in PROVIDERS:
        conn.execute(
            "INSERT OR IGNORE INTO providers (id, display_name, kind, base_url,"
            " api_key_ref, capabilities, cost_per_1k_in, cost_per_1k_out, enabled)"
            " VALUES (?,?,?,?,?,?,?,?,1)", row)
    for aid, model, temp, tools, requires, brief in AGENTS:
        role = aid.replace("_", " ")
        # a provider outage must not be a desk outage (§8): opus-class work
        # falls back to sonnet, which is the same family and always cheaper
        fb = "claude-sonnet-5" if model.startswith("claude-opus") else None
        conn.execute(
            "INSERT OR IGNORE INTO agents (id, display_name, system_prompt,"
            " version, tools, requires, provider_id, model, temperature,"
            " fallback_provider_id, fallback_model, updated_at)"
            " VALUES (?,?,?,1,?,?,?,?,?,?,?,?)",
            (aid, role.title(), _P.format(role=role, brief=brief),
             json.dumps(tools), json.dumps(requires), "anthropic", model,
             temp if supports_temperature(model) else None,
             "anthropic" if fb else None, fb, ts))
        # existing installs: adopt the fallback and drop rejected temperatures
        conn.execute(
            "UPDATE agents SET fallback_provider_id=?, fallback_model=?"
            " WHERE id=? AND fallback_model IS NULL",
            ("anthropic" if fb else None, fb, aid))
        if not supports_temperature(model):
            conn.execute("UPDATE agents SET temperature=NULL WHERE id=?"
                         " AND model=?", (aid, model))
        conn.execute(
            "INSERT OR IGNORE INTO agent_versions (agent_id, version,"
            " system_prompt, changed_at) VALUES (?,1,?,?)",
            (aid, _P.format(role=role, brief=brief), ts))
