"""The exit review runs news → arbitrate → gate, not the full six.

A stop breach is not a fresh investment question. Re-running every analyst
while the position falls costs ~$1.27 and minutes; the analysis that opened
the position is attached instead, and the only new input bought is the one
thing that has actually changed — what happened.
"""
import json

import pytest

from app.accounting import db
from app.accounting.repo import Repo
from app.graph.nodes_analysis import arbiter_prompt
from app.graph.state import (AnalystReport, Instrument, PipelineState, Thesis)
from datetime import datetime, timezone

T0 = 1_786_456_800
AT = "2026-08-12T10:00:00Z"


def _desk(verdict="sell"):
    from langgraph.checkpoint.memory import InMemorySaver
    from app.api.desk import Desk
    from app.graph.pipeline import ANALYSTS, Deps

    conn = db.connect(":memory:")
    db.init(conn)
    repo = Repo(conn)
    conn.execute("INSERT INTO instruments (id,ticker,exchange,currency)"
                 " VALUES ('NASDAQ:SBUX','SBUX','NASDAQ','USD')")
    called = []

    def analyst(agent):
        def fn(state):
            called.append(agent)
            return AnalystReport(
                ticker="SBUX", agent=agent, signal="bearish", conviction=0.6,
                summary="sector-wide selloff, no company news",
                data_asof=datetime.now(timezone.utc))
        return fn

    def arbiter(state):
        called.append("arbiter")
        called.append(("prompt", arbiter_prompt(state)))
        if verdict == "pass":
            return Thesis(ticker="SBUX", direction="pass", conviction=0.4,
                          currency="USD", conditions=["noise, hold"])
        return Thesis(ticker="SBUX", direction="sell", conviction=0.7,
                      currency="USD", entry_low=96.0, entry_high=96.0,
                      stop_loss=101.0)

    def blow_up(state):
        raise AssertionError("an exit review must not run this agent")

    deps = Deps(analysts={a: (analyst(a) if a == "news" else blow_up)
                          for a in ANALYSTS},
                bull=blow_up, bear=blow_up, arbiter=arbiter,
                execute=lambda s: {"order_ids": ["ord_1"]},
                record=lambda s: {})
    desk = Desk(conn, deps, InMemorySaver(), repo, clock=lambda: T0)

    # the run that opened the position, with its evidence on record
    repo.create_work_item("wi_buy", "pipeline", "SBUX", ts=T0)
    thesis = {"ticker": "SBUX", "direction": "buy", "conviction": 0.7,
              "currency": "USD", "entry_low": 106.0, "entry_high": 106.0,
              "stop_loss": 98.0, "conditions": ["turnaround must show volume"]}
    conn.execute("UPDATE work_items SET thesis_json=?, state='done'"
                 " WHERE id='wi_buy'", (json.dumps(thesis),))
    for agent, payload in (
            ("fundamental", {"agent": "fundamental", "signal": "bullish",
                             "conviction": 0.6, "summary": "cheap on FCF",
                             "key_findings": ["FCF yield 5.1%"]}),
            ("arbiter", thesis)):
        conn.execute("INSERT INTO agent_reports (work_item_id, agent_id, kind,"
                     " payload, created_at) VALUES ('wi_buy',?,?,?,?)",
                     (agent, "thesis" if agent == "arbiter" else "analyst",
                      json.dumps(payload), T0))
    return conn, repo, desk, called


def _run(desk):
    inst = Instrument(id="NASDAQ:SBUX", ticker="SBUX", exchange="NASDAQ",
                      currency="USD")
    return desk.start_run(inst, "sell_review", background=False,
                          trigger="watcher: trailing stop breached — last "
                                  "96.00, -9.4% from the best price")


# ── only news runs, and the arbiter gets the entry analysis ────────────────
def test_only_news_and_the_arbiter_run():
    conn, _, desk, called = _desk()
    _run(desk)
    agents = [c for c in called if isinstance(c, str)]
    assert agents == ["news", "arbiter"]        # blow_up guards the rest


