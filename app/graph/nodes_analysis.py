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
        findings = "\n".join(f"- {f}" for f in (r.key_findings or []))
        parts.append(f"### {r.agent} — {r.signal} (conviction {r.conviction})\n"
                     f"{r.summary}"
                     + (f"\nfindings:\n{findings}" if findings else "")
                     + f"\nsources: {', '.join(r.sources) or 'n/a'}")
    return "\n\n".join(parts) or "(no analyst reports)"


def render_trigger(state) -> str:
    return f"Why this run exists: {state.trigger}\n" if state.trigger else ""


def render_case(c) -> str:
    if c is None:
        return "(none)"
    pts = "\n".join(f"- {p}" for p in c.key_points)
    reb = "\n".join(f"- {p}" for p in c.rebuttals)
    out = f"conviction {c.conviction}\nkey points:\n{pts}"
    return out + (f"\nrebuttals:\n{reb}" if reb else "")


def bull_prompt(state) -> str:
    return (f"Ticker: {state.ticker} ({state.instrument.exchange}, "
            f"{state.instrument.currency}). Run kind: {state.kind}.\n"
            f"{render_trigger(state)}\n"
            f"Analyst reports:\n{render_reports(state)}\n\n"
            "Make the strongest honest BULL case as a structured DebateCase "
            "(side='bull').")


def bear_prompt(state) -> str:
    return (f"Ticker: {state.ticker}. Run kind: {state.kind}.\n"
            f"{render_trigger(state)}\n"
            f"Analyst reports:\n{render_reports(state)}\n\n"
            f"The bull argues:\n{render_case(state.bull)}\n\n"
            "Rebut the bull point by point, then make the BEAR case as a "
            "structured DebateCase (side='bear').")


def arbiter_prompt(state) -> str:
    if state.kind == "sell_review":
        # An exit review is a narrower question than a purchase, and it is
        # asked while the position is moving — so it gets the entry analysis
        # rather than a fresh debate, and is told plainly that holding is a
        # legitimate answer. Without that it reads a breach as an instruction.
        return (f"Ticker: {state.ticker} ({state.instrument.exchange}, "
                f"{state.instrument.currency}). EXIT REVIEW of a position we "
                f"already hold.\n{render_trigger(state)}\n"
                f"What we believed when we opened it:\n"
                f"{state.prior_evidence or '(no earlier analysis on record)'}"
                f"\n\nWhat has happened since:\n{render_reports(state)}\n\n"
                "Decide whether the reason we own this is still intact.\n"
                "- direction 'sell' to exit; size is chosen at approval, in "
                "shares, and stop_loss sits ABOVE entry for an exit.\n"
                "- direction 'pass' to HOLD — say so when the fall is noise, "
                "sector-wide, or already priced in. A breached stop is a "
                "prompt to look, not an instruction to sell.\n"
                "Numbers must be defensible from the record; currency is "
                f"{state.instrument.currency}. Put the single reason this is "
                "still owned, or no longer is, in the summary.")
    return (f"Ticker: {state.ticker} ({state.instrument.exchange}, "
            f"{state.instrument.currency}).\n"
            f"{render_trigger(state)}\n"
            f"Analyst reports:\n{render_reports(state)}\n\n"
            f"BULL:\n{render_case(state.bull)}\n\n"
            f"BEAR:\n{render_case(state.bear)}\n\n"
            "Weigh the debate and emit a Thesis. Numbers must be defensible "
            f"from the record; currency is {state.instrument.currency}; pass "
            "when unconvinced. entry_low/entry_high bound a limit order; "
            "stop_loss below entry for buys, above for sells/shorts.")


SCHEMAS = {"bull": DebateCase, "bear": DebateCase, "arbiter": Thesis}
PROMPTS = {"bull": bull_prompt, "bear": bear_prompt, "arbiter": arbiter_prompt}


