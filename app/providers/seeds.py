"""Default provider + agent roster (DESIGN.md §4/§8). Idempotent seeding —
INSERT OR IGNORE, so operator edits in the DB survive restarts."""
import json
import time

CAPS_FULL = json.dumps({"tool_calling": True, "structured_output": True})

DEFAULT_PROVIDER = "claude_max"

PROVIDERS = [
    # id, display, kind, base_url, api_key_ref, capabilities, cost_in, cost_out
    # The desk's default: Claude Agent SDK on the claude.ai login, so model
    # use counts against the Max subscription instead of API credit.
    ("claude_max", "Claude (Max subscription)", "claude_sdk", None, None,
     CAPS_FULL, 0.0, 0.0),
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

# The first template, given to EVERY agent. It told the debaters, the
# recorder, the screener and the chat agent that they were "analysing
# {ticker}" and should "use your tools" — four of them have no tools, three
# have no ticker, and only the analyst path ever filled the placeholder in,
# so the rest saw the braces literally (REVIEW-2026-11-11 B9). Kept verbatim
# only so seed() can recognise a prompt nobody has edited and upgrade it.
_P_V1 = """You are the {role} on Claire's trading desk, analysing {{ticker}}.
{brief}
Use your tools to gather REAL data — never invent numbers. Write your full
reasoning as markdown with the narrative tool, then emit the structured report.
Conviction is strength only (0..1); your signal/side carries direction.
State data timestamps; say plainly when data is missing rather than guessing."""

# One template per KIND of role. `{ticker}` appears only where the caller has
# one (registry.render_prompt fills it in).
_ANALYST = """You are the {role} analyst on Claire's trading desk, analysing {{ticker}}.
{brief}
Use your tools to gather REAL data — never invent numbers. Write your full
reasoning as markdown with the narrative tool, then emit the structured report.
Conviction is strength only (0..1); your signal carries direction.
State data timestamps; say plainly when data is missing rather than guessing."""

_DEBATER = """You are the {role} on Claire's trading desk, in the debate on {{ticker}}.
{brief}
You have no tools: work only from the reports and the debate record you are
given, and never invent numbers. Give your answer as the structured report.
Conviction is strength only (0..1); your side or direction says which way.
Say plainly when the record is missing something rather than guessing."""

_DESK = """You are the {role} on Claire's trading desk.
{brief}
Work only from what you are given — never invent numbers. Say plainly when
something is missing rather than guessing."""

_CHAT = """{brief}
Use your tools to get REAL data — never invent numbers. Say plainly when
something is missing rather than guessing."""

TEMPLATES = {"fundamental": _ANALYST, "technical": _ANALYST, "news": _ANALYST,
             "bull": _DEBATER, "bear": _DEBATER, "arbiter": _DEBATER,
             "claire_chat": _CHAT}          # everyone else: _DESK


def default_prompt(agent_id, brief) -> str:
    return TEMPLATES.get(agent_id, _DESK).format(
        role=agent_id.replace("_", " "), brief=brief)

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


MAINTAINER_PROMPT = (
    "You are the maintainer of Claire, Richard's AI trading desk. You are "
    "given the results of today's automated health checks and housekeeping "
    "as JSON. Write a short status note for Richard in plain English: at "
    "most 6 sentences, no markdown tables, no headings. Lead with the overall "
    "state. Name anything with status 'bad' or 'warn' and say concretely what "
    "he should do about it (e.g. 'sign in to Saxo SIM on the Saxo accounts "
    "page'). Mention what housekeeping did only briefly. Use only the facts "
    "given; never invent numbers, causes or fixes. You cannot change "
    "anything yourself and must not claim to have done so.")


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
        prompt = default_prompt(aid, brief)
        # a provider outage must not be a desk outage (§8): opus-class work
        # falls back to sonnet, which is the same family and always cheaper
        fb = "claude-sonnet-5" if model.startswith("claude-opus") else None
        conn.execute(
            "INSERT OR IGNORE INTO agents (id, display_name, system_prompt,"
            " version, tools, requires, provider_id, model, temperature,"
            " fallback_provider_id, fallback_model, updated_at)"
            " VALUES (?,?,?,1,?,?,?,?,?,?,?,?)",
            (aid, role.title(), prompt,
             json.dumps(tools), json.dumps(requires), DEFAULT_PROVIDER, model,
             temp if supports_temperature(model) else None,
             DEFAULT_PROVIDER if fb else None, fb, ts))
        # existing installs: adopt the fallback (on the agent's OWN provider,
        # so a subscription agent never falls back onto the metered API) and
        # drop rejected temperatures
        conn.execute(
            "UPDATE agents SET fallback_provider_id=CASE WHEN ? IS NULL"
            " THEN NULL ELSE provider_id END, fallback_model=?"
            " WHERE id=? AND fallback_model IS NULL", (fb, fb, aid))
        if not supports_temperature(model):
            conn.execute("UPDATE agents SET temperature=NULL WHERE id=?"
                         " AND model=?", (aid, model))
        conn.execute(
            "INSERT OR IGNORE INTO agent_versions (agent_id, version,"
            " system_prompt, changed_at) VALUES (?,1,?,?)",
            (aid, prompt, ts))
        # existing installs: a prompt still word-for-word the first template
        # was never edited, so it moves to the role's own template as a new
        # version (the old text stays in agent_versions). An edited prompt
        # is left exactly as its operator wrote it.
        cur = conn.execute(
            "UPDATE agents SET system_prompt=?, version=version+1,"
            " updated_at=? WHERE id=? AND system_prompt=?",
            (prompt, ts, aid, _P_V1.format(role=role, brief=brief)))
        if cur.rowcount == 1:
            conn.execute(
                "INSERT OR IGNORE INTO agent_versions (agent_id, version,"
                " system_prompt, changed_at) SELECT id, version,"
                " system_prompt, ? FROM agents WHERE id=?", (ts, aid))
    # the maintainer (DESIGN.md §23) is not a desk role analysing a ticker,
    # so it has its own prompt rather than the _P template. Text in, text
    # out: it holds no tools and decides nothing.
    conn.execute(
        "INSERT OR IGNORE INTO agents (id, display_name, system_prompt,"
        " version, tools, requires, provider_id, model, temperature,"
        " updated_at) VALUES ('maintainer','Maintainer',?,1,'[]','[]',?,?,"
        "NULL,?)", (MAINTAINER_PROMPT, DEFAULT_PROVIDER, "claude-haiku-4-5",
                    ts))
    conn.execute(
        "INSERT OR IGNORE INTO agent_versions (agent_id, version,"
        " system_prompt, changed_at) VALUES ('maintainer',1,?,?)",
        (MAINTAINER_PROMPT, ts))
