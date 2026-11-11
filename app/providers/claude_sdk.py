"""Claude Agent SDK provider — model calls billed to the Claude Max
subscription instead of the metered API.

The SDK drives the bundled `claude` CLI, which signs in with the machine's
claude.ai login (~/.claude). That only happens when no ANTHROPIC_API_KEY is
visible to the CLI, so every call explicitly blanks it: claire.env still
carries the key for the `anthropic` provider, and inheriting it would silently
move these calls back onto the API bill.

LangGraph keeps the orchestration (fan-out, debate, the interrupt() approval
gate); this module only replaces the LLM calls inside the nodes. `SdkChat`
duck-types the slice of the LangChain chat-model interface the desk uses
(invoke / stream / with_structured_output / with_fallbacks / with_config), so
the call sites are unchanged. Tool loops use the SDK's own agent loop, with
the desk's LangChain tools re-exposed as an in-process MCP server.

Isolation matters: every session starts with NO built-in tools (no Bash, no
file access), no user/project settings, no plugins and no MCP servers except
the desk's own in-process one. Agents see exactly the tools the `agents` table
grants them and nothing from Richard's personal Claude Code setup.
"""
import asyncio
import contextlib
import json
import logging
import os
import queue
import threading
import time
import uuid
from pathlib import Path

from ..logsetup import swallowed

log = logging.getLogger(__name__)

KIND = "claude_sdk"
SERVER = "desk"                         # tools surface as mcp__desk__<name>
TOOL_PREFIX = f"mcp__{SERVER}__"
STRUCTURED_MAX_TURNS = 6                # structured output needs ~2-3 turns
# Same ceiling the LangGraph loop had: RECURSION_LIMIT 30 supersteps is about
# fifteen model calls once each tool round-trip is counted as two.
TOOL_LOOP_MAX_TURNS = 15

# Where the CLI keeps its session transcripts (~/.claude/projects/<this dir>).
# A dedicated directory keeps them out of every other project's history.
WORK_DIR = Path(__file__).resolve().parents[2] / "var" / "sdk"


class SdkError(Exception):
    pass


class SdkTimeout(SdkError):
    """A conversation ran past its wall-clock limit and was stopped."""


def _sdk():
    import claude_agent_sdk
    return claude_agent_sdk


def subprocess_env():
    """Blank the API key so the CLI falls back to the claude.ai (Max) login."""
    return {"ANTHROPIC_API_KEY": "", "ANTHROPIC_AUTH_TOKEN": "",
            "CLAUDE_CODE_ENTRYPOINT": "claire-desk"}


def base_options(*, model, fallback_model=None, system_prompt="",
                 max_turns=STRUCTURED_MAX_TURNS, mcp_tools=None,
                 output_format=None, resume=None):
    """Options for one conversation. `resume` is the id of an earlier
    session in WORK_DIR to continue: the CLI reloads that session's messages
    from its own transcript, so only the new prompt is sent."""
    sdk = _sdk()
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    servers, allowed = {}, []
    if mcp_tools:
        servers[SERVER] = sdk.create_sdk_mcp_server(SERVER, tools=mcp_tools)
        allowed = [TOOL_PREFIX + t.name for t in mcp_tools]
    kw = dict(model=model, system_prompt=system_prompt, tools=[],
              allowed_tools=allowed, mcp_servers=servers,
              strict_mcp_config=True, setting_sources=[],
              permission_mode="dontAsk", max_turns=max_turns,
              env=subprocess_env(), cwd=str(WORK_DIR))
    if fallback_model and fallback_model != model:
        kw["fallback_model"] = fallback_model
    if output_format:
        kw["output_format"] = output_format
    if resume:
        kw["resume"] = resume
    return sdk.ClaudeAgentOptions(**kw)