def report_md(agent_id: str, ticker: str, obj) -> str:
    """The agent's CONCLUSION as markdown — this is the artifact downstream
    agents actually consume (they never see the working notes), so it is
    written as its own file rather than buried in a transcript."""
    d = obj.model_dump()
    lines = [f"# {agent_id} — {ticker}", ""]
    if d.get("signal"):
        lines.append(f"**Signal: {d['signal'].upper()}** · conviction "
                     f"{d.get('conviction')}")
    if d.get("side"):
        lines.append(f"**{d['side'].title()} case** · conviction "
                     f"{d.get('conviction')}")
    if d.get("direction"):
        lines.append(f"**Verdict: {str(d['direction']).upper()}** · "
                     f"conviction {d.get('conviction')}")
        levels = [(k.replace("_", " "), d.get(k)) for k in
                  ("entry_low", "entry_high", "stop_loss", "take_profit")]
        levels = [f"{k} {v}" for k, v in levels if v is not None]
        if levels:
            lines.append("· ".join(levels) + f" {d.get('currency') or ''}")
    if d.get("summary"):
        lines += ["", d["summary"]]
    for key, title in (("key_findings", "Key findings"),
                       ("key_points", "Key points"),
                       ("rebuttals", "Rebuttals"),
                       ("conditions", "Conditions & caveats"),
                       ("sources", "Sources")):
        items = d.get(key) or []
        if items:
            lines += ["", f"**{title}**"] + [f"- {i}" for i in items]
    if d.get("data_asof"):
        lines += ["", f"_data as of {d['data_asof']}_"]
    return "\n".join(lines)


# ── production factories ─────────────────────────────────────────────────
def default_structured_factory(conn, env=None):
    """Structured calls get the SAME fallback protection as tool loops — an
    overloaded primary model must not fail a run (§8). This was the gap that
    let a 529 on opus kill an otherwise-complete pipeline."""
    def factory(agent_id, schema, work_item_id):
        a = registry.agent_row(conn, agent_id)
        environ = env or __import__("os").environ

        def structured(provider_id, model):
            return registry.build_llm(conn, provider_id, model,
                                      a["temperature"], a["max_tokens"],
                                      env=environ).with_structured_output(schema)

        llm = structured(a["provider_id"], a["model"])
        if a["fallback_provider_id"] and a["fallback_model"]:
            llm = llm.with_fallbacks([structured(a["fallback_provider_id"],
                                                 a["fallback_model"])])
        meter = registry.MeterCallback(conn, agent_id, work_item_id)
        return llm.with_config(callbacks=[meter]), a["system_prompt"]
    return factory


def record_report(conn, work_item_id, agent_id, kind, obj):
    """desk.db is the durable record; checkpoints.db is disposable (§2).
    Without this the analyst reports and debate cases lived only in the
    checkpoint and in prose."""
    import time
    try:
        conn.execute(
            "INSERT INTO agent_reports (work_item_id, agent_id, kind,"
            " payload, created_at) VALUES (?,?,?,?,?)"
            " ON CONFLICT(work_item_id, agent_id) DO UPDATE SET"
            " payload=excluded.payload, created_at=excluded.created_at",
            (work_item_id, agent_id, kind, obj.model_dump_json(),
             int(time.time())))
    except Exception:                           # noqa: BLE001 — never fail a
        pass                                    # run over bookkeeping


def make_debater(conn, agent_id, narratives, *, structured_factory):
    schema, prompt_fn = SCHEMAS[agent_id], PROMPTS[agent_id]

    def node(state):
        llm, system = structured_factory(agent_id, schema, state.work_item_id)
        obj = llm.invoke([("system", system), ("user", prompt_fn(state))])
        if isinstance(obj, dict):                   # some providers hand dicts
            obj = schema.model_validate(obj)
        path = narratives.write(state.work_item_id, f"{agent_id}.report",
                                report_md(agent_id, state.ticker, obj))
        obj = obj.model_copy(update={"narrative_path": path})
        record_report(conn, state.work_item_id, agent_id,
                      "thesis" if agent_id == "arbiter" else "debate", obj)
        return obj
    return node


