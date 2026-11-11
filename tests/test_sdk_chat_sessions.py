"""Claire's chat on the Claude SDK is a real multi-turn session (B8): each
turn resumes the thread's session and sends only the new message, instead of
flattening the whole history into one prompt. chat_threads stays the record.
Offline: scripted conversations via `_query`."""
import json

import claude_agent_sdk as sdk
import pytest

from app.graph import claire_agent
from app.graph.claire_agent import Claire
from app.providers import claude_sdk
from tests.test_claude_sdk import conn, result  # noqa: F401 — fixture

REFUSAL = "API Error: Opus 5's safeguards flagged this message"


class Desk:
    """A scripted SDK: answers every conversation with "answer N" in a
    session of its own ("s1", "s2", …; a resumed one keeps its id), unless
    `fail(prompt, options)` returns something to do instead — an exception
    to raise, or a ResultMessage to end on."""

    def __init__(self, fail=None):
        self.calls, self.fail, self.sessions = [], fail, 0

    @property
    def seen(self):
        return [(o.model, o.resume, p) for p, o in self.calls]

    def __call__(self, *, prompt, options):
        self.calls.append((prompt, options))
        bad = self.fail(prompt, options) if self.fail else None
        n = len(self.calls)

        async def gen():
            if isinstance(bad, Exception):
                raise bad
            if bad is not None:
                yield bad
                return
            if not options.resume:
                self.sessions += 1
            sid = options.resume or f"s{self.sessions}"
            yield sdk.AssistantMessage(
                content=[sdk.TextBlock(text=f"answer {n}")], model="m")
            yield result(result=f"answer {n}", session_id=sid)
        return gen()


def make_claire(conn, q, *, fallback="claude-sonnet-5"):
    claire = Claire(conn, repo=None, desk=None, market=None)
    claire._ensure_agent()
    _, system, tools = claire._sdk_chat
    claire._sdk_chat = (claude_sdk.SdkChat("claude-opus-5", _query=q,
                                           fallback_model=fallback),
                        system, tools)
    return claire


def ask(claire, text, thread="t"):
    return [json.loads(x) for x in claire.ask_stream(text, thread)]


def saved(conn, thread="t"):
    row = conn.execute("SELECT messages FROM chat_threads WHERE thread=?",
                       (thread,)).fetchone()
    return json.loads(row[0]) if row else None


def texts(out):
    return [o["text"] for o in out if o["kind"] == "text"]


def errors(out):
    return [o for o in out if o["kind"] == "error"]


OLD = [{"role": "user", "content": "old question"},
       {"role": "assistant", "content": "old answer"}]


# ── the normal case ──────────────────────────────────────────────────────
def test_each_turn_resumes_the_session_and_sends_only_the_new_message(conn):
    q = Desk()
    claire = make_claire(conn, q)
    ask(claire, "what do I hold?")
    ask(claire, "and how much cash?")
    out = ask(claire, "thanks")
    assert q.seen == [("claude-opus-5", None, "what do I hold?"),
                      ("claude-opus-5", "s1", "and how much cash?"),
                      ("claude-opus-5", "s1", "thanks")]
    assert texts(out) == ["answer 3"] and out[-1] == {"kind": "done"}
    # chat_threads is still the whole conversation
    assert saved(conn) == [
        {"role": "user", "content": "what do I hold?"},
        {"role": "assistant", "content": "answer 1"},
        {"role": "user", "content": "and how much cash?"},
        {"role": "assistant", "content": "answer 2"},
        {"role": "user", "content": "thanks"},
        {"role": "assistant", "content": "answer 3"}]


def test_the_session_to_resume_is_the_one_the_last_turn_ended_in(conn):
    async def q(*, prompt, options):
        # a CLI that gives a resumed conversation a new id each time
        yield result(result="ok", session_id=f"after {prompt}")
    claire = make_claire(conn, q)
    ask(claire, "one")
    assert claire._sessions == {"t": "after one"}
    ask(claire, "two")
    assert claire._sessions == {"t": "after two"}


def test_threads_have_separate_sessions(conn):
    q = Desk()
    claire = make_claire(conn, q)
    ask(claire, "hello", "dashboard")
    ask(claire, "hi", "main")
    ask(claire, "again", "dashboard")
    assert [r for _, r, _ in q.seen] == [None, None, "s1"]
    assert claire._sessions == {"dashboard": "s1", "main": "s2"}


def test_sessions_stay_isolated_in_the_work_dir(conn):
    q = Desk()
    claire = make_claire(conn, q)
    ask(claire, "one")
    ask(claire, "two")
    o = q.calls[1][1]
    assert o.resume == "s1" and o.cwd == str(claude_sdk.WORK_DIR)
    assert o.setting_sources == [] and o.tools == [] and o.strict_mcp_config
    assert o.env["ANTHROPIC_API_KEY"] == ""
    assert not o.continue_conversation and o.session_store is None


