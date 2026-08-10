"""The trading pipeline graph (DESIGN.md §5).

Invariants live in STRUCTURE: all three analysts complete before bull; bear
strictly after bull; only the arbiter produces a Thesis; a `pass` verdict never
reaches the gate; nothing reaches `execute` except through `gate`.

Node behaviour is injected via `Deps` — production wiring builds them from the
provider registry and broker adapters (wiring.py); tests inject fakes. The
graph itself neither knows nor cares which, so graph tests exercise the REAL
structure, checkpointing, and interrupt/resume machinery offline.
"""
from dataclasses import dataclass, field
from typing import Callable

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from .state import Approval, PipelineState

ANALYSTS = ("fundamental", "technical", "news")


@dataclass
class Deps:
    analysts: dict                                  # agent_id -> fn(state)->AnalystReport
    bull: Callable                                  # fn(state)->DebateCase
    bear: Callable                                  # fn(state)->DebateCase
    arbiter: Callable                               # fn(state)->Thesis
    execute: Callable                               # fn(state)->dict state update
    record: Callable                                # fn(state)->dict state update
    prepare: Callable = field(default=lambda s: {})  # instrument/FX enrichment


def build_pipeline(deps: Deps, checkpointer):
    g = StateGraph(PipelineState)

    def _guarded(name, fn, key):
        """LLM nodes degrade honestly: an exception becomes an errors entry,
        and the arbitrate conditional routes a thesis-less run to record."""
        def node(state):
            try:
                return {key: fn(state)} if key != "reports" else {
                    "reports": [fn(state)]}
            except Exception as e:                  # noqa: BLE001
                return {"errors": [f"{name}: {e}"]}
        return node

    g.add_node("prepare", deps.prepare)
    for a in ANALYSTS:
        g.add_node(a, _guarded(a, deps.analysts[a], "reports"))
    g.add_node("bull", _guarded("bull", deps.bull, "bull"))
    g.add_node("bear", _guarded("bear", deps.bear, "bear"))
    g.add_node("arbitrate", _guarded("arbiter", deps.arbiter, "thesis"))
    g.add_node("gate", approval_gate)
    g.add_node("execute", deps.execute)             # deterministic — NOT guarded:
    g.add_node("record", deps.record)               # money errors must fail loudly

    g.add_edge(START, "prepare")
    for a in ANALYSTS:
        g.add_edge("prepare", a)                    # parallel fan-out
    g.add_edge(list(ANALYSTS), "bull")              # explicit barrier: ALL analysts
    g.add_edge("bull", "bear")                      # bear STRICTLY after bull
    g.add_edge("bear", "arbitrate")
    g.add_conditional_edges(
        "arbitrate",
        lambda s: "gate" if s.thesis and s.thesis.direction in ("buy", "sell")
        else "record",
        ["gate", "record"])
    g.add_conditional_edges(
        "gate",
        lambda s: {"approved": "execute", "rejected": "record",
                   "expired": "record"}[s.approval.status],
        ["execute", "record"])
    g.add_edge("execute", "record")
    g.add_edge("record", END)
    return g.compile(checkpointer=checkpointer)


def approval_gate(state: PipelineState):
    decision = interrupt({                          # ⏸ suspend; checkpoint persists
        "work_item_id": state.work_item_id,
        "ticker": state.thesis.ticker,
        "direction": state.thesis.direction,
        "conviction": state.thesis.conviction,
        "entry_low": state.thesis.entry_low,
        "entry_high": state.thesis.entry_high,
        "stop_loss": state.thesis.stop_loss,
        "take_profit": state.thesis.take_profit,
        "currency": state.thesis.currency,
        "conditions": state.thesis.conditions,
    })
    # Reached ONLY via the authorized resume path (§10) — the payload was
    # token-verified server-side before the graph ever sees it.
    return {"approval": Approval(**decision)}