def run_sync(coro_fn):
    """Run an async SDK conversation from the desk's synchronous nodes. Nodes
    run on worker threads with no loop; if one ever is running (an async
    caller), use a private thread rather than fail."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro_fn())
    box = {}

    def target():
        try:
            box["v"] = asyncio.run(coro_fn())
        except BaseException as e:      # noqa: BLE001 — re-raised below
            box["e"] = e
    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join()
    if "e" in box:
        raise box["e"]
    return box["v"]


# ── limits: how long one conversation may run, and how many at once ──────
# Every conversation is a `claude` subprocess. Nothing used to bound a hung
# one, and N concurrent pipeline runs meant about 3N of them
# (REVIEW-2026-11-11 §4.1).
TIMEOUT_ENV = "CLAIRE_SDK_TIMEOUT_S"
MAX_CONCURRENT_ENV = "CLAIRE_SDK_MAX_CONCURRENT"
# seconds of wall clock per conversation, by kind
DEFAULT_TIMEOUT_S = {"tool_loop": 600, "chat": 300, "structured": 300}
_KIND_NAMES = {"tool_loop": "tool loop", "chat": "chat turn",
               "structured": "call"}
DEFAULT_MAX_CONCURRENT = 4

_sleep = asyncio.sleep          # the deadline's clock; tests replace it


def timeout_for(kind, env=None):
    """Seconds a conversation of this kind may run. CLAIRE_SDK_TIMEOUT_S is
    the limit for a research tool loop (default 600) and a ceiling for the
    shorter kinds: a chat turn or a structured call gets its own default
    (300) or the configured value, whichever is lower."""
    env = os.environ if env is None else env
    default = DEFAULT_TIMEOUT_S[kind]
    raw = str(env.get(TIMEOUT_ENV) or "").strip()
    if not raw:
        return default
    try:
        ceiling = float(raw)
    except ValueError:
        ceiling = 0
    if not 0 < ceiling < float("inf"):
        swallowed(log, f"{TIMEOUT_ENV} is not a positive number of seconds"
                       " — using the defaults")
        return default
    return ceiling if kind == "tool_loop" else min(default, ceiling)


class Gate:
    """At most `limit` SDK conversations at a time in this process. A caller
    past the limit WAITS for a slot — it is never refused. It blocks the
    calling thread, so it holds across the separate event loops each
    conversation runs on. `urgent` callers (chat: a person is waiting) take
    the next free slot ahead of queued background work."""

    def __init__(self, limit=DEFAULT_MAX_CONCURRENT):
        self.limit = max(1, int(limit))
        self.active = 0                 # conversations holding a slot
        self.waiting = 0                # callers queued for one
        self._urgent = 0
        self._cond = threading.Condition()

    def acquire(self, *, urgent=False):
        with self._cond:
            self.waiting += 1
            self._urgent += bool(urgent)
            try:
                if self.active >= self.limit:
                    log.info("SDK conversation queued: %d running (limit %d),"
                             " %d waiting", self.active, self.limit,
                             self.waiting)
                while self.active >= self.limit or (self._urgent
                                                    and not urgent):
                    self._cond.wait()
                self.active += 1
            finally:
                self.waiting -= 1
                if urgent:
                    self._urgent -= 1
                    self._cond.notify_all()     # background may look again

    def release(self):
        with self._cond:
            self.active -= 1
            self._cond.notify_all()

    @contextlib.contextmanager
    def slot(self, *, urgent=False):
        self.acquire(urgent=urgent)
        try:
            yield self
        finally:
            self.release()


_gate = None                    # process-wide; built on first use
_gate_lock = threading.Lock()


def gate():
    global _gate
    with _gate_lock:
        if _gate is None:
            raw = str(os.environ.get(MAX_CONCURRENT_ENV) or "").strip()
            try:
                limit = int(raw) if raw else DEFAULT_MAX_CONCURRENT
            except ValueError:
                limit = 0
            if limit < 1:
                swallowed(log, f"{MAX_CONCURRENT_ENV} is not a positive whole"
                               " number — using the default")
                limit = DEFAULT_MAX_CONCURRENT
            _gate = Gate(limit)
        return _gate


async def _conversation(q, prompt, opts, on_message, *, timeout_s, kind):
    """One SDK conversation, start to finish, under a wall-clock deadline.
    Past the deadline it is cancelled and SdkTimeout raised.

    The deadline is an anyio cancel scope, not asyncio.wait_for: the SDK
    shields its own shutdown (stdin EOF, then SIGTERM, then SIGKILL) against
    anyio cancellation only, so this is what makes sure the `claude`
    subprocess is gone before the error is raised."""
    import anyio
    agen = q(prompt=prompt, options=opts)
    with anyio.CancelScope() as scope:
        async def deadline():
            await _sleep(timeout_s)
            scope.cancel()
        dog = asyncio.ensure_future(deadline())
        try:
            async for m in agen:
                on_message(m)
        finally:
            dog.cancel()
            close = getattr(agen, "aclose", None)
            if close is not None:       # stops the subprocess on every exit
                with anyio.CancelScope(shield=True):
                    await close()
    if scope.cancelled_caught:
        log.warning("SDK %s conversation stopped after %gs (model %s)",
                    kind, timeout_s, getattr(opts, "model", "?"))
        raise SdkTimeout(
            f"Claude SDK {_KIND_NAMES.get(kind, kind)} timed out after "
            f"{timeout_s:g}s and was stopped ({TIMEOUT_ENV} sets the limit)")


def _drive(q, prompt, opts, on_message, *, kind, urgent=False,
           timeout_s=None, meter=None):
    """Blocking: wait for a free slot, then run one conversation under its
    deadline. Time spent queued counts against neither the deadline nor the
    metered run, which starts only once the slot is held."""
    limit = timeout_for(kind) if timeout_s is None else timeout_s
    with gate().slot(urgent=urgent):
        if meter:
            meter.start()
        run_sync(lambda: _conversation(q, prompt, opts, on_message,
                                       timeout_s=limit, kind=kind))


# ── metering ─────────────────────────────────────────────────────────────
class Meter:
    """Writes the agent_runs row for one SDK conversation. Tokens are recorded
    as usual; cost_usd is what was actually charged, which on the
    subscription is zero — the CLI's API-equivalent estimate is deliberately
    NOT stored, so desk spend totals stay true."""

    def __init__(self, conn, agent_id, work_item_id, provider_id, model):
        self.conn, self.agent_id, self.work_item_id = conn, agent_id, work_item_id
        self.provider_id, self.model = provider_id, model
        self.run_id = None

    def fork(self):
        """A fresh meter per conversation: one SdkChat can be shared (Claire's
        chat agent is cached), and each call needs its own agent_runs row."""
        return Meter(self.conn, self.agent_id, self.work_item_id,
                     self.provider_id, self.model)

    def start(self):
        self.run_id = f"run_{uuid.uuid4().hex[:12]}"
        self.conn.execute(
            "INSERT INTO agent_runs (id, agent_id, work_item_id, provider_id,"
            " model, started_at, status) VALUES (?,?,?,?,?,?,?)",
            (self.run_id, self.agent_id, self.work_item_id, self.provider_id,
             self.model, int(time.time()), "running"))

    def finish(self, result):
        u = (getattr(result, "usage", None) or {}) if result else {}
        fresh = int(u.get("input_tokens") or 0)
        cwrite = int(u.get("cache_creation_input_tokens") or 0)
        cread = int(u.get("cache_read_input_tokens") or 0)
        ok = result is not None and not result.is_error
        self.conn.execute(
            "UPDATE agent_runs SET ended_at=?, tokens_in=?, tokens_out=?,"
            " cache_write_tokens=?, cache_read_tokens=?, cost_usd=0,"
            " status=?, error=? WHERE id=?",
            (int(time.time()), fresh + cwrite + cread,
             int(u.get("output_tokens") or 0), cwrite, cread,
             "ok" if ok else "error",
             None if ok else _result_error(result), self.run_id))

    def fail(self, err):
        self.conn.execute(
            "UPDATE agent_runs SET ended_at=?, status='error', error=?"
            " WHERE id=?", (int(time.time()), str(err)[:500], self.run_id))


def _result_error(result):
    if result is None:
        return "no result from Claude Agent SDK"
    bits = [result.subtype or "error"]
    if result.errors:
        bits.append(json.dumps(result.errors, default=str))
    if result.result:
        bits.append(str(result.result))
    return " · ".join(bits)[:500]


def _meter_from(callbacks, provider_id, model):
    """Adopt the desk's MeterCallback (built by registry.model_for and the
    structured factory) as an SDK meter — same agent/work-item attribution."""
    for cb in callbacks or []:
        if hasattr(cb, "agent_id") and hasattr(cb, "conn"):
            return Meter(cb.conn, cb.agent_id, getattr(cb, "work_item_id", None),
                         provider_id, model)
    return None


# ── message helpers ──────────────────────────────────────────────────────
def split_messages(messages):
    """LangChain-style input → (system prompt, user prompt)."""
    if isinstance(messages, str):
        return "", messages
    system, turns = [], []
    for m in messages:
        if isinstance(m, tuple):
            role, content = m
        elif isinstance(m, dict):
            role, content = m.get("role"), m.get("content")
        else:
            role, content = getattr(m, "type", "user"), getattr(m, "content", "")
        content = content if isinstance(content, str) else json.dumps(
            content, default=str)
        if role == "system":
            system.append(content)
        else:
            turns.append((role, content))
    if len(turns) == 1:
        return "\n\n".join(system), turns[0][1]
    return "\n\n".join(system), render_history(turns[:-1], turns[-1][1])


def render_history(turns, latest):
    """Earlier turns as context, the newest as the request — for a caller
    that hands invoke() a multi-turn message list. Claire's chat no longer
    comes through here: a chat thread is a resumed SDK session (B8), and
    recap_prompt covers the case where that session is gone."""
    lines = ["Conversation so far:"]
    for role, content in turns:
        who = "Richard" if role in ("user", "human") else "You"
        lines.append(f"{who}: {content}")
    return "\n".join(lines) + f"\n\nRichard's new message:\n{latest}"


RECAP_TURNS = 6                 # messages, i.e. the last three exchanges
RECAP_CHARS = 400               # of each


def _clip(text, limit):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def recap_prompt(turns, latest, *, max_turns=RECAP_TURNS,
                 max_chars=RECAP_CHARS):
    """The prompt that starts a NEW session for a chat thread whose earlier
    session cannot be resumed (service restart, expired transcript, CLI
    error): short notes on the last few exchanges, then the new message.
    Deliberately brief and not a transcript — replaying the whole history as
    one "Richard: … You: …" prompt is what B8 suspects of tripping the
    safety filter, and it cost 20-30k tokens a turn."""
    recent = [(r, c) for r, c in turns if str(c or "").strip()][-max_turns:]
    if not recent:
        return latest
    lines = ["Context: this continues an earlier chat with Richard. Notes on "
             "the last few exchanges, oldest first (clipped):"]
    for role, content in recent:
        who = "Richard asked" if role in ("user", "human") else "You answered"
        lines.append(f"- {who}: {_clip(content, max_chars)}")
    return "\n".join(lines) + f"\n\nRichard's new message:\n{latest}"


class _Msg:
    """Just enough of an AIMessage for the desk's callers."""

    def __init__(self, content):
        self.content = content
        self.type = "ai"


