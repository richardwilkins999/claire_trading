"""Claude Agent SDK provider (subscription billing) — offline, scripted SDK
conversations injected via `_query`. The real SDK is blocked in conftest."""
import json

import claude_agent_sdk as sdk
import pytest
from langchain_core.tools import tool

from app.accounting import db
from app.graph.state import DebateCase
from app.providers import claude_sdk, registry, seeds


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init(c)
    seeds.seed(c, ts=1_754_900_000)
    return c


def result(**kw):
    base = dict(subtype="success", duration_ms=1, duration_api_ms=1,
                is_error=False, num_turns=1, session_id="s",
                usage={"input_tokens": 10, "cache_creation_input_tokens": 100,
                       "cache_read_input_tokens": 5, "output_tokens": 7},
                total_cost_usd=0.42)
    base.update(kw)
    return sdk.ResultMessage(**base)


def scripted(*messages, seen=None):
    async def q(*, prompt, options):
        if seen is not None:
            seen.append((prompt, options))
        for m in messages:
            yield m
    return q


def test_default_roster_runs_on_the_subscription(conn):
    for a in conn.execute("SELECT provider_id, fallback_provider_id FROM agents"):
        assert a["provider_id"] == "claude_max"
        assert a["fallback_provider_id"] in (None, "claude_max")
    p = registry.provider_row(conn, "claude_max")
    assert p["kind"] == "claude_sdk" and p["api_key_ref"] is None


def test_build_needs_no_api_key(conn):
    llm = registry.build_llm(conn, "claude_max", "claude-sonnet-5", env={})
    assert claude_sdk.is_sdk(llm)


def test_options_isolate_and_strip_the_api_key():
    o = claude_sdk.base_options(model="claude-sonnet-5",
                                fallback_model="claude-haiku-4-5")
    # the CLI would bill the API if it inherited the key from claire.env
    assert o.env["ANTHROPIC_API_KEY"] == ""
    assert o.tools == []                     # no Bash / file tools
    assert o.setting_sources == []           # none of Richard's settings
    assert o.strict_mcp_config and o.mcp_servers == {}
    assert o.fallback_model == "claude-haiku-4-5"


def test_structured_invoke_validates_and_meters(conn):
    case = {"side": "bull", "conviction": 0.7, "key_points": ["a", "b"],
            "rebuttals": []}
    seen = []
    base = registry.build_llm(conn, "claude_max", "claude-sonnet-5")
    base._query = scripted(result(structured_output=case), seen=seen)
    llm = base.with_structured_output(DebateCase).with_config(
        callbacks=[registry.MeterCallback(conn, "bull", "wi_1")])
    obj = llm.invoke([("system", "SYS"), ("user", "make the case")])
    assert isinstance(obj, DebateCase) and obj.conviction == 0.7
    prompt, opts = seen[0]
    assert prompt == "make the case" and opts.system_prompt == "SYS"
    assert opts.output_format["type"] == "json_schema"
    row = conn.execute("SELECT * FROM agent_runs").fetchone()
    assert (row["agent_id"], row["work_item_id"], row["provider_id"]) == \
        ("bull", "wi_1", "claude_max")
    assert row["status"] == "ok"
    assert row["tokens_in"] == 115 and row["tokens_out"] == 7
    assert row["cost_usd"] == 0              # subscription: nothing billed


def test_missing_structured_output_is_an_error(conn):
    llm = claude_sdk.SdkChat("m", schema=DebateCase,
                             _query=scripted(result(structured_output=None)))
    with pytest.raises(claude_sdk.SdkError):
        llm.invoke("x")


def test_fallback_chain_stays_on_the_subscription(conn):
    sdk_a = claude_sdk.SdkChat("claude-opus-5")
    sdk_b = claude_sdk.SdkChat("claude-sonnet-5")
    assert registry.with_fallback(sdk_a, sdk_b).fallback_model == \
        "claude-sonnet-5"

    class NotSdk:
        def with_fallbacks(self, fbs):
            raise AssertionError("must not chain across billing modes")
    # never silently fall from the subscription onto the metered API
    assert registry.with_fallback(sdk_a, NotSdk()) is sdk_a


