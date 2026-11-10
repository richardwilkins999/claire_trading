"""Authorization tests (DESIGN.md §18 layer 3) — offline, sub-second.
Real graph + real API app + real dashboards authorization, fake LLM deps."""
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.sqlite import SqliteSaver

from app.accounting import db
from app.accounting.repo import Repo
from app.api.desk import Desk
from app.api.main import create_app
from app.approvals import AuthError, retry_pending, thesis_action
from tests.test_graph import NVDA, Script

SECRET = "test-internal-secret"
# Tue 2026-08-11 14:00 UTC = 10:00 New York — NASDAQ is open
CLOCK = lambda: 1_786_456_800  # noqa: E731


@pytest.fixture
def world():
    conn = db.connect(":memory:")
    db.init(conn)
    repo = Repo(conn)
    repo.create_account("alpaca-paper", "alpaca", "paper", "USD",
                        fee_model={"type": "flat", "per_trade": "0"},
                        ts=CLOCK())
    repo.deposit("alpaca-paper", 10_000_000_000, ts=CLOCK())  # $10k
    script = Script()
    desk = Desk(conn, script.deps,
                SqliteSaver(sqlite3.connect(":memory:", check_same_thread=False)),
                repo, clock=CLOCK)
    app = create_app(desk, conn, SECRET, clock=CLOCK)
    client = TestClient(app)

    def resume_post(body):
        r = client.post("/internal/resume", json=body,
                        headers={"x-claire-secret": SECRET})
        return r.status_code, r.json()

    wi = desk.start_run(NVDA, background=False)     # runs to the interrupt
    return conn, desk, client, resume_post, wi, script, app


def row(conn, wi):
    return conn.execute("SELECT * FROM work_items WHERE id=?", (wi,)).fetchone()


def token_of(conn, wi):
    return row(conn, wi)["approval_token"]


def approve_payload(conn, wi, **over):
    p = {"work_item_id": wi, "action": "approve", "size_base": 1000.0,
         "broker": "alpaca", "token": token_of(conn, wi)}
    p.update(over)
    return p


def test_interrupt_minted_token_and_session_expiry(world):
    conn, desk, client, resume_post, wi, script, app = world
    r = row(conn, wi)
    assert r["state"] == "awaiting_approval"
    assert r["token_state"] == "minted"
    assert len(r["approval_token"]) == 32
    # expiry is NASDAQ close (20:00 UTC that day), not now+24h
    assert r["expires_at"] == 1_786_478_400
    assert "execute" not in script.calls


def test_happy_path_approve(world):
    conn, desk, client, resume_post, wi, script, app = world
    out = thesis_action(conn, approve_payload(conn, wi), resume_post,
                        clock=CLOCK)
    assert out["ok"] is True
    assert "execute" in script.calls
    r = row(conn, wi)
    assert r["state"] == "executing"                # fills pending (custodian)
    assert r["token_state"] == "burned"


def test_reject_path(world):
    conn, desk, client, resume_post, wi, script, app = world
    thesis_action(conn, {"work_item_id": wi, "action": "reject",
                         "token": token_of(conn, wi)}, resume_post, clock=CLOCK)
    assert "execute" not in script.calls
    assert row(conn, wi)["state"] == "rejected"


def test_no_token_403_even_from_localhost(world):
    conn, desk, client, resume_post, wi, script, app = world
    with pytest.raises(AuthError) as e:
        thesis_action(conn, approve_payload(conn, wi, token=""), resume_post,
                      clock=CLOCK)
    assert e.value.code == 403
    assert row(conn, wi)["token_state"] == "minted"     # nothing consumed


def test_wrong_token_403(world):
    conn, desk, client, resume_post, wi, script, app = world
    with pytest.raises(AuthError) as e:
        thesis_action(conn, approve_payload(conn, wi, token="f" * 32),
                      resume_post, clock=CLOCK)
    assert e.value.code == 403