# ── the chat-model stand-in ──────────────────────────────────────────────
class SdkChat:
    def __init__(self, model, *, provider_id="claude_max", fallback_model=None,
                 schema=None, meter=None, _query=None):
        self.model, self.provider_id = model, provider_id
        self.fallback_model, self.schema, self.meter = fallback_model, schema, meter
        self._query = _query

    def _copy(self, **kw):
        d = dict(model=self.model, provider_id=self.provider_id,
                 fallback_model=self.fallback_model, schema=self.schema,
                 meter=self.meter, _query=self._query)
        d.update(kw)
        return SdkChat(d.pop("model"), **d)

    # LangChain-compatible combinators
    def with_structured_output(self, schema, **_):
        return self._copy(schema=schema)

    def with_fallbacks(self, fallbacks, **_):
        fb = next((f for f in fallbacks if isinstance(f, SdkChat)), None)
        if fb is None:
            raise SdkError("Claude SDK agents can only fall back to another "
                           "Claude SDK model")
        return self._copy(fallback_model=fb.model)

    def with_config(self, config=None, callbacks=None, **_):
        cbs = callbacks or (config or {}).get("callbacks")
        meter = _meter_from(cbs, self.provider_id, self.model)
        return self._copy(meter=meter or self.meter)

    def invoke(self, messages, config=None, *, resume=None, timeout_s=None,
               **_):
        """`resume` makes this call one more turn of an earlier session (an
        analyst's research loop, say) instead of a conversation of its own."""
        system, prompt = split_messages(messages)
        fmt = None
        if self.schema is not None:
            fmt = {"type": "json_schema",
                   "schema": self.schema.model_json_schema()}
        opts = base_options(model=self.model, fallback_model=self.fallback_model,
                            system_prompt=system, output_format=fmt,
                            resume=resume)
        result, texts = self._converse(prompt, opts, timeout_s=timeout_s)
        if self.schema is not None:
            if result.structured_output is None:
                raise SdkError("model returned no structured output: "
                               + _result_error(result))
            return self.schema.model_validate(result.structured_output)
        return _Msg(result.result or "\n".join(texts))

    def stream(self, messages, config=None, **kw):
        yield self.invoke(messages, config, **kw)

    def _converse(self, prompt, opts, *, timeout_s=None):
        sdk = _sdk()
        q = self._query or sdk.query
        meter = self.meter.fork() if self.meter else None
        texts, box = [], {"result": None}

        def on_message(m):
            if isinstance(m, sdk.AssistantMessage):
                texts.extend(b.text for b in m.content
                             if isinstance(b, sdk.TextBlock))
            elif isinstance(m, sdk.ResultMessage):
                box["result"] = m
        try:
            _drive(q, prompt, opts, on_message, kind="structured",
                   timeout_s=timeout_s, meter=meter)
        except Exception as e:
            if meter and meter.run_id:
                meter.fail(e)
            raise
        result = box["result"]
        if meter:
            meter.finish(result)
        if result is None or (result.is_error and self.schema is None
                              and not result.result):
            raise SdkError(_result_error(result))
        return result, texts


