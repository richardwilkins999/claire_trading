"""Every Claude SDK conversation is bounded (REVIEW-2026-11-11 §4.1): a
wall-clock deadline that stops a hung `claude` subprocess, and a process-wide
cap on how many run at once. Offline: scripted conversations via `_query`,
and the deadline's clock replaced, so no test waits for a real timeout."""
import asyncio
import threading
import time

import claude_agent_sdk as sdk
import pytest

from app.graph.state import DebateCase
from app.providers import claude_sdk, registry
from tests.test_claude_sdk import conn, result, scripted  # noqa: F401 — fixture


def hung(*first, closed=None):
    """A conversation that says `first` and then never ends — the `claude`
    subprocess that stopped answering. `closed` records that it was shut
    down (in the real SDK that is where the subprocess is terminated)."""
    async def q(*, prompt, options):
        try:
            for m in first:
                yield m
            await asyncio.Event().wait()
        finally:
            if closed is not None:
                closed.append(True)
    return q


@pytest.fixture
def deadline_now(monkeypatch):
    """The deadline passes at once; returns the seconds each one was set to."""
    asked = []

    async def at_once(seconds):
        asked.append(seconds)
    monkeypatch.setattr(claude_sdk, "_sleep", at_once)
    return asked


@pytest.fixture
def own_gate(monkeypatch):
    def make(limit):
        g = claude_sdk.Gate(limit)
        monkeypatch.setattr(claude_sdk, "_gate", g)
        return g
    return make


def until(cond, what, seconds=5.0):
    end = time.monotonic() + seconds
    while not cond():
        assert time.monotonic() < end, f"never happened: {what}"
        time.sleep(0.002)


# ── the deadline ─────────────────────────────────────────────────────────
def test_a_hung_structured_call_times_out_and_is_shut_down(conn, deadline_now):
    closed = []
    base = registry.build_llm(conn, "claude_max", "claude-sonnet-5")
    base._query = hung(closed=closed)
    llm = base.with_structured_output(DebateCase).with_config(
        callbacks=[registry.MeterCallback(conn, "bull", "wi_1")])
    with pytest.raises(claude_sdk.SdkTimeout) as e:
        llm.invoke("make the case")
    assert isinstance(e.value, claude_sdk.SdkError)
    assert "timed out after 300s" in str(e.value)
    assert "CLAIRE_SDK_TIMEOUT_S" in str(e.value)
    assert closed == [True]                 # not left running
    assert deadline_now == [300]
    row = conn.execute("SELECT status, error FROM agent_runs").fetchone()
    assert row["status"] == "error" and "timed out" in row["error"]


def test_a_hung_tool_loop_keeps_what_it_gathered(conn, deadline_now):
    closed = []
    chat = registry.model_for(conn, "news", work_item_id="wi_2")
    chat._query = hung(sdk.AssistantMessage(
        content=[sdk.TextBlock(text="half done")], model="m"), closed=closed)
    lines, truncated = claude_sdk.run_tool_loop(chat, "S", "T", [])
    assert "half done" in "\n".join(lines)
    assert "timed out after 600s" in truncated
    assert closed == [True] and deadline_now == [600]
    row = conn.execute("SELECT status, error FROM agent_runs").fetchone()
    assert row["status"] == "error" and "timed out" in row["error"]


def test_a_hung_chat_turn_raises_after_what_was_said(deadline_now):
    closed = []
    chat = claude_sdk.SdkChat("m", _query=hung(sdk.AssistantMessage(
        content=[sdk.TextBlock(text="Let me look.")], model="m"),
        closed=closed))
    got = []
    with pytest.raises(claude_sdk.SdkTimeout):
        for item in claude_sdk.stream_chat(chat, "S", "hi", []):
            got.append(item)
    assert got == [("text", "Let me look.")]
    assert closed == [True] and deadline_now == [300]


def test_the_deadline_also_works_on_the_real_clock():
    closed = []
    chat = claude_sdk.SdkChat("m", _query=hung(closed=closed))
    started = time.monotonic()
    with pytest.raises(claude_sdk.SdkTimeout) as e:
        chat.invoke("x", timeout_s=0.05)
    assert "timed out after 0.05s" in str(e.value)
    assert closed == [True] and time.monotonic() - started < 5


@pytest.fixture
def deadline_never(monkeypatch):
    async def never(seconds):
        await asyncio.Event().wait()
    monkeypatch.setattr(claude_sdk, "_sleep", never)


def test_a_conversation_that_finishes_is_closed_and_not_timed_out(
        deadline_never):
    closed = []

    async def q(*, prompt, options):
        try:
            yield sdk.AssistantMessage(content=[sdk.TextBlock(text="hello")],
                                       model="m")
            await asyncio.sleep(0)          # let the deadline start waiting
            yield result(result="hello")
        finally:
            closed.append(True)
    msg = claude_sdk.SdkChat("m", _query=q).invoke("x")
    assert msg.content == "hello" and closed == [True]


def test_a_failing_conversation_still_raises_its_own_error(deadline_never):
    async def q(*, prompt, options):
        await asyncio.sleep(0)
        raise RuntimeError("network down")
        yield  # noqa — generator
    with pytest.raises(RuntimeError, match="network down"):
        claude_sdk.SdkChat("m", _query=q).invoke("x")


def test_timeout_setting():
    t = claude_sdk.timeout_for
    assert (t("tool_loop", {}), t("chat", {}), t("structured", {})) == \
        (600, 300, 300)
    # the setting is the tool-loop limit and a ceiling for the shorter kinds
    more = {"CLAIRE_SDK_TIMEOUT_S": "900"}
    assert (t("tool_loop", more), t("chat", more), t("structured", more)) == \
        (900, 300, 300)
    less = {"CLAIRE_SDK_TIMEOUT_S": "45"}
    assert (t("tool_loop", less), t("chat", less), t("structured", less)) == \
        (45, 45, 45)
    for bad in ("", "soon", "0", "-5", "nan", "inf"):
        assert t("tool_loop", {"CLAIRE_SDK_TIMEOUT_S": bad}) == 600


