"""Overturning a PASS verdict (Approvals page).

The desk declining is a decision, so overriding it goes through the SAME
money path as any approval: a gate, a minted token, a cash check. What the
human adds is the thing a pass verdict never carries — an entry and a floor.
"""
import json

import pytest

from app.accounting import db
from app.accounting.repo import Repo
from app.graph.state import Instrument, PipelineState, Thesis

T0 = 1_786_456_800
AT = "2026-08-12T10:00:00Z"


def _desk(tmp_path, thesis_direction="pass", state="done"):
    """A desk whose only run has already concluded with the given verdict."""
    from langgraph.checkpoint.memory import InMemorySaver
    from app.api.desk import Desk

    conn = db.connect(":memory:")
    db.init(conn)
    repo = Repo(conn)
    conn.execute("INSERT INTO instruments (id, ticker, exchange, currency)"
                 " VALUES ('NASDAQ:SBUX','SBUX','NASDAQ','USD')")

    from app.graph.pipeline import ANALYSTS, Deps
    # only the money half is exercised — an override never re-analyses
    deps = Deps(analysts={a: (lambda s: None) for a in ANALYSTS},
                bull=lambda s: None, bear=lambda s: None,
                arbiter=lambda s: None,
                execute=lambda s: {"order_ids": ["ord_1"]},
                record=lambda s: {})
    desk = Desk(conn, deps, InMemorySaver(), repo, clock=lambda: T0)

    repo.create_work_item("wi_pass", "pipeline", "SBUX", ts=T0)
    t = {"ticker": "SBUX", "direction": thesis_direction, "conviction": 0.28,
         "currency": "USD", "conditions": ["wait for a full-volume session"]}
    if thesis_direction != "pass":
        t.update(entry_low=100.0, entry_high=100.0, stop_loss=90.0)
    conn.execute("UPDATE work_items SET thesis_json=?, state=? WHERE id=?",
                 (json.dumps(t), state, "wi_pass"))
    conn.execute("INSERT INTO agent_reports (work_item_id, agent_id, kind,"
                 " payload, created_at) VALUES ('wi_pass','arbiter','thesis',"
                 " ?, ?)", (json.dumps(t), T0))
    return conn, repo, desk


# ── the override reaches the gate, and only the gate ────────────────────────
def test_override_sends_a_pass_to_the_approval_gate(tmp_path):
    conn, repo, desk = _desk(tmp_path)
    wi = desk.override("wi_pass", direction="buy", entry=106.0, stop=98.0,
                       target=120.0, note="turnaround is real",
                       background=False)
    row = conn.execute("SELECT * FROM work_items WHERE id=?", (wi,)).fetchone()
    assert row["state"] == "awaiting_approval"     # in front of the human
    assert row["approval_token"] and row["token_state"] == "minted"
    assert row["override_of"] == "wi_pass"
    assert row["kind"] == "override"
    t = json.loads(row["thesis_json"])
    assert t["direction"] == "buy" and t["entry_low"] == 106.0
    assert t["stop_loss"] == 98.0 and t["take_profit"] == 120.0
    # the original caveats survive and the override is stated in the thesis
    assert "wait for a full-volume session" in t["conditions"]
    assert any("HUMAN OVERRIDE" in c and "turnaround is real" in c
               for c in t["conditions"])
    # nothing was bought by overriding
    assert conn.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"] == 0
    # and it is on the record as a human act, not a system one
    ev = conn.execute("SELECT actor, to_state, payload FROM events WHERE"
                      " item_id=? AND to_state='override'", (wi,)).fetchone()
    assert ev["actor"] == "human"
    assert json.loads(ev["payload"])["origin"] == "wi_pass"


def test_the_original_verdict_is_untouched(tmp_path):
    conn, _, desk = _desk(tmp_path)
    before = dict(conn.execute("SELECT * FROM work_items WHERE id='wi_pass'"
                               ).fetchone())
    desk.override("wi_pass", direction="buy", entry=106.0, stop=98.0,
                  background=False)
    after = dict(conn.execute("SELECT * FROM work_items WHERE id='wi_pass'"
                              ).fetchone())
    assert after["state"] == before["state"] == "done"
    assert after["thesis_json"] == before["thesis_json"]


