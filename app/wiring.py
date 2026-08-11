"""Production composition root: registry-backed LLM nodes + deterministic
execution wired into the pipeline Deps (DESIGN.md §2). Tests never import
this; they inject fakes straight into Deps.
"""
import os
import time

from .graph.nodes_analysis import (default_structured_factory, make_analyst,
                                   make_debater)
from .graph.nodes_execution import build_execute, build_record
from .graph.pipeline import Deps
from .tools.brokers import PaperSimBroker
from .tools.lc_tools import build_tool_registry


def account_for_broker(conn):
    def account_for(broker_name):
        row = conn.execute(
            "SELECT id FROM broker_accounts WHERE broker=? LIMIT 1",
            (broker_name,)).fetchone()
        if row is None:
            raise LookupError(f"no account for broker {broker_name!r}")
        return row["id"]
    return account_for


def build_brokers(env=os.environ):
    """The sim venue is always available; real MCP venues activate only when
    their credentials exist (honest degradation, §1.7)."""
    brokers = {"alpaca": PaperSimBroker(fill_mode="instant"),
               "saxo": PaperSimBroker(fill_mode="instant"),
               "moomoo": PaperSimBroker(fill_mode="instant")}
    # TODO(step 12+): swap PaperSimBroker for MCP adapters per venue when
    # ALPACA_PAPER_KEY / SAXO_SIM_TOKEN / OpenD are present (tools/brokers.py
    # MCP_BROKERS carries the launch config).
    return brokers


def make_prepare(repo):
    """Prepare owns instrument resolution: currency from the exchange suffix
    (GBp→GBP), venue lot size, and the desk.db registration — callers only
    need ticker + exchange, and every entry point gets identical results."""
    from .tools.market import currency_for, yahoo_symbol

    def prepare(state):
        inst = state.instrument
        ccy = currency_for(yahoo_symbol(inst.ticker, inst.exchange))
        resolved = inst.model_copy(update={
            "currency": "GBP" if ccy == "GBp" else ccy,
            "lot_size": max(inst.lot_size, 100 if inst.exchange == "SGX"
                            else 1)})
        repo.add_instrument(resolved.id, resolved.ticker, resolved.exchange,
                            resolved.currency, resolved.name)
        return {"instrument": resolved}
    return prepare


def build_deps(conn, repo, narratives, market, *, brokers=None,
               clock=time.time, cal=None, env=os.environ) -> Deps:
    structured = default_structured_factory(conn, env=env)
    tool_builder = build_tool_registry(market, narratives, env=env)
    brokers = brokers or build_brokers(env)
    prepare = make_prepare(repo)

    analysts = {a: make_analyst(conn, a, narratives, tool_builder,
                                structured_factory=structured, env=env)
                for a in ("fundamental", "technical", "news")}
    return Deps(
        analysts=analysts,
        bull=make_debater(conn, "bull", narratives,
                          structured_factory=structured),
        bear=make_debater(conn, "bear", narratives,
                          structured_factory=structured),
        arbiter=make_debater(conn, "arbiter", narratives,
                             structured_factory=structured),
        execute=build_execute(repo, brokers, market,
                              account_for=account_for_broker(conn),
                              clock=clock, cal=cal),
        record=build_record(repo, narratives, clock=clock),
        prepare=prepare,
    )