def test_reused_token_409(world):
    conn, desk, client, resume_post, wi, script, app = world
    thesis_action(conn, approve_payload(conn, wi), resume_post, clock=CLOCK)
    with pytest.raises(AuthError) as e:
        thesis_action(conn, approve_payload(conn, wi), resume_post, clock=CLOCK)
    assert e.value.code == 409


def test_expired_410(world):
    conn, desk, client, resume_post, wi, script, app = world
    late = lambda: 1_786_478_401                    # noqa: E731 — past close
    with pytest.raises(AuthError) as e:
        thesis_action(conn, approve_payload(conn, wi), resume_post, clock=late)
    assert e.value.code == 410


def test_resume_requires_secret_and_valid_token(world):
    conn, desk, client, resume_post, wi, script, app = world
    body = {"work_item_id": wi, "status": "approved", "actor": "human",
            "token": token_of(conn, wi), "size_base": 1000, "broker": "alpaca"}
    assert client.post("/internal/resume", json=body).status_code == 403
    assert client.post("/internal/resume", json=body,
                       headers={"x-claire-secret": "wrong"}).status_code == 403
    bad = dict(body, token="0" * 32)
    assert client.post("/internal/resume", json=bad,
                       headers={"x-claire-secret": SECRET}).status_code == 403
    assert "execute" not in script.calls


def test_resume_refuses_non_localhost(world):
    conn, desk, client, resume_post, wi, script, app = world
    import asyncio

    async def from_remote_host():
        transport = httpx.ASGITransport(app=app, client=("10.0.0.9", 4444))
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://t") as remote:
            return await remote.post(
                "/internal/resume",
                json={"work_item_id": wi, "status": "approved",
                      "actor": "human", "token": token_of(conn, wi)},
                headers={"x-claire-secret": SECRET})
    assert asyncio.run(from_remote_host()).status_code == 403


def test_reaper_expiry_terminates_cleanly(world):
    conn, desk, client, resume_post, wi, script, app = world
    code, resp = resume_post({"work_item_id": wi, "status": "expired",
                              "actor": "reaper", "token": token_of(conn, wi)})
    assert code == 200
    assert row(conn, wi)["state"] == "expired"
    assert "execute" not in script.calls
    assert script.calls[-1] == "record"             # graph finished, no leak
    # and only the reaper may expire
    code, _ = resume_post({"work_item_id": wi, "status": "expired",
                           "actor": "human", "token": token_of(conn, wi)})
    assert code == 403


def test_crash_between_authorize_and_resume_retries_burns_once(world):
    conn, desk, client, resume_post, wi, script, app = world

    def down(body):
        raise ConnectionError("claire-api restarting")
    with pytest.raises(AuthError) as e:
        thesis_action(conn, approve_payload(conn, wi), down, clock=CLOCK)
    assert e.value.code == 502
    assert row(conn, wi)["token_state"] == "pending_resume"  # saved, not lost
    assert "execute" not in script.calls

    done = retry_pending(conn, resume_post, clock=CLOCK)     # custodian path
    assert done == [wi]
    r = row(conn, wi)
    assert r["token_state"] == "burned"
    assert r["state"] == "executing"
    assert script.calls.count("execute") == 1
    # a second retry is a no-op (idempotent ack), token burned exactly once
    assert retry_pending(conn, resume_post, clock=CLOCK) == []


def test_resume_idempotent_replay(world):
    conn, desk, client, resume_post, wi, script, app = world
    tok = token_of(conn, wi)
    thesis_action(conn, approve_payload(conn, wi), resume_post, clock=CLOCK)
    code, resp = resume_post({"work_item_id": wi, "status": "approved",
                              "actor": "human", "token": tok,
                              "size_base": 1000, "broker": "alpaca"})
    assert (code, resp.get("idempotent")) == (200, True)
    assert script.calls.count("execute") == 1


# ── stop / target set on the approval card (2026-11-06, PG) ─────────────
def graph_thesis(desk, wi):
    return desk._pipeline_for(wi).get_state(desk.cfg(wi)).values["thesis"]