def make_analyst(conn, agent_id, narratives, tool_builder, *,
                 structured_factory, agent_factory=None, env=None):
    """Tool loop for research, then one structured extraction call — robust
    across create_agent API drift, and the extraction is schema-validated."""

    def node(state):
        a = registry.agent_row(conn, agent_id)
        system = a["system_prompt"].replace("{ticker}", state.ticker)
        task = (f"Research {state.ticker} ({state.instrument.exchange}) now. "
                f"Run kind: {state.kind}."
                + (f"\n{render_trigger(state)}" if state.trigger else ""))
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
                     f"data_asof=now in ISO-8601 UTC). `summary` must be ONE "
                     f"plain sentence under 300 characters — no markdown, no "
                     f"tables; your full reasoning belongs in the narrative. "
                     f"`conviction` is strength 0..1; the signal carries "
                     f"direction. `key_findings` MUST list 4-8 concrete facts "
                     f"WITH NUMBERS and dates that a colleague could act on "
                     f"(valuations, growth rates, levels, catalysts) — this "
                     f"is what the debate and the arbiter actually receive, "
                     f"so anything you leave out is lost.")])
        if isinstance(obj, dict):
            obj = AnalystReport.model_validate(obj)
        # two artifacts, deliberately separate: the REPORT is the conclusion
        # (and the only thing downstream agents receive), the TRANSCRIPT is
        # the working that produced it — kept for audit, never propagated.
        obj = obj.model_copy(update={"agent": agent_id,
                                     "ticker": state.ticker})
        path = narratives.write(state.work_item_id, f"{agent_id}.report",
                                report_md(agent_id, state.ticker, obj))
        narratives.write(
            state.work_item_id, f"{agent_id}.transcript",
            f"# {agent_id} — working notes for {state.ticker}\n\n"
            f"_How the conclusion was reached. Downstream agents never see "
            f"this; they receive the report only._\n\n{transcript}")
        obj = obj.model_copy(update={
            "narrative_path": path, "agent": agent_id,
            "ticker": state.ticker,
            "data_asof": obj.data_asof or datetime.now(timezone.utc)})
        record_report(conn, state.work_item_id, agent_id, "analyst", obj)
        return obj
    return node


# LangGraph counts supersteps, so a tool round-trip is worth about two. The
# analysts that finish cleanly use 4-11 model calls, so 30 leaves real
# headroom while capping the runaway case — and every step costs a re-send
# of the whole transcript, so the ceiling is a cost control, not just a
# safety net.
RECURSION_LIMIT = 30
STEP_BUDGET = 8                       # what we tell the agent it has


def _run_agent(conn, agent_id, state, system, task, tools, agent_factory, env):
    """Run the research loop, STREAMING so that a step-budget overrun still
    leaves us the work done so far. Hitting the limit used to raise and throw
    the whole analyst away (the SBUX fundamental run cost 40 steps and
    produced nothing) — now it degrades to a partial report."""
    # Say the budget out loud, before anything dispatches. An agent that knows
    # it has roughly eight calls spends them on distinct sources instead of
    # re-querying one that came back empty, and finishes on evidence rather
    # than on the ceiling.
    task = (f"{task}\n\nYou have roughly {STEP_BUDGET} tool calls for this. "
            f"Spend them on DIFFERENT sources rather than retrying one that "
            f"failed or returned nothing, and stop as soon as you can support "
            f"a view. An honest report that names its gaps beats an exhausted "
            f"search.")
    if agent_factory is not None:
        return agent_factory(agent_id, state, system, task, tools)
    from langchain.agents import create_agent
    model = registry.model_for(conn, agent_id, work_item_id=state.work_item_id,
                               env=env or __import__("os").environ)
    meter = registry.MeterCallback(conn, agent_id, state.work_item_id)
    agent = create_agent(model=model, tools=tools, system_prompt=system)
    messages, truncated = [], None
    try:
        # callbacks on the INVOKE config, not the model — with_config on the
        # model does not survive create_agent, which is why tool-loop calls
        # were never metered and run costs read far too low
        for update in agent.stream(
                {"messages": [("user", task)]},
                config={"recursion_limit": RECURSION_LIMIT,
                        "callbacks": [meter]},
                stream_mode="updates"):
            for _node, data in (update or {}).items():
                messages.extend((data or {}).get("messages", []) or [])
    except Exception as e:                      # noqa: BLE001
        truncated = str(e)[:200]
    lines = []
    for m in messages:
        role = getattr(m, "type", "?")
        content = m.content if isinstance(m.content, str) else json.dumps(
            m.content, default=str)[:2000]
        lines.append(f"[{role}] {content[:2000]}")
    if truncated:
        lines.append(f"[system] RESEARCH CUT SHORT: {truncated}. Report on "
                     f"what was gathered above and say plainly what is "
                     f"missing.")
    return "\n".join(lines[-60:])