# ── no session to resume ─────────────────────────────────────────────────
def test_after_a_restart_a_new_session_starts_from_a_short_recap(conn):
    q = Desk()
    first = make_claire(conn, q)
    first._histories["t"] = [
        {"role": "user" if i % 2 == 0 else "assistant",
         "content": f"turn {i} " + "blah " * 2000} for i in range(40)]
    first._save("t")
    claire = make_claire(conn, q)                   # "restart": ids are gone
    out = ask(claire, "what changed today?")
    [(model, resume, prompt)] = q.seen
    assert resume is None and not errors(out)
    assert prompt.endswith("Richard's new message:\nwhat changed today?")
    assert "turn 39 blah" in prompt and "turn 34 blah" in prompt
    assert "turn 33" not in prompt                  # the last six only
    assert len(prompt) < 3500                       # not 40 × 10k characters
    assert "Conversation so far" not in prompt
    assert len(saved(conn)) == 42                   # the record is untouched
    assert ask(claire, "and?") and q.seen[-1][1:] == ("s1", "and?")


@pytest.mark.parametrize("failure", [
    sdk.ProcessError("Command failed", exit_code=1,
                     stderr="No conversation found with session ID: s1"),
    result(subtype="error_during_execution", is_error=True, session_id="",
           errors=["No conversation found with session ID: s1"]),
], ids=["cli exits", "error result"])
def test_a_session_that_cannot_be_resumed_falls_back_to_a_recap(conn, failure,
                                                                caplog):
    q = Desk(fail=lambda p, o: failure if o.resume == "s1" else None)
    claire = make_claire(conn, q)
    ask(claire, "what do I hold?")
    out = ask(claire, "and how much cash?")
    assert not errors(out) and texts(out) == ["answer 3"]
    (_, r2, p2), (m3, r3, p3) = q.seen[1:]
    assert (r2, p2) == ("s1", "and how much cash?")
    assert (m3, r3) == ("claude-opus-5", None)      # same model, new session
    assert "Richard asked: what do I hold?" in p3
    assert "You answered: answer 1" in p3
    assert p3.endswith("Richard's new message:\nand how much cash?")
    assert len(saved(conn)) == 4 and claire._sessions == {"t": "s2"}
    assert "could not be resumed" in caplog.text
    ask(claire, "thanks")
    assert q.seen[-1][1:] == ("s2", "thanks")


# ── refusals ─────────────────────────────────────────────────────────────
def refused():
    return result(is_error=True, result=REFUSAL, session_id="poisoned")


def test_a_refused_session_is_never_resumed_again(conn):
    q = Desk(fail=lambda p, o: refused() if o.resume == "s1" else None)
    claire = make_claire(conn, q)
    ask(claire, "what do I hold?")
    out = ask(claire, "tell me about the stop")
    assert not errors(out) and texts(out) == ["answer 3"]
    assert [r for _, r, _ in q.seen] == [None, "s1", None]
    assert q.seen[2][0] == "claude-opus-5"          # a recap, same model
    assert "You answered: answer 1" in q.seen[2][2]
    assert len(saved(conn)) == 4                    # nothing was dropped
    ask(claire, "thanks")
    assert q.seen[-1][1:] == ("s2", "thanks")       # the new one, not s1


def test_a_refused_recap_goes_to_the_backup_model_then_starts_fresh(conn):
    q = Desk(fail=lambda p, o: refused() if "old question" in p else None)
    claire = make_claire(conn, q)
    claire._histories["t"] = list(OLD)
    out = ask(claire, "status?")
    assert [(m, r) for m, r, _ in q.seen] == [
        ("claude-opus-5", None), ("claude-sonnet-5", None),
        ("claude-sonnet-5", None)]
    assert q.seen[-1][2] == "status?"               # only the question
    assert any("fresh conversation" in t for t in texts(out))
    assert saved(conn) == [{"role": "user", "content": "status?"},
                           {"role": "assistant", "content": "answer 3"}]
    ask(claire, "thanks")
    assert q.seen[-1][1:] == ("s1", "thanks")       # and it carries on there


def test_a_refused_first_message_is_not_sent_twice_the_same_way(conn):
    q = Desk(fail=lambda p, o: refused())
    claire = make_claire(conn, q)
    out = ask(claire, "hello")
    # no history, so "fresh" would repeat the backup attempt word for word
    assert q.seen == [("claude-opus-5", None, "hello"),
                      ("claude-sonnet-5", None, "hello")]
    assert errors(out) and "safeguards flagged" in errors(out)[0]["text"]
    assert claire._histories["t"] == [] and claire._sessions == {}


def test_without_a_backup_model_a_refusal_still_ends_in_a_fresh_start(conn):
    q = Desk(fail=lambda p, o: refused() if "old question" in p else None)
    claire = make_claire(conn, q, fallback=None)
    claire._histories["t"] = list(OLD)
    out = ask(claire, "status?")
    assert [(m, r) for m, r, _ in q.seen] == [("claude-opus-5", None)] * 2
    assert q.seen[-1][2] == "status?" and not errors(out)


