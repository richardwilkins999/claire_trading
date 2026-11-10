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


def build_brokers(env=os.environ, conn=None, *, sim_store=False):
    """The in-process sim is the default venue for every paper account.
    Real Saxo venues activate only when explicitly configured:

    - saxo (SIM): SAXO_SIM_EXECUTION=enabled + SAXO_SIM_TOKEN +
      SAXO_SIM_ACCOUNT_KEY route the saxo-paper account to Saxo's SIM
      environment — real Saxo order handling, simulated money.
    - saxo_live (REAL MONEY): only when CLAIRE_LIVE_TRADING=enabled AND a
      live account exists (`bin/claire live setup`). Even then every
      placement passes the live switch (DESIGN.md §22) in submit_order, and
      the adapter re-checks that the desk is armed as a second lock.

    The built-in sims hold a bracket's stop-loss / take-profit as resting
    orders (protect=True) that the custodian fills when the live price
    reaches them. `sim_store=True` (claire-api, which owns the sims) keeps
    them in desk.db so they survive a restart; other callers only read."""
    store = conn if sim_store and conn is not None else None

    def sim(venue):
        return PaperSimBroker(fill_mode="instant", protect=True, store=store,
                              venue=venue)
    brokers = {"alpaca": sim("alpaca"), "saxo": sim("saxo"),
               "moomoo": sim("moomoo")}
    from .tools.saxo_broker import SaxoBroker, SaxoSimWithLocalMarkets
    if (env.get("SAXO_SIM_EXECUTION") or "").lower() == "enabled" and \
            env.get("SAXO_SIM_ACCOUNT_KEY"):
        token = sim_token_source(env)
        if token is not None:
            # SGX shares are not offered at Saxo SIM (CFDs only): those
            # orders are simulated in-process; everything else is Saxo's
            brokers["saxo"] = SaxoSimWithLocalMarkets(
                SaxoBroker("sim", token=token,
                           account_key=env["SAXO_SIM_ACCOUNT_KEY"]),
                sim("saxo-local"))
    from . import live_trading
    if conn is not None and live_trading.env_enabled(env):
        acct = conn.execute("SELECT * FROM broker_accounts WHERE"
                            " broker='saxo_live' AND environment='live'"
                            ).fetchone()
        from .tools import saxo_live
        # bound to the LIVE sign-in only: a SIM token can never drive it
        if acct is not None and acct["external_ref"]:

            def armed_guard():
                st = live_trading.status(conn, env)
                if not st["armed"]:
                    raise live_trading.LiveTradingBlocked(st["summary"])
            brokers["saxo_live"] = SaxoBroker(
                "live", token=saxo_live.token_source("live"),
                account_key=acct["external_ref"], guard=armed_guard)
    return brokers


def sim_token_source(env=os.environ):
    """Where Saxo SIM trading gets its login, best first:

    1. the SIM OAuth sign-in (`bin/saxo login sim`) — renewed by
       claire-api's keepalive, alerts on Telegram when it lapses, and
       exercises exactly the path live trading uses;
    2. a pasted 24-hour developer-portal token (SAXO_SIM_TOKEN) — legacy,
       dies after a day with no renewal.

    Returns a () -> token callable, or None when neither is available. It is
    always bound to SIM: the live sign-in can never serve SIM trading."""
    from .tools import saxo_live
    if saxo_live.token_file("sim").exists():
        return saxo_live.token_source("sim")
    if env.get("SAXO_SIM_TOKEN"):
        return lambda: env["SAXO_SIM_TOKEN"]
    return None


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
        record=build_record(repo, narratives, clock=clock,
                            prose_fn=_recorder_prose(conn, env)),
        prepare=prepare,
        last_price=_last_price(market),
    )


def _last_price(market):
    """The gate's price-band check (review H5): an arbiter's entry far from
    where the stock actually trades never reaches the approval card. A
    failing lookup raises, which the gate treats as "skip here" — execute
    re-checks against the live quote and fails closed."""
    from .tools.market import yahoo_symbol

    def last_price(inst):
        return market.quote(yahoo_symbol(inst.ticker, inst.exchange))["price"]
    return last_price


def _recorder_prose(conn, env):
    def prose(summary_md: str) -> str:
        from .providers import registry
        llm = registry.model_for(conn, "recorder", env=env)
        out = llm.invoke([("system", registry.agent_row(
            conn, "recorder")["system_prompt"]),
            ("user", f"Run record:\n{summary_md[:4000]}")])
        text = out.content if isinstance(out.content, str) else str(out.content)
        return text[:600]
    return prose
