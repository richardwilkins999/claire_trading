"""An analyst's research loop and its structured extraction in ONE Claude SDK
session (REVIEW-2026-11-11 §4.1 item 4) — opt-in with
CLAIRE_SDK_ANALYST_ONE_SESSION=1, and it falls back to the two-call path
whenever the session cannot be used. Offline: the SDK's query is scripted."""
import claude_agent_sdk as sdk
import pytest

from app.graph.nodes_analysis import default_structured_factory, make_analyst
from app.graph.state import AnalystReport, Instrument, PipelineState
from app.providers import claude_sdk
from app.tools.files import Narratives
from tests.test_claude_sdk import conn, result  # noqa: F401 — fixture

REPORT = {"ticker": "NVDA", "agent": "news", "signal": "bullish",
          "conviction": 0.6, "summary": "Guidance raised.",
          "key_findings": ["FY27 revenue guide +12% on 2026-11-05"],
          "data_asof": "2026-11-11T09:00:00Z", "sources": ["newswire"]}
ON = {"CLAIRE_SDK_ANALYST_ONE_SESSION": "1"}


class Sdk:
    """A scripted SDK: the research loop (it has the desk's tool server, and
    no output format) says one thing and ends in session "research-1"; an
    extraction returns REPORT. `loop_result` / `fail_resume` change that."""

    def __init__(self, loop_result=None, fail_resume=None):
        self.calls = []
        self.loop_result = loop_result or result(session_id="research-1")
        self.fail_resume = fail_resume

    def __call__(self, *, prompt, options):
        extraction = options.output_format is not None
        self.calls.append(("extract" if extraction else "loop",
                           options.resume, prompt))

        async def gen():
            if not extraction:
                yield sdk.AssistantMessage(
                    content=[sdk.TextBlock(text="Guidance was raised.")],
                    model="m")
                yield self.loop_result
            elif options.resume and self.fail_resume is not None:
                if isinstance(self.fail_resume, Exception):
                    raise self.fail_resume
                yield self.fail_resume
            else:
                yield result(structured_output=REPORT,
                             session_id=options.resume or "extract-1")
        return gen()


def run(conn, tmp_path, monkeypatch, q, env):
    # on top of conftest's block: still no real SDK, just a scripted one
    monkeypatch.setattr(sdk, "query", q)
    env = {**env}
    node = make_analyst(conn, "news", Narratives(tmp_path),
                        lambda names, st: [],
                        structured_factory=default_structured_factory(
                            conn, env=env), env=env)
    state = PipelineState(work_item_id="wi_x", ticker="NVDA",
                          instrument=Instrument(id="NASDAQ:NVDA", ticker="NVDA",
                                                exchange="NASDAQ",
                                                currency="USD"))
    return node(state)


def test_by_default_the_extraction_is_its_own_call(conn, tmp_path,
                                                   monkeypatch):
    q = Sdk()
    out = run(conn, tmp_path, monkeypatch, q, {})
    assert isinstance(out, AnalystReport) and out.signal == "bullish"
    (k1, r1, _), (k2, r2, p2) = q.calls
    assert (k1, r1, k2, r2) == ("loop", None, "extract", None)
    assert "Your research transcript:\n[ai] Guidance was raised." in p2
    assert "Now emit the structured AnalystReport" in p2


def test_opted_in_the_extraction_resumes_the_research_session(conn, tmp_path,
                                                              monkeypatch):
    q = Sdk()
    out = run(conn, tmp_path, monkeypatch, q, ON)
    assert out.signal == "bullish" and out.agent == "news"
    (k1, r1, _), (k2, r2, p2) = q.calls
    assert (k1, r1, k2, r2) == ("loop", None, "extract", "research-1")
    # the session already holds the research: only the request is sent
    assert p2.startswith("Now emit the structured AnalystReport")
    assert "research transcript" not in p2
    # both artifacts are still written, and both calls are metered
    n = Narratives(tmp_path)
    assert "Guidance was raised." in n.read("wi_x", "news.transcript")
    assert "**Signal: BULLISH**" in n.read("wi_x", "news.report")
    runs = conn.execute("SELECT status FROM agent_runs WHERE agent_id='news'"
                        ).fetchall()
    assert [r["status"] for r in runs] == ["ok", "ok"]


@pytest.mark.parametrize("failure", [
    sdk.ProcessError("Command failed", exit_code=1,
                     stderr="No conversation found with session ID"),
    result(structured_output=None),
    result(is_error=True, subtype="error_during_execution",
           errors=["tool_use ids were found without tool_result blocks"]),
], ids=["cli exits", "no structured output", "error result"])
def test_a_session_that_cannot_be_used_falls_back_to_the_transcript(
        conn, tmp_path, monkeypatch, failure, caplog):
    q = Sdk(fail_resume=failure)
    out = run(conn, tmp_path, monkeypatch, q, ON)
    assert out.signal == "bullish"                  # the run is not failed
    assert [(k, r) for k, r, _ in q.calls] == [
        ("loop", None), ("extract", "research-1"), ("extract", None)]
    assert "Your research transcript:" in q.calls[-1][2]
    assert "extracting from the transcript instead" in caplog.text


def test_a_loop_that_was_cut_short_is_not_resumed(conn, tmp_path,
                                                  monkeypatch):
    q = Sdk(loop_result=result(subtype="error_max_turns", is_error=True,
                               session_id="research-1"))
    out = run(conn, tmp_path, monkeypatch, q, ON)
    assert out.signal == "bullish"
    assert [(k, r) for k, r, _ in q.calls] == [("loop", None),
                                               ("extract", None)]
    assert "RESEARCH CUT SHORT" in q.calls[-1][2]


def test_other_providers_and_test_doubles_are_untouched(conn, tmp_path):
    seen = []

    class LLM:                              # not an SdkChat: no `resume`
        def invoke(self, messages):
            seen.append(messages[1][1])
            return AnalystReport(**REPORT)
    node = make_analyst(conn, "news", Narratives(tmp_path),
                        lambda names, st: [],
                        structured_factory=lambda a, s, w: (LLM(), "sys"),
                        agent_factory=lambda *a: "[ai] notes", env=ON)
    out = node(PipelineState(
        work_item_id="wi_x", ticker="NVDA",
        instrument=Instrument(id="NASDAQ:NVDA", ticker="NVDA",
                              exchange="NASDAQ", currency="USD")))
    assert out.signal == "bullish" and "[ai] notes" in seen[0]


def test_tool_loop_reports_its_session_only_when_it_finished():
    info = {}
    chat = claude_sdk.SdkChat("m", _query=Sdk())
    claude_sdk.run_tool_loop(chat, "S", "T", [], info=info)
    assert info == {"session_id": "research-1"}
    info = {}
    chat = claude_sdk.SdkChat("m", _query=Sdk(loop_result=result(
        subtype="error_max_turns", is_error=True, session_id="research-1")))
    claude_sdk.run_tool_loop(chat, "S", "T", [], info=info)
    assert info == {}