# ── turns that fail ──────────────────────────────────────────────────────
def test_a_turn_that_fails_half_way_gives_up_its_session(conn):
    state = {"break": False}

    def q(*, prompt, options):
        calls.append((options.resume, prompt))

        async def gen():
            yield sdk.AssistantMessage(
                content=[sdk.TextBlock(text="Let me look.")], model="m")
            if state["break"]:
                raise RuntimeError("connection reset")
            yield result(result="done", session_id="s1")
        return gen()
    calls = []
    claire = make_claire(conn, q)
    ask(claire, "what do I hold?")
    state["break"] = True
    out = ask(claire, "and cash?")
    assert errors(out) and "connection reset" in errors(out)[0]["text"]
    assert len(calls) == 2                          # half-said: not retried
    assert len(claire._histories["t"]) == 2         # no unanswered turn kept
    assert claire._sessions == {}
    # the session now holds a turn the record does not: start from the record
    state["break"] = False
    ask(claire, "and cash?")
    resume, prompt = calls[-1]
    assert resume is None and "Richard asked: what do I hold?" in prompt
    assert "and cash?" in prompt and len(saved(conn)) == 4


def test_a_timed_out_turn_is_not_retried(conn, monkeypatch):
    calls = []

    def slow(chat, system, prompt, tools, **kw):
        calls.append(kw.get("resume"))
        raise claude_sdk.SdkTimeout("Claude SDK chat turn timed out")
        yield  # noqa — generator
    q = Desk()
    claire = make_claire(conn, q)
    ask(claire, "one")
    monkeypatch.setattr(claude_sdk, "stream_chat", slow)
    out = ask(claire, "two")
    assert calls == ["s1"] and "timed out" in errors(out)[0]["text"]
    assert claire._sessions == {} and len(claire._histories["t"]) == 2


def test_other_errors_on_a_new_session_are_final(conn):
    q = Desk(fail=lambda p, o: RuntimeError("network down"))
    claire = make_claire(conn, q)
    out = ask(claire, "hi")
    assert len(q.calls) == 1 and "network down" in errors(out)[0]["text"]


def test_a_reader_who_hangs_up_leaves_no_half_turn_behind(conn):
    q = Desk()
    claire = make_claire(conn, q)
    ask(claire, "one")
    stream = claire.ask_stream("two", "t")
    assert json.loads(next(stream))["text"] == "answer 2"
    stream.close()                                  # the client went away
    assert claire._histories["t"] == saved(conn) and len(saved(conn)) == 2
    assert claire._sessions == {}                   # s1 may now hold "two"
    ask(claire, "three")
    assert q.seen[-1][1] is None and "You answered: answer 1" in q.seen[-1][2]


def test_the_auto_reset_starts_a_new_session_too(conn, monkeypatch):
    monkeypatch.setattr(claire_agent, "MAX_TURNS_PER_THREAD", 1)
    q = Desk()
    claire = make_claire(conn, q)
    ask(claire, "one")
    ask(claire, "two")                              # 4 messages: over 1 × 2
    ask(claire, "three")
    assert q.seen == [("claude-opus-5", None, "one"),
                      ("claude-opus-5", "s1", "two"),
                      ("claude-opus-5", None, "three")]
    assert len(saved(conn)) == 2


# ── the pieces ───────────────────────────────────────────────────────────
def test_recap_prompt_is_short_and_ends_with_the_new_message():
    assert claude_sdk.recap_prompt([], "hello") == "hello"
    assert claude_sdk.recap_prompt([("assistant", "  ")], "hello") == "hello"
    turns = [("user", "q1"), ("assistant", "a1\n\n| a | table |\n" + "x" * 900),
             ("user", "q2"), ("assistant", "a2")]
    p = claude_sdk.recap_prompt(turns, "now what?", max_turns=3)
    assert "q1" not in p                            # only the last three
    assert "- You answered: a1 | a | table | xxx" in p      # one line each
    assert "…\n- Richard asked: q2\n- You answered: a2\n" in p
    assert p.endswith("\n\nRichard's new message:\nnow what?")
    assert max(len(line) for line in p.splitlines()) < 450


def test_stream_chat_resumes_and_reports_the_session():
    seen = []

    async def q(*, prompt, options):
        seen.append((prompt, options.resume))
        yield sdk.AssistantMessage(content=[sdk.TextBlock(text="hi")],
                                   model="m")
        yield result(result="hi", session_id="abc")
    chat = claude_sdk.SdkChat("m", _query=q)
    assert list(claude_sdk.stream_chat(chat, "S", "hello", [],
                                       resume="abc")) == [
        ("text", "hi"), ("session", "abc"), ("done", "hi")]
    assert seen == [("hello", "abc")]
    assert claude_sdk.base_options(model="m").resume is None