def test_card_stop_and_target_replace_the_plan(world):
    conn, desk, client, resume_post, wi, script, app = world
    thesis_action(conn, approve_payload(
        conn, wi, levels={"stop_loss": 165.0, "take_profit": None}),
        resume_post, clock=CLOCK)
    th = graph_thesis(desk, wi)
    assert th.stop_loss == 165.0 and th.take_profit is None   # execution's view
    import json
    assert json.loads(row(conn, wi)["thesis_json"])["stop_loss"] == 165.0
    ev = conn.execute("SELECT payload FROM events WHERE item_id=? AND"
                      " to_state='authorize:approve'", (wi,)).fetchone()
    assert json.loads(ev[0])["levels"] == {"stop_loss": 165.0,
                                           "take_profit": None}


def test_unchanged_levels_keep_the_plan(world):
    conn, desk, client, resume_post, wi, script, app = world
    thesis_action(conn, approve_payload(
        conn, wi, levels={"stop_loss": 160.0, "take_profit": 200.0}),
        resume_post, clock=CLOCK)
    th = graph_thesis(desk, wi)
    assert (th.stop_loss, th.take_profit) == (160.0, 200.0)


@pytest.mark.parametrize("levels, why", [
    ({"stop_loss": None, "take_profit": 200.0}, "needs a stop"),
    ({"stop_loss": 180.0}, "below the entry"),
    ({"stop_loss": 160.0, "take_profit": 170.0}, "above the entry"),
])
def test_bad_card_levels_are_refused_before_anything_resumes(world, levels,
                                                             why):
    conn, desk, client, resume_post, wi, script, app = world
    with pytest.raises(AuthError, match=why):
        thesis_action(conn, approve_payload(conn, wi, levels=levels),
                      resume_post, clock=CLOCK)
    assert row(conn, wi)["state"] == "awaiting_approval"
    assert "execute" not in script.calls


def test_saxo_25_percent_rule_on_card_levels(world):
    conn, desk, client, resume_post, wi, script, app = world
    from app.approvals import _card_levels
    r = row(conn, wi)
    with pytest.raises(AuthError, match="25%"):
        _card_levels(r, {"broker": "saxo",
                         "levels": {"stop_loss": 120.0}})   # 31% below 175
    assert _card_levels(r, {"broker": "alpaca",
                            "levels": {"stop_loss": 120.0}}) is not None


def test_order_type_reaches_the_graph(world):
    conn, desk, client, resume_post, wi, script, app = world
    thesis_action(conn, approve_payload(conn, wi, order_type="market"),
                  resume_post, clock=CLOCK)
    ap = desk._pipeline_for(wi).get_state(desk.cfg(wi)).values["approval"]
    assert ap.order_type == "market"


def test_an_unknown_order_type_is_refused(world):
    conn, desk, client, resume_post, wi, script, app = world
    with pytest.raises(AuthError, match="order_type"):
        thesis_action(conn, approve_payload(conn, wi, order_type="stop"),
                      resume_post, clock=CLOCK)


# ── a limit price you type on the card (manual order, 2026-11-10) ──────
def test_card_limit_price_replaces_the_entry(world):
    conn, desk, client, resume_post, wi, script, app = world
    thesis_action(conn, approve_payload(
        conn, wi, levels={"entry": 172.5, "stop_loss": 160.0,
                          "take_profit": 200.0}), resume_post, clock=CLOCK)
    th = graph_thesis(desk, wi)
    assert (th.entry_low, th.entry_high) == (172.5, 172.5)
    assert th.stop_loss == 160.0


@pytest.mark.parametrize("levels, why", [
    ({"entry": 159.0, "stop_loss": 160.0}, "below the entry"),
    ({"entry": -1, "stop_loss": 160.0}, "more than zero"),
    ({"entry": "abc", "stop_loss": 160.0}, "must be a number"),
])
def test_bad_card_limit_price_is_refused(world, levels, why):
    conn, desk, client, resume_post, wi, script, app = world
    with pytest.raises(AuthError, match=why):
        thesis_action(conn, approve_payload(conn, wi, levels=levels),
                      resume_post, clock=CLOCK)
    assert row(conn, wi)["state"] == "awaiting_approval"
