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
from decimal import Decimal, InvalidOperation
from typing import Callable

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from .state import Approval, PipelineState, Thesis

ANALYSTS = ("fundamental", "technical", "news")

# How far an entry level may sit from the last traded price before the desk
# refuses to put it in front of a human. Wide enough for a genuine "buy the
# dip" limit; far too narrow for another stock's price levels (H5).
PRICE_BAND = Decimal("0.25")


@dataclass
class Deps:
    analysts: dict                                  # agent_id -> fn(state)->AnalystReport
    bull: Callable                                  # fn(state)->DebateCase
    bear: Callable                                  # fn(state)->DebateCase
    arbiter: Callable                               # fn(state)->Thesis
    execute: Callable                               # fn(state)->dict state update
    record: Callable                                # fn(state)->dict state update
    prepare: Callable = field(default=lambda s: {})  # instrument/FX enrichment
    # fn(instrument) -> last price in the instrument's currency, for the
    # thesis price-band check. None (or a failing lookup) skips the check
    # here; execute re-checks against the live quote and fails closed.
    last_price: Callable | None = None


def _norm_ticker(t: str) -> str:
    """'SGX:C07', 'C07.SI', ' c07 ' -> 'C07' — formatting is not a different
    stock; a different symbol is."""
    t = (t or "").strip().upper()
    if ":" in t:
        t = t.split(":", 1)[1]
    if "." in t:
        t = t.split(".", 1)[0]
    return t


def vet_thesis(state, th: Thesis, last_price=None) -> Thesis:
    """Pin the arbiter's verdict to the run's instrument (H5).

    The approval card shows the thesis while execute trades the run's
    instrument; nothing tied the two together, so a thesis with
    ticker='AAPL' on an SGX:C07 run showed AAPL to the human and ordered C07
    at AAPL's price levels. Ticker and currency are now pinned to the
    instrument; if the arbiter named a different stock or currency, or its
    entry sits outside PRICE_BAND of the last price, its prices cannot be
    trusted and the verdict is downgraded to a PASS whose conditions say
    why — it never reaches the gate, and the human can still override it
    with levels of their own."""
    inst = state.instrument
    problems = []
    if _norm_ticker(th.ticker) != _norm_ticker(inst.ticker):
        problems.append(f"thesis names {th.ticker!r} but this run is for "
                        f"{inst.id}")
    if (th.currency or "").strip() != inst.currency:
        problems.append(f"thesis is priced in {th.currency!r} but "
                        f"{inst.id} trades in {inst.currency}")
    if not problems and th.direction in ("buy", "sell") and last_price:
        try:
            ref = Decimal(str(last_price(inst)))
        except Exception:                           # noqa: BLE001 — execute
            ref = None                              # re-checks, fail-closed
        if ref is not None and ref > 0:
            for name in ("entry_low", "entry_high"):
                v = getattr(th, name)
                if v is None:
                    continue
                try:
                    off = abs(Decimal(str(v)) / ref - 1)
                except (InvalidOperation, ZeroDivisionError):
                    continue
                if off > PRICE_BAND:
                    problems.append(
                        f"{name} {v} is {off * 100:.0f}% from the last price "
                        f"{ref} (band ±{PRICE_BAND * 100:.0f}%)")
    pinned = th.model_dump()
    pinned.update(ticker=inst.ticker, currency=inst.currency)
    if problems and th.direction in ("buy", "sell"):
        pinned.update(direction="pass", conditions=list(th.conditions) + [
            f"DESK CHECK — {th.direction} downgraded to pass: "
            + "; ".join(problems)])
    return Thesis.model_validate(pinned)


def _arbiter(deps):
    def arbitrate(state):
        return vet_thesis(state, deps.arbiter(state), deps.last_price)
    return arbitrate


def _guarded(name, fn, key):
    """LLM nodes degrade honestly: an exception becomes an errors entry, and
    the arbitrate conditional routes a thesis-less run to record."""
    def node(state):
        try:
            return {key: fn(state)} if key != "reports" else {
                "reports": [fn(state)]}
        except Exception as e:                      # noqa: BLE001
            return {"errors": [f"{name}: {e}"]}
    return node