def test_tool_loop_runs_desk_tools_and_builds_transcript(conn):
    calls = []

    @tool
    def market_quote(symbol: str) -> str:
        """Live quote."""
        calls.append(symbol)
        return json.dumps({"symbol": symbol, "price": 101.5})

    [sdk_tool] = claude_sdk.lc_to_sdk_tools([market_quote])
    assert sdk_tool.name == "market_quote"

    async def q(*, prompt, options):
        assert options.allowed_tools == ["mcp__desk__market_quote"]
        out = await sdk_tool.handler({"symbol": "AMD"})
        yield sdk.AssistantMessage(content=[
            sdk.ToolUseBlock(id="t1", name="mcp__desk__market_quote",
                             input={"symbol": "AMD"})], model="m")
        yield sdk.UserMessage(content=[sdk.ToolResultBlock(
            tool_use_id="t1", content=out["content"])])
        yield sdk.AssistantMessage(content=[sdk.TextBlock(text="AMD at 101.5")],
                                   model="m")
        yield result()

    chat = registry.model_for(conn, "news", work_item_id="wi_2")
    chat._query = q
    lines, truncated = claude_sdk.run_tool_loop(chat, "SYS", "research", [
        market_quote])
    assert calls == ["AMD"] and truncated is None
    text = "\n".join(lines)
    assert "tool call market_quote" in text and "101.5" in text
    assert "AMD at 101.5" in text
    row = conn.execute("SELECT * FROM agent_runs").fetchone()
    assert row["agent_id"] == "news" and row["status"] == "ok"


def test_tool_loop_out_of_turns_degrades_to_partial(conn):
    chat = claude_sdk.SdkChat("m", _query=scripted(
        sdk.AssistantMessage(content=[sdk.TextBlock(text="half done")],
                             model="m"),
        result(subtype="error_max_turns", is_error=True)))
    lines, truncated = claude_sdk.run_tool_loop(chat, "S", "T", [])
    assert "half done" in "\n".join(lines)
    assert "error_max_turns" in truncated


def test_tool_errors_go_back_to_the_model():
    @tool
    def fetch_page(url: str) -> str:
        """Fetch."""
        raise RuntimeError("403 forbidden")
    [t] = claude_sdk.lc_to_sdk_tools([fetch_page])
    import asyncio
    out = asyncio.run(t.handler({"url": "http://x"}))
    assert out["is_error"] and "403" in out["content"][0]["text"]


def test_history_is_rendered_into_one_prompt():
    system, prompt = claude_sdk.split_messages([
        ("system", "S"), ("user", "hi"), ("assistant", "hello"),
        ("user", "what is held?")])
    assert system == "S"
    assert "Richard: hi" in prompt and "You: hello" in prompt
    assert prompt.endswith("what is held?")


def test_claire_chat_streams_tools_and_text(conn):
    from app.graph.claire_agent import Claire

    seen = []

    async def q(*, prompt, options):
        seen.append(prompt)
        yield sdk.AssistantMessage(content=[sdk.ToolUseBlock(
            id="t", name="mcp__desk__portfolio_summary", input={})], model="m")
        yield sdk.AssistantMessage(content=[sdk.TextBlock(text="All cash.")],
                                   model="m")
        yield result(result="All cash.")

    claire = Claire(conn, repo=None, desk=None, market=None)
    claire._ensure_agent()
    chat, system, tools = claire._sdk_chat
    claire._sdk_chat = (claude_sdk.SdkChat(chat.model, _query=q,
                                           meter=chat.meter), system, tools)
    out = [json.loads(x) for x in claire.ask_stream("what do I hold?", "t1")]
    assert out[0] == {"kind": "tool", "name": "portfolio_summary", "args": {}}
    assert {"kind": "text", "text": "All cash."} in out
    assert out[-1]["kind"] == "done"
    saved = json.loads(conn.execute("SELECT messages FROM chat_threads"
                                    " WHERE thread='t1'").fetchone()[0])
    assert saved[-1] == {"role": "assistant", "content": "All cash."}