# ── tools ────────────────────────────────────────────────────────────────
def lc_to_sdk_tools(lc_tools):
    """Re-expose LangChain @tool objects to the SDK as in-process MCP tools.
    The tools are synchronous (HTTP, sandboxed python), so each call runs on
    a worker thread to keep the SDK's event loop responsive."""
    sdk = _sdk()
    out = []
    for t in lc_tools:
        schema = t.tool_call_schema.model_json_schema()
        schema = {k: v for k, v in schema.items()
                  if k in ("type", "properties", "required", "$defs")}
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})

        def make(lc):
            async def handler(args):
                try:
                    res = await asyncio.to_thread(lc.invoke, args or {})
                except Exception as e:      # noqa: BLE001 — back to the model
                    return {"content": [{"type": "text",
                                         "text": f"tool error: {e}"[:2000]}],
                            "is_error": True}
                text = res if isinstance(res, str) else json.dumps(
                    res, default=str)
                return {"content": [{"type": "text", "text": text}]}
            return handler
        out.append(sdk.tool(t.name, t.description or t.name, schema)(make(t)))
    return out


def _tool_events(sdk, m):
    """(kind, payload) events from one SDK message, for transcripts and the
    chat stream."""
    if isinstance(m, sdk.AssistantMessage):
        for b in m.content:
            if isinstance(b, sdk.TextBlock) and b.text:
                yield "text", b.text
            elif isinstance(b, sdk.ToolUseBlock):
                yield "tool", {"name": b.name.removeprefix(TOOL_PREFIX),
                               "args": b.input}
    elif isinstance(m, sdk.UserMessage) and isinstance(m.content, list):
        for b in m.content:
            if isinstance(b, sdk.ToolResultBlock):
                c = b.content
                if isinstance(c, list):
                    c = "".join(x.get("text", "") for x in c
                                if isinstance(x, dict))
                yield "tool_result", str(c or "")