def test_timeout_setting_is_read_from_the_environment(monkeypatch,
                                                      deadline_now):
    monkeypatch.setenv("CLAIRE_SDK_TIMEOUT_S", "120")
    with pytest.raises(claude_sdk.SdkTimeout, match="after 120s"):
        claude_sdk.SdkChat("m", _query=hung()).invoke("x")
    assert deadline_now == [120]


# ── the cap ──────────────────────────────────────────────────────────────
def test_gate_queues_past_the_limit():
    g = claude_sdk.Gate(2)
    g.acquire()
    g.acquire()
    got = threading.Event()

    def third():
        with g.slot():
            got.set()
    t = threading.Thread(target=third, daemon=True)
    t.start()
    until(lambda: g.waiting == 1, "the third caller queued")
    assert not got.is_set() and g.active == 2
    g.release()
    t.join(5)
    assert got.is_set() and (g.active, g.waiting) == (1, 0)


def test_gate_lets_a_person_ahead_of_queued_background_work():
    g = claude_sdk.Gate(1)
    g.acquire()
    order = []

    def caller(name, urgent):
        g.acquire(urgent=urgent)
        order.append(name)
    bg = threading.Thread(target=caller, args=("pipeline", False), daemon=True)
    bg.start()
    until(lambda: g.waiting == 1, "pipeline call queued")
    chat = threading.Thread(target=caller, args=("chat", True), daemon=True)
    chat.start()
    until(lambda: g.waiting == 2, "chat queued behind it")
    g.release()
    chat.join(5)
    assert order == ["chat"] and g.waiting == 1
    g.release()
    bg.join(5)
    assert order == ["chat", "pipeline"]


def test_gate_releases_its_slot_when_the_work_fails():
    g = claude_sdk.Gate(1)
    with pytest.raises(ValueError):
        with g.slot():
            raise ValueError("boom")
    assert g.active == 0


def test_conversations_past_the_cap_wait_their_turn(own_gate):
    g = own_gate(1)
    go, state = threading.Event(), {"running": 0, "peak": 0, "started": 0}

    async def q(*, prompt, options):
        state["running"] += 1
        state["started"] += 1
        state["peak"] = max(state["peak"], state["running"])
        await asyncio.to_thread(go.wait)
        state["running"] -= 1
        yield result(result=f"answer to {prompt}")

    answers, errors = [], []

    def ask(prompt):
        try:
            answers.append(claude_sdk.SdkChat("m", _query=q).invoke(
                prompt).content)
        except Exception as e:              # noqa: BLE001 — asserted below
            errors.append(e)
    threads = [threading.Thread(target=ask, args=(p,), daemon=True)
               for p in ("a", "b", "c")]
    for t in threads:
        t.start()
    until(lambda: g.waiting == 2 and state["started"] == 1,
          "one running, two queued")
    go.set()
    for t in threads:
        t.join(5)
    assert errors == [] and sorted(answers) == [
        "answer to a", "answer to b", "answer to c"]    # queued, not refused
    assert state["peak"] == 1 and (g.active, g.waiting) == (0, 0)


def test_every_kind_of_conversation_takes_a_slot(own_gate):
    g = own_gate(3)
    seen = []

    async def q(*, prompt, options):
        seen.append(g.active)
        yield result(result="ok")
    chat = claude_sdk.SdkChat("m", _query=q)
    chat.invoke("x")
    claude_sdk.run_tool_loop(chat, "S", "T", [])
    list(claude_sdk.stream_chat(chat, "S", "hi", []))
    assert seen == [1, 1, 1] and g.active == 0


def test_a_timed_out_conversation_gives_its_slot_back(own_gate, deadline_now):
    g = own_gate(1)
    with pytest.raises(claude_sdk.SdkTimeout):
        claude_sdk.SdkChat("m", _query=hung()).invoke("x")
    assert g.active == 0
    assert claude_sdk.SdkChat("m", _query=scripted(
        result(result="ok"))).invoke("x").content == "ok"


def test_a_queued_run_is_metered_from_when_it_starts(conn, own_gate):
    g = own_gate(1)
    g.acquire()                             # the only slot is taken
    base = registry.build_llm(conn, "claude_max", "claude-sonnet-5")
    base._query = scripted(result(result="ok"))
    llm = base.with_config(
        callbacks=[registry.MeterCallback(conn, "bull", "wi_1")])
    t = threading.Thread(target=llm.invoke, args=("x",), daemon=True)
    t.start()
    until(lambda: g.waiting == 1, "the call queued")
    assert conn.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0] == 0
    g.release()
    t.join(5)
    assert conn.execute("SELECT status FROM agent_runs").fetchone()[0] == "ok"


def test_cap_setting(monkeypatch):
    monkeypatch.setattr(claude_sdk, "_gate", None)
    monkeypatch.delenv("CLAIRE_SDK_MAX_CONCURRENT", raising=False)
    assert claude_sdk.gate().limit == 4
    assert claude_sdk.gate() is claude_sdk.gate()       # one per process
    for raw, limit in (("2", 2), ("0", 4), ("many", 4), ("", 4)):
        monkeypatch.setattr(claude_sdk, "_gate", None)
        monkeypatch.setenv("CLAIRE_SDK_MAX_CONCURRENT", raw)
        assert claude_sdk.gate().limit == limit