def test_the_arbiter_is_handed_what_we_believed_at_entry():
    conn, _, desk, called = _desk()
    wi = _run(desk)
    prompt = next(c[1] for c in called if isinstance(c, tuple))
    assert "EXIT REVIEW" in prompt
    assert "Verdict at entry: BUY" in prompt
    assert "FCF yield 5.1%" in prompt          # the original evidence
    assert "turnaround must show volume" in prompt
    assert "trailing stop breached" in prompt   # and why it is being asked
    # holding must be offered as an answer, or a breach reads as an order
    assert "'pass' to HOLD" in prompt
    assert conn.execute("SELECT prior_run FROM work_items WHERE id=?",
                        (wi,)).fetchone()["prior_run"] == "wi_buy"


def test_a_review_with_no_earlier_run_still_works():
    """A position can predate the desk. It must degrade, not crash."""
    conn, _, desk, called = _desk()
    conn.execute("DELETE FROM agent_reports WHERE work_item_id='wi_buy'")
    conn.execute("DELETE FROM events WHERE item_id='wi_buy'")
    conn.execute("DELETE FROM work_items WHERE id='wi_buy'")
    _run(desk)
    prompt = next(c[1] for c in called if isinstance(c, tuple))
    assert "no earlier analysis on record" in prompt


# ── the exit still needs a human ───────────────────────────────────────────
def test_a_sell_verdict_stops_at_the_gate():
    conn, _, desk, _ = _desk(verdict="sell")
    wi = _run(desk)
    row = conn.execute("SELECT state, approval_token FROM work_items"
                       " WHERE id=?", (wi,)).fetchone()
    assert row["state"] == "awaiting_approval"
    assert row["approval_token"]
    assert conn.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"] == 0
    # and approving it drives the same execution path
    desk.resume(wi, {"status": "approved", "qty": 9.3, "size_base": None,
                     "trail_pct": None, "broker": "alpaca", "actor": "human",
                     "token": row["approval_token"], "at": AT})
    assert conn.execute("SELECT state FROM work_items WHERE id=?",
                        (wi,)).fetchone()["state"] == "executing"


def test_a_hold_verdict_never_reaches_the_gate():
    """The desk looked and decided not to exit — that is a decision, and it
    must not put a sell card in front of the human."""
    conn, _, desk, _ = _desk(verdict="pass")
    wi = _run(desk)
    row = conn.execute("SELECT state FROM work_items WHERE id=?",
                       (wi,)).fetchone()
    assert row["state"] == "done"


# ── the card carries both sets of evidence ─────────────────────────────────
def test_the_approval_card_shows_the_entry_analysis_too():
    import threading
    import httpx
    from app.dashboards import create_server
    conn, repo, desk, _ = _desk(verdict="sell")
    repo.create_account("a", "alpaca", "paper", "USD",
                        fee_model={"type": "flat", "per_trade": "0"}, ts=T0)
    wi = _run(desk)
    # the review's own news report, as the node would have stored it
    conn.execute("INSERT INTO agent_reports (work_item_id, agent_id, kind,"
                 " payload, created_at) VALUES (?,'news','analyst',?,?)",
                 (wi, json.dumps({"agent": "news", "signal": "bearish",
                                  "summary": "sector selloff"}), T0))

    class M:
        _stale_keys = set()

        def metrics(self, s):
            return {}
    srv = create_server(conn, repo, M(), clock=lambda: T0, port=0,
                        sse_interval=None, dash_key="k")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        d = httpx.get(f"http://127.0.0.1:{srv.server_address[1]}/api/approvals",
                      headers={"X-Dash-Key": "k"}, timeout=5).json()
        card = [c for c in d["cards"] if c["id"] == wi][0]
        agents = [r["agent"] for r in card["reports"]]
        assert "fundamental" in agents      # inherited from the buy
        assert "news" in agents             # and this review's own
        assert card["prior_run"] == "wi_buy"
    finally:
        srv.shutdown()
