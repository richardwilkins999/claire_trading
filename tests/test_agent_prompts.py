"""Stored agent prompts (REVIEW-2026-11-11 B9).

One template was given to every agent, so the debaters, the recorder, the
screener and the chat agent were all told they were "analysing {ticker}"
and should "use your tools" — and only the analyst path filled the
placeholder in, so the others saw the braces.
"""
import json
from types import SimpleNamespace

import pytest

from app.accounting import db
from app.graph import nodes_analysis
from app.graph.state import DebateCase
from app.providers import registry, seeds
from app.screener import Shortlist, screener_pick

T0 = 1_786_456_800
ANALYSTS = ("fundamental", "technical", "news")
DEBATERS = ("bull", "bear", "arbiter")
NO_TICKER_ROLES = ("screener", "claire_chat", "recorder", "maintainer")


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init(c)
    seeds.seed(c, ts=T0)
    return c


def _prompt(conn, agent_id):
    return registry.agent_row(conn, agent_id)["system_prompt"]


# ── the seeded defaults ─────────────────────────────────────────────────────
def test_only_roles_that_have_a_ticker_mention_one(conn):
    for a in ANALYSTS + DEBATERS:
        assert "{ticker}" in _prompt(conn, a), a
    for a in NO_TICKER_ROLES:
        assert "{ticker}" not in _prompt(conn, a), a
        assert "analysing" not in _prompt(conn, a), a


def test_no_agent_without_tools_is_told_to_use_tools(conn):
    for r in conn.execute("SELECT id, tools, system_prompt FROM agents"):
        if not json.loads(r["tools"]):
            assert "Use your tools" not in r["system_prompt"], r["id"]
            assert "narrative tool" not in r["system_prompt"], r["id"]
    for a in DEBATERS:
        assert "You have no tools" in _prompt(conn, a)
    for a in ANALYSTS:                       # unchanged where it was right
        assert "Use your tools to gather REAL data" in _prompt(conn, a)
        assert "narrative tool" in _prompt(conn, a)


def test_roles_read_as_roles(conn):
    assert _prompt(conn, "news").startswith(
        "You are the news analyst on Claire's trading desk, analysing")
    assert _prompt(conn, "claire_chat").startswith("You are Claire, the desk")
    assert "the claire chat" not in _prompt(conn, "claire_chat")
    # each role keeps its own brief
    assert "SOLE producer of theses" in _prompt(conn, "arbiter")
    assert "three factual sentences" in _prompt(conn, "recorder")


# ── existing installs ───────────────────────────────────────────────────────
def _downgrade(conn):
    """Put every agent back on the first template, as a desk seeded before
    this fix has them."""
    briefs = {a[0]: a[5] for a in seeds.AGENTS}
    for aid, brief in briefs.items():
        old = seeds._P_V1.format(role=aid.replace("_", " "), brief=brief)
        conn.execute("UPDATE agents SET system_prompt=?, version=1 WHERE id=?",
                     (old, aid))
        conn.execute("DELETE FROM agent_versions WHERE agent_id=?", (aid,))
        conn.execute("INSERT INTO agent_versions (agent_id, version,"
                     " system_prompt, changed_at) VALUES (?,1,?,?)",
                     (aid, old, T0))
    return briefs


def test_an_unedited_old_prompt_is_upgraded_as_a_new_version(conn):
    briefs = _downgrade(conn)
    conn.execute("UPDATE agents SET model='operator-chose-this' WHERE"
                 " id='bull'")
    seeds.seed(conn, ts=T0 + 5)
    for aid, brief in briefs.items():
        row = registry.agent_row(conn, aid)
        assert row["system_prompt"] == seeds.default_prompt(aid, brief), aid
        assert row["version"] == 2 and row["updated_at"] == T0 + 5
        hist = {r["version"]: r["system_prompt"] for r in conn.execute(
            "SELECT version, system_prompt FROM agent_versions WHERE"
            " agent_id=?", (aid,))}
        assert set(hist) == {1, 2}
        assert "analysing {ticker}" in hist[1]      # the old text is kept
        assert hist[2] == row["system_prompt"]
    # only the prompt moved: an operator's model choice is untouched
    assert registry.agent_row(conn, "bull")["model"] == "operator-chose-this"

    seeds.seed(conn, ts=T0 + 9)                     # and it happens once
    assert registry.agent_row(conn, "bull")["version"] == 2
    assert conn.execute("SELECT COUNT(*) FROM agent_versions WHERE"
                        " agent_id='bull'").fetchone()[0] == 2