def run_tool_loop(chat: SdkChat, system, task, lc_tools, *,
                  max_turns=TOOL_LOOP_MAX_TURNS, timeout_s=None, info=None):
    """An analyst's research loop. Returns (transcript lines, truncation note).
    A loop that runs out of turns — or out of time — still hands back what it
    gathered, which then goes to the structured extraction like any other
    transcript. `info`, if given, receives "session_id" for a loop that ran
    to its end: the session a follow-up call may resume."""
    sdk = _sdk()
    q = chat._query or sdk.query
    tools = lc_to_sdk_tools(lc_tools)
    opts = base_options(model=chat.model, fallback_model=chat.fallback_model,
                        system_prompt=system, max_turns=max_turns,
                        mcp_tools=tools)
    meter = chat.meter.fork() if chat.meter else None
    lines, box = [], {"result": None}

    def on_message(m):
        for kind, payload in _tool_events(sdk, m):
            if kind == "text":
                lines.append(f"[ai] {payload[:2000]}")
            elif kind == "tool":
                lines.append(f"[ai] tool call {payload['name']} "
                             f"{json.dumps(payload['args'], default=str)[:1500]}")
            else:
                lines.append(f"[tool] {payload[:2000]}")
        if isinstance(m, sdk.ResultMessage):
            box["result"] = m
    truncated = None
    try:
        _drive(q, task, opts, on_message, kind="tool_loop",
               timeout_s=timeout_s, meter=meter)
    except Exception as e:                      # noqa: BLE001
        truncated = str(e)[:200]
        swallowed(log, "analyst tool loop cut short — reporting on what was "
                       "gathered", e)
    result = box["result"]
    if meter and meter.run_id:
        if result is None:
            meter.fail(truncated or "no result")
        else:
            meter.finish(result)
    if result is not None and result.is_error and not truncated:
        truncated = _result_error(result)[:200]
    if info is not None and result is not None and not truncated:
        info["session_id"] = result.session_id
    return lines, truncated