def test_old_databases_accept_the_sdk_kind(tmp_path):
    """A desk.db created before claude_sdk existed has a CHECK that rejects
    it; init() must widen it without losing provider rows."""
    import sqlite3
    path = tmp_path / "old.db"
    old = db.SCHEMA.read_text().replace(
        "'anthropic','claude_sdk','openai_compatible'",
        "'anthropic','openai_compatible'")
    raw = sqlite3.connect(path)
    raw.executescript(old)
    raw.execute("INSERT INTO providers (id, display_name, kind, capabilities)"
                " VALUES ('anthropic','A','anthropic','{}')")
    raw.commit()
    raw.close()
    c = db.connect(path)
    db.init(c)
    seeds.seed(c, ts=1)
    kinds = {r["id"]: r["kind"] for r in c.execute("SELECT * FROM providers")}
    assert kinds["anthropic"] == "anthropic"
    assert kinds["claude_max"] == "claude_sdk"


def _refusing_claire(conn, monkeypatch, refuse):
    """Claire whose stream_chat refuses for the models in `refuse` (by
    name), and for any call carrying the old history when 'history' is in
    `refuse`. Returns (claire, calls)."""
    from app.graph.claire_agent import Claire
    calls = []

    def fake_stream(chat, system, prompt, tools, **kw):
        calls.append((chat.model, prompt))
        if chat.model in refuse or ("history" in refuse and "old" in prompt):
            raise RuntimeError("API Error: Opus 5's safeguards flagged this "
                               "message")
        yield ("text", "Desk is fine.")
        yield ("done", "Desk is fine.")
    monkeypatch.setattr(claude_sdk, "stream_chat", fake_stream)
    claire = Claire(conn, repo=None, desk=None, market=None)
    claire._ensure_agent()
    _, system, tools = claire._sdk_chat
    claire._sdk_chat = (claude_sdk.SdkChat("claude-opus-5",
                                           fallback_model="claude-sonnet-5"),
                        system, tools)
    claire._histories["t"] = [{"role": "user", "content": "old question"},
                              {"role": "assistant", "content": "old answer"}]
    return claire, calls


def test_a_refused_chat_retries_on_the_backup_model(conn, monkeypatch):
    claire, calls = _refusing_claire(conn, monkeypatch, {"claude-opus-5"})
    out = [json.loads(x) for x in claire.ask_stream("status?", "t")]
    assert [c[0] for c in calls] == ["claude-opus-5", "claude-sonnet-5"]
    assert {"kind": "text", "text": "Desk is fine."} in out
    assert not any(o["kind"] == "error" for o in out)
    assert len(claire._histories["t"]) == 4         # history kept


def test_a_thread_that_keeps_being_refused_starts_fresh(conn, monkeypatch):
    claire, calls = _refusing_claire(conn, monkeypatch, {"history"})
    out = [json.loads(x) for x in claire.ask_stream("status?", "t")]
    assert len(calls) == 3 and "old" not in calls[-1][1]
    assert any("fresh conversation" in (o.get("text") or "") for o in out)
    assert claire._histories["t"] == [
        {"role": "user", "content": "status?"},
        {"role": "assistant", "content": "Desk is fine."}]


def test_other_errors_are_not_retried(conn, monkeypatch):
    from app.graph.claire_agent import Claire
    calls = []

    def boom(chat, system, prompt, tools, **kw):
        calls.append(chat.model)
        raise RuntimeError("network down")
        yield  # noqa — generator
    monkeypatch.setattr(claude_sdk, "stream_chat", boom)
    claire = Claire(conn, repo=None, desk=None, market=None)
    claire._ensure_agent()
    _, system, tools = claire._sdk_chat
    claire._sdk_chat = (claude_sdk.SdkChat("a", fallback_model="b"),
                        system, tools)
    out = [json.loads(x) for x in claire.ask_stream("hi", "t2")]
    assert calls == ["a"] and out[0]["kind"] == "error"