def test_an_edited_prompt_is_left_exactly_as_written(conn):
    _downgrade(conn)
    mine = "You are the bear. Be brutal about {ticker}."
    conn.execute("UPDATE agents SET system_prompt=? WHERE id='bear'", (mine,))
    seeds.seed(conn, ts=T0 + 5)
    row = registry.agent_row(conn, "bear")
    assert row["system_prompt"] == mine and row["version"] == 1


# ── nothing reaches a model with the braces in it ───────────────────────────
def test_render_prompt():
    assert registry.render_prompt("on {ticker}, {ticker}!", "D05") \
        == "on D05, D05!"
    assert "{ticker}" not in registry.render_prompt("analysing {ticker}.")
    assert registry.render_prompt("no placeholder", "D05") == "no placeholder"
    assert registry.render_prompt(None) == ""


class _LLM:
    def __init__(self, answer, seen):
        self.answer, self.seen = answer, seen

    def invoke(self, messages):
        self.seen.append(messages)
        return self.answer


def test_debaters_get_the_ticker(conn, monkeypatch):
    seen = []
    case = DebateCase(side="bull", key_points=["x"], conviction=0.5)
    monkeypatch.setitem(nodes_analysis.PROMPTS, "bull", lambda s: "the record")
    narratives = SimpleNamespace(write=lambda wi, name, md: f"{wi}/{name}.md")
    node = nodes_analysis.make_debater(
        conn, "bull", narratives,
        structured_factory=lambda a, schema, wi: (
            _LLM(case, seen), _prompt(conn, a)))
    node(SimpleNamespace(work_item_id="wi_1", ticker="D05"))
    system = dict(seen[0])["system"]
    assert "in the debate on D05." in system and "{ticker}" not in system


def test_screener_gets_no_braces_even_from_an_old_prompt(conn):
    seen = []
    old = seeds._P_V1.format(role="screener", brief="pick")   # an edited/old one
    picks = screener_pick(
        conn, None, ["SGX"], set(), with_catalysts=False, clock=lambda: T0,
        structured_factory=lambda a, schema, wi: (
            _LLM(Shortlist(picks=[]), seen), old),
        _gather=lambda *a, **k: [{"ticker": "D05", "exchange": "SGX",
                                  "name": "DBS", "scans": ["momentum"]}])
    assert picks == []
    system = dict(seen[0])["system"]
    assert "{ticker}" not in system and registry.NO_TICKER in system


def test_recorder_and_agent_ask_render_their_prompt(conn, monkeypatch):
    from app import wiring
    from app.api.agent_ask import make_agent_ask
    conn.execute("UPDATE agents SET system_prompt='About {ticker}: be brief.'"
                 " WHERE id IN ('recorder', 'bear')")
    seen = []

    class Chat:
        def invoke(self, messages):
            seen.append(messages)
            return SimpleNamespace(content="done")

        def stream(self, messages):
            seen.append(messages)
            yield SimpleNamespace(content="hi")
    monkeypatch.setattr(registry, "model_for", lambda *a, **k: Chat())
    assert wiring._recorder_prose(conn, {})("run record") == "done"
    list(make_agent_ask(conn, None, llm_factory=lambda a: Chat(),
                        clock=lambda: T0)({"agent_id": "bear", "text": "?"}))
    assert len(seen) == 2
    for messages in seen:
        system = dict(messages)["system"]
        assert system.startswith(f"About {registry.NO_TICKER}: be brief.")


def test_chat_agent_renders_its_prompt(conn, monkeypatch):
    from app.graph import claire_agent
    from app.providers import claude_sdk
    conn.execute("UPDATE agents SET system_prompt='Claire, on {ticker}.'"
                 " WHERE id='claire_chat'")
    monkeypatch.setattr(registry, "model_for", lambda *a, **k: "model")
    monkeypatch.setattr(claude_sdk, "is_sdk", lambda m: True)
    monkeypatch.setattr(claire_agent, "build_tools", lambda *a, **k: [])
    c = claire_agent.Claire(conn, None, None, None, env={})
    c._ensure_agent()
    assert c._sdk_chat[1] == f"Claire, on {registry.NO_TICKER}."
