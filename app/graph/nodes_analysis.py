"""LLM nodes (DESIGN.md §4): tool-using analysts, single-structured-call
debaters, the arbiter. Prompts and models come from the `agents` table; every
call is metered; structured output is validated at the moment of production.

`structured_factory` / `agent_factory` are injectable so the graph runs
offline in tests; production uses the registry.
"""
import json
from datetime import datetime, timezone

from ..providers import registry
from .state import AnalystReport, DebateCase, Thesis


# ── prompt renderers (pure; unit-testable) ───────────────────────────────
def render_reports(state) -> str:
    parts = []
    for r in state.reports:
        parts.append(f"### {r.agent} — {r.signal} (conviction {r.conviction})\n"
                     f"{r.summary}\nsources: {', '.join(r.sources) or 'n/a'}")
    return "\n\n".join(parts) or "(no analyst reports)"


def render_case(c) -> str:
    if c is None:
        return "(none)"
    pts = "\n".join(f"- {p}" for p in c.key_points)
    reb = "\n".join(f"- {p}" for p in c.rebuttals)
    out = f"conviction {c.conviction}\nkey points:\n{pts}"
    return out + (f"\nrebuttals:\n{reb}" if reb else "")


def bull_prompt(state) -> str:
    return (f"Ticker: {state.ticker} ({state.instrument.exchange}, "
            f"{state.instrument.currency}). Run kind: {state.kind}.\n\n"
            f"Analyst reports:\n{render_reports(state)}\n\n"
            "Make the strongest honest BULL case as a structured DebateCase "
            "(side='bull').")


def bear_prompt(state) -> str:
    return (f"Ticker: {state.ticker}. Run kind: {state.kind}.\n\n"
            f"Analyst reports:\n{render_reports(state)}\n\n"
            f"The bull argues:\n{render_case(state.bull)}\n\n"
            "Rebut the bull point by point, then make the BEAR case as a "
            "structured DebateCase (side='bear').")


def arbiter_prompt(state) -> str:
    sell_note = ("This is a SELL REVIEW of an existing position: direction "
                 "'sell' means exit; size is decided at approval in shares.\n"
                 if state.kind == "sell_review" else "")
    return (f"Ticker: {state.ticker} ({state.instrument.exchange}, "
            f"{state.instrument.currency}).\n{sell_note}\n"
            f"Analyst reports:\n{render_reports(state)}\n\n"
            f"BULL:\n{render_case(state.bull)}\n\n"
            f"BEAR:\n{render_case(state.bear)}\n\n"
            "Weigh the debate and emit a Thesis. Numbers must be defensible "
            f"from the record; currency is {state.instrument.currency}; pass "
            "when unconvinced. entry_low/entry_high bound a limit order; "
            "stop_loss below entry for buys, above for sells/shorts.")


SCHEMAS = {"bull": DebateCase, "bear": DebateCase, "arbiter": Thesis}
PROMPTS = {"bull": bull_prompt, "bear": bear_prompt, "arbiter": arbiter_prompt}


# ── production factories ─────────────────────────────────────────────────
def default_structured_factory(conn, env=None):
    def factory(agent_id, schema, work_item_id):
        a = registry.agent_row(conn, agent_id)
        llm = registry.build_llm(conn, a["provider_id"], a["model"],
                                 a["temperature"], a["max_tokens"],
                                 env=env or __import__("os").environ)
        meter = registry.MeterCallback(conn, agent_id, work_item_id)
        return llm.with_structured_output(schema).with_config(
            callbacks=[meter]), a["system_prompt"]
    return factory


def make_debater(conn, agent_id, narratives, *, structured_factory):
    schema, prompt_fn = SCHEMAS[agent_id], PROMPTS[agent_id]

    def node(state):
        llm, system = structured_factory(agent_id, schema, state.work_item_id)
        obj = llm.invoke([("system", system), ("user", prompt_fn(state))])
        if isinstance(obj, dict):                   # some providers hand dicts
            obj = schema.model_validate(obj)
        md = (f"# {agent_id} — {state.ticker}\n\n"
              f"{json.dumps(obj.model_dump(), indent=2, default=str)}")
        path = narratives.write(state.work_item_id, agent_id, md)
        return obj.model_copy(update={"narrative_path": path})
    return node


def make_analyst(conn, agent_id, narratives, tool_builder, *,
                 structured_factory, agent_factory=None, env=None):
    """Tool loop for research, then one structured extraction call — robust
    across create_agent API drift, and the extraction is schema-validated."""

    def node(state):
        a = registry.agent_row(conn, agent_id)
        system = a["system_prompt"].replace("{ticker}", state.ticker)
        task = (f"Research {state.ticker} ({state.instrument.exchange}) now. "
                f"Run kind: {state.kind}.")
        tools = tool_builder(json.loads(a["tools"]), state)
        transcript = _run_agent(conn, agent_id, state, system, task, tools,
                                agent_factory, env)
        llm, _ = structured_factory(agent_id, AnalystReport,
                                    state.work_item_id)
        obj = llm.invoke([
            ("system", system),
            ("user", f"{task}\n\nYour research transcript:\n{transcript}\n\n"
                     f"Now emit the structured AnalystReport "
                     f"(agent='{agent_id}', ticker='{state.ticker}', "
                     f"data_asof=now in ISO-8601 UTC).")])
        if isinstance(obj, dict):
            obj = AnalystReport.model_validate(obj)
        path = narratives.write(state.work_item_id, agent_id,
                                f"# {agent_id} — {state.ticker}\n\n{transcript}")
        return obj.model_copy(update={
            "narrative_path": path, "agent": agent_id,
            "ticker": state.ticker,
            "data_asof": obj.data_asof or datetime.now(timezone.utc)})
    return node


def _run_agent(conn, agent_id, state, system, task, tools, agent_factory, env):
    if agent_factory is not None:
        return agent_factory(agent_id, state, system, task, tools)
    from langchain.agents import create_agent
    model = registry.model_for(conn, agent_id, work_item_id=state.work_item_id,
                               env=env or __import__("os").environ)
    agent = create_agent(model=model, tools=tools, system_prompt=system)
    result = agent.invoke({"messages": [("user", task)]},
                          config={"recursion_limit": 40})
    lines = []
    for m in result.get("messages", []):
        role = getattr(m, "type", "?")
        content = m.content if isinstance(m.content, str) else json.dumps(
            m.content, default=str)[:2000]
        lines.append(f"[{role}] {content[:2000]}")
    return "\n".join(lines[-40:])