def _money_tail(g, deps):
    """gate → execute → record, identical in EVERY graph. This is the part
    where being one function matters: an approval fix that landed in the full
    pipeline but not the override graph would be a security bug, not a style
    problem. execute/record are deterministic and deliberately NOT guarded —
    money errors must fail loudly."""
    g.add_node("gate", approval_gate)
    g.add_node("execute", deps.execute)
    g.add_node("record", deps.record)
    g.add_conditional_edges(
        "gate",
        lambda s: {"approved": "execute", "rejected": "record",
                   "expired": "record"}[s.approval.status],
        ["execute", "record"])
    g.add_edge("execute", "record")
    g.add_edge("record", END)


def _arbitrate_or_record(g):
    """Only buy/sell verdicts face the gate. A 'pass' — including 'hold' on an
    exit review — completes the run without ever creating an approval."""
    g.add_conditional_edges(
        "arbitrate",
        lambda s: "gate" if s.thesis and s.thesis.direction in ("buy", "sell")
        else "record",
        ["gate", "record"])


def build_pipeline(deps: Deps, checkpointer):
    g = StateGraph(PipelineState)
    g.add_node("prepare", deps.prepare)
    for a in ANALYSTS:
        g.add_node(a, _guarded(a, deps.analysts[a], "reports"))
    g.add_node("bull", _guarded("bull", deps.bull, "bull"))
    g.add_node("bear", _guarded("bear", deps.bear, "bear"))
    g.add_node("arbitrate", _guarded("arbiter", _arbiter(deps), "thesis"))
    _money_tail(g, deps)

    g.add_edge(START, "prepare")
    for a in ANALYSTS:
        g.add_edge("prepare", a)                    # parallel fan-out
    g.add_edge(list(ANALYSTS), "bull")              # explicit barrier: ALL analysts
    g.add_edge("bull", "bear")                      # bear STRICTLY after bull
    g.add_edge("bear", "arbitrate")
    _arbitrate_or_record(g)
    return g.compile(checkpointer=checkpointer)


def build_sell_review_pipeline(deps: Deps, checkpointer):
    """An exit review: news → arbitrate → gate → execute → record.

    A stop-loss breach is not a fresh investment question, and re-running six
    agents while the position falls costs both money and minutes. The analysis
    that opened the position is attached instead (state.prior_evidence), and
    the only thing bought new is the one input that has actually changed since
    then: what happened. The arbiter weighs that against the original thesis
    and the breach — a 'pass' verdict here means HOLD — and the exit still
    needs the same human approval a buy does."""
    g = StateGraph(PipelineState)
    g.add_node("prepare", deps.prepare)
    g.add_node("news", _guarded("news", deps.analysts["news"], "reports"))
    g.add_node("arbitrate", _guarded("arbiter", _arbiter(deps), "thesis"))
    _money_tail(g, deps)

    g.add_edge(START, "prepare")
    g.add_edge("prepare", "news")
    g.add_edge("news", "arbitrate")
    _arbitrate_or_record(g)
    return g.compile(checkpointer=checkpointer)


def build_override_pipeline(deps: Deps, checkpointer):
    """The money half of the pipeline on its own: gate → execute → record.

    A PASS verdict routes arbitrate → record, so the graph RUNS TO COMPLETION
    and there is no interrupt left to resume — overriding one cannot mean
    reviving a finished run. Instead the override starts a fresh item that
    enters at the gate carrying the thesis the human authored, so approval,
    token burning and execution stay the code that is already proven. The
    analysis nodes are deliberately absent: an override re-uses the original
    run's evidence rather than paying to think again.

    `prepare` still runs first: it is what applies the venue's board lot
    (SGX: 100 shares). Skipping it sized an SGX override in fractions of a
    share (M9)."""
    g = StateGraph(PipelineState)
    g.add_node("prepare", deps.prepare)
    _money_tail(g, deps)
    g.add_edge(START, "prepare")
    g.add_edge("prepare", "gate")
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
    decision = dict(decision)
    levels = decision.pop("levels", None)
    out = {"approval": Approval(**decision)}
    if levels and out["approval"].status == "approved" and \
            state.thesis.direction in ("buy", "sell"):
        # the stop / target YOU set on the card replace the plan's (the
        # dashboard validated them; the Thesis validator checks them again)
        data = state.thesis.model_dump()
        for k in ("entry_low", "entry_high", "stop_loss", "take_profit"):
            if k in levels:
                data[k] = levels[k]
        out["thesis"] = Thesis.model_validate(data)
    return out