# ── the guards ──────────────────────────────────────────────────────────────
def test_a_stop_that_is_not_a_floor_is_refused(tmp_path):
    conn, _, desk = _desk(tmp_path)
    with pytest.raises(Exception) as e:            # Thesis validator
        desk.override("wi_pass", direction="buy", entry=100.0, stop=110.0,
                      background=False)
    assert "stop" in str(e.value).lower()
    with pytest.raises(Exception):
        desk.override("wi_pass", direction="buy", entry=100.0, stop=None,
                      background=False)
    assert conn.execute("SELECT COUNT(*) c FROM work_items WHERE"
                        " override_of IS NOT NULL").fetchone()["c"] == 0


def test_only_a_pass_can_be_overridden(tmp_path):
    _, _, desk = _desk(tmp_path, thesis_direction="buy")
    with pytest.raises(ValueError, match="only a pass"):
        desk.override("wi_pass", direction="buy", entry=106.0, stop=98.0,
                      background=False)


def test_a_running_item_cannot_be_overridden(tmp_path):
    _, _, desk = _desk(tmp_path, state="running")
    with pytest.raises(ValueError, match="still running"):
        desk.override("wi_pass", direction="buy", entry=106.0, stop=98.0,
                      background=False)


def test_one_live_override_per_verdict(tmp_path):
    _, _, desk = _desk(tmp_path)
    desk.override("wi_pass", direction="buy", entry=106.0, stop=98.0,
                  background=False)
    with pytest.raises(ValueError, match="already overridden"):
        desk.override("wi_pass", direction="buy", entry=107.0, stop=99.0,
                      background=False)


def test_unknown_item_is_refused(tmp_path):
    _, _, desk = _desk(tmp_path)
    with pytest.raises(ValueError, match="unknown"):
        desk.override("nope", direction="buy", entry=1.0, stop=0.5,
                      background=False)


# ── the override still has to be approved to become a position ─────────────
def test_approving_an_override_executes_through_the_normal_path(tmp_path):
    conn, _, desk = _desk(tmp_path)
    wi = desk.override("wi_pass", direction="buy", entry=106.0, stop=98.0,
                       background=False)
    row = conn.execute("SELECT * FROM work_items WHERE id=?", (wi,)).fetchone()
    desk.resume(wi, {"status": "approved", "size_base": 1000.0, "qty": None,
                     "trail_pct": 8.0, "broker": "alpaca", "actor": "human",
                     "token": row["approval_token"], "at": AT})
    after = conn.execute("SELECT state FROM work_items WHERE id=?",
                         (wi,)).fetchone()
    assert after["state"] == "executing"        # the gate-only graph executed
    # rejecting instead leaves nothing behind
    wi2_conn, _, desk2 = _desk(tmp_path)
    wi2 = desk2.override("wi_pass", direction="buy", entry=106.0, stop=98.0,
                         background=False)
    r2 = wi2_conn.execute("SELECT approval_token FROM work_items WHERE id=?",
                          (wi2,)).fetchone()
    desk2.resume(wi2, {"status": "rejected", "actor": "human",
                       "token": r2["approval_token"], "at": AT})
    assert wi2_conn.execute("SELECT state FROM work_items WHERE id=?",
                            (wi2,)).fetchone()["state"] == "rejected"


# ── the page shows the evidence on both halves ─────────────────────────────
def test_decided_runs_carry_full_agent_evidence(tmp_path):
    import threading
    import httpx
    from app.dashboards import create_server
    conn, repo, desk = _desk(tmp_path)
    repo.create_account("a", "alpaca", "paper", "USD",
                        fee_model={"type": "flat", "per_trade": "0"}, ts=T0)
    conn.execute("UPDATE work_items SET updated_at=? WHERE id='wi_pass'",
                 (T0,))

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
        row = d["decided"][0]
        assert row["direction"] == "pass"
        assert row["reports"][0]["agent"] == "arbiter"   # evidence is there
        assert row["overridable"] is True
        assert row["overridden_by"] is None

        # once overridden, the offer is withdrawn and the card inherits the
        # evidence of the verdict it overturned
        wi = desk.override("wi_pass", direction="buy", entry=106.0, stop=98.0,
                           background=False)
        d = httpx.get(f"http://127.0.0.1:{srv.server_address[1]}/api/approvals",
                      headers={"X-Dash-Key": "k"}, timeout=5).json()
        assert d["decided"][0]["overridable"] is False
        assert d["decided"][0]["overridden_by"] == wi
        card = [c for c in d["cards"] if c["id"] == wi][0]
        assert card["override_of"] == "wi_pass"
        assert card["reports"][0]["agent"] == "arbiter"
    finally:
        srv.shutdown()
