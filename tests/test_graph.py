"""Graph structure tests (DESIGN.md §18 layer 2) — scripted fakes, no keys,
no network; the REAL graph, checkpointer, and interrupt machinery."""
import sqlite3
from datetime import datetime, timezone

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from app.graph.pipeline import Deps, build_pipeline
from app.graph.state import (AnalystReport, DebateCase, Instrument,
                             PipelineState, Thesis)

NOW = datetime(2026, 8, 11, 8, 0, tzinfo=timezone.utc)
NVDA = Instrument(id="NASDAQ:NVDA", ticker="NVDA", exchange="NASDAQ",
                  currency="USD")


def report(agent):
    return AnalystReport(ticker="NVDA", agent=agent, signal="bullish",
                         conviction=0.8, summary=f"{agent} summary",
                         narrative_path=f"var/narratives/x/{agent}.md",
                         data_asof=NOW)


def case(side):
    return DebateCase(side=side, key_points=[f"{side} point"],
                      conviction=0.7, narrative_path=f"n/{side}.md")


def thesis(direction="buy"):
    kw = dict(entry_low=170.0, entry_high=175.0, stop_loss=160.0,
              take_profit=200.0) if direction == "buy" else {}
    return Thesis(ticker="NVDA", direction=direction, conviction=0.7,
                  currency="USD", narrative_path="n/thesis.md", **kw)


class Script:
    """Fake deps that log call order and capture the state they saw."""

    def __init__(self, verdict="buy", conn=None):
        self.calls = []
        self.seen = {}
        self.verdict = verdict
        an = {a: self._mk(a, lambda a=a: report(a)) for a in
              ("fundamental", "technical", "news")}
        self.deps = Deps(
            analysts=an,
            bull=self._mk("bull", lambda: case("bull")),
            bear=self._mk("bear", lambda: case("bear")),
            arbiter=self._mk("arbiter", lambda: thesis(self.verdict)),
            execute=self._node("execute", {"order_ids": ["ord_1"]}),
            record=self._node("record", {}),
        )

    def _mk(self, name, producer):
        def fn(state):
            self.calls.append(name)
            self.seen[name] = state
            return producer()
        return fn

    def _node(self, name, update):
        def fn(state):
            self.calls.append(name)
            self.seen[name] = state
            return update
        return fn


def init_state(kind="pipeline"):
    return PipelineState(work_item_id="wi_test", kind=kind, ticker="NVDA",
                         instrument=NVDA)


def approval_payload(status="approved"):
    return {"status": status, "size_base": 1000.0, "broker": "alpaca",
            "actor": "human", "token": "tok", "at": NOW.isoformat()}


@pytest.fixture
def saver():
    return SqliteSaver(sqlite3.connect(":memory:", check_same_thread=False))


CFG = {"configurable": {"thread_id": "wi_test"}}


def test_analysts_all_before_bull_and_bear_after_bull(saver):
    s = Script()
    pipe = build_pipeline(s.deps, saver)
    pipe.invoke(init_state(), CFG)                  # runs to the interrupt
    i_bull = s.calls.index("bull")
    for a in ("fundamental", "technical", "news"):
        assert s.calls.index(a) < i_bull
    assert s.calls.index("bear") > i_bull
    assert s.calls.index("arbiter") > s.calls.index("bear")
    # bear saw the bull case; arbiter saw both
    assert s.seen["bear"].bull is not None
    assert s.seen["arbiter"].bear is not None
    assert len(s.seen["bull"].reports) == 3


def test_pass_never_reaches_gate(saver):
    s = Script(verdict="pass")
    pipe = build_pipeline(s.deps, saver)
    out = pipe.invoke(init_state(), CFG)            # completes without interrupt
    assert "execute" not in s.calls
    assert "record" in s.calls
    assert out.get("approval") is None
    assert pipe.get_state(CFG).next == ()           # terminal, nothing suspended


def test_interrupt_payload_is_typed(saver):
    s = Script()
    pipe = build_pipeline(s.deps, saver)
    pipe.invoke(init_state(), CFG)
    (task,) = pipe.get_state(CFG).tasks
    payload = task.interrupts[0].value
    assert payload["ticker"] == "NVDA"
    assert payload["direction"] == "buy"
    assert payload["stop_loss"] == 160.0
    assert "execute" not in s.calls                 # suspended BEFORE execution


@pytest.mark.parametrize("status,executes", [
    ("approved", True), ("rejected", False), ("expired", False)])
def test_each_resume_status_routes_correctly(saver, status, executes):
    s = Script()
    pipe = build_pipeline(s.deps, saver)
    pipe.invoke(init_state(), CFG)
    out = pipe.invoke(Command(resume=approval_payload(status)), CFG)
    assert ("execute" in s.calls) is executes
    assert s.calls[-1] == "record"
    assert out["approval"] is not None and out["approval"].status == status
    if executes:
        assert out["order_ids"] == ["ord_1"]


def test_kill_and_restart_resumes_without_rerunning(tmp_path):
    dbfile = str(tmp_path / "ckpt.db")
    s1 = Script()
    pipe1 = build_pipeline(s1.deps, SqliteSaver(
        sqlite3.connect(dbfile, check_same_thread=False)))
    pipe1.invoke(init_state(), CFG)
    assert s1.calls.count("fundamental") == 1
    # "process dies"; a fresh graph over the same checkpoint file resumes
    s2 = Script()
    pipe2 = build_pipeline(s2.deps, SqliteSaver(
        sqlite3.connect(dbfile, check_same_thread=False)))
    out = pipe2.invoke(Command(resume=approval_payload()), CFG)
    assert "fundamental" not in s2.calls            # finished nodes DON'T re-run
    assert "execute" in s2.calls
    assert out["thesis"]["direction"] if isinstance(out["thesis"], dict) \
        else out["thesis"].direction == "buy"


def test_failed_arbiter_degrades_to_record(saver):
    s = Script()

    def broken(state):
        s.calls.append("arbiter")
        raise RuntimeError("provider outage")
    s.deps.arbiter = broken
    pipe = build_pipeline(s.deps, saver)
    out = pipe.invoke(init_state(), CFG)
    assert "execute" not in s.calls
    assert s.calls[-1] == "record"
    assert any("provider outage" in e for e in out["errors"])