def stream_chat(chat: SdkChat, system, prompt, lc_tools, *, max_turns=25,
                resume=None, timeout_s=None):
    """Synchronous generator over a tool-using conversation: yields
    ("text", str) / ("tool", {name, args}) as they arrive, then
    ("session", id) and ("done", final_text). Bridges the SDK's async stream
    onto a thread.

    `resume` continues an earlier session by id, so `prompt` is only the new
    message; the id yielded at the end is the one to resume next time. A
    session that cannot be resumed fails like any other conversation — the
    caller decides what to start instead."""
    sdk = _sdk()
    q = chat._query or sdk.query
    opts = base_options(model=chat.model, fallback_model=chat.fallback_model,
                        system_prompt=system, max_turns=max_turns,
                        mcp_tools=lc_to_sdk_tools(lc_tools), resume=resume)
    out = queue.Queue()
    END = object()
    box = {"result": None}
    meter = chat.meter.fork() if chat.meter else None

    def on_message(m):
        for kind, payload in _tool_events(sdk, m):
            if kind != "tool_result":
                out.put((kind, payload))
        if isinstance(m, sdk.ResultMessage):
            box["result"] = m

    def worker():
        try:
            # urgent: a person is waiting on this one
            _drive(q, prompt, opts, on_message, kind="chat", urgent=True,
                   timeout_s=timeout_s, meter=meter)
            out.put(("result", box["result"]))
        except BaseException as e:      # noqa: BLE001 — surfaced to caller
            out.put(("error", e))
        out.put(END)

    threading.Thread(target=worker, daemon=True, name="claire-sdk").start()
    final, result, err = "", None, None
    while True:
        item = out.get()
        if item is END:
            break
        kind, payload = item
        if kind == "result":
            result = payload
        elif kind == "error":
            err = payload
        else:
            if kind == "text":
                final = payload
            yield kind, payload
    if meter and meter.run_id:
        if err is not None or result is None:
            meter.fail(err or "no result")
        else:
            meter.finish(result)
    if err is not None:
        raise err
    if result is not None and result.is_error:
        raise SdkError(_result_error(result))
    if result is not None and result.session_id:
        yield "session", result.session_id
    yield "done", (result.result if result and result.result else final)


def is_sdk(llm) -> bool:
    return isinstance(llm, SdkChat)


def not_ready(home=None):
    """Why an SDK agent cannot run right now, or None. Cheap enough for the
    dashboards to call on every refresh: no subprocess, no network."""
    try:
        _sdk()
    except ImportError:
        return "claude-agent-sdk not installed in the venv"
    home = Path(home) if home else Path.home()
    if not (home / ".claude" / ".credentials.json").exists():
        return "not signed in to Claude — run `claude` and /login"
    return None
