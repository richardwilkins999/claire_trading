"""Approved trades whose placement was refused (2026-11-04: Saxo refused
9.6 shares of CSCO). They wait for the human — retried or cancelled from
the Approvals page — and are never re-sent in an automatic loop."""
import json

import pytest

from app import approvals
from app.accounting import db
from app.accounting.repo import Repo

T0 = 1_793_725_000


def _failed(conn, wi="wi_ovr", broker="saxo", ts=T0):
    Repo(conn).create_work_item(wi, "override", "CSCO", ts=ts)
    conn.execute("UPDATE work_items SET state='approved', approval_token='tok',"
                 " token_state='pending_resume', thesis_json=? WHERE id=?",
                 (json.dumps({"direction": "buy", "entry_high": 103.15,
                              "stop_loss": 96.97, "currency": "USD"}), wi))
    for to, payload in (
            ("authorize:approve", {"size_base": 1000, "qty": None,
                                   "trail_pct": 8, "broker": broker}),
            ("approved", {"size_base": 1000.0}),
            ("resume_failed", {"error": "broker rejected order: quantity 9.6"
                                        " is not a whole multiple"})):
        conn.execute("INSERT INTO events (item_id, ts, actor, from_state,"
                     " to_state, payload) VALUES (?,?,?,?,?,?)",
                     (wi, ts, "human", "approved", to, json.dumps(payload)))


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init(c)
    Repo(c).create_account("saxo-paper", "saxo", "paper", "USD",
                           fee_model={"type": "flat", "per_trade": "0"}, ts=T0)
    return c


def test_a_refused_trade_is_not_retried_automatically(conn):
    _failed(conn)
    sent = []
    assert approvals.retry_pending(
        conn, lambda b: sent.append(b) or (200, {}), clock=lambda: T0) == []
    assert sent == []


def test_crash_recovery_still_retries_an_unattempted_approval(conn):
    _failed(conn)
    conn.execute("DELETE FROM events WHERE to_state='resume_failed'")
    sent = []
    approvals.retry_pending(conn, lambda b: sent.append(b) or (200, {}),
                            clock=lambda: T0)
    assert [b["work_item_id"] for b in sent] == ["wi_ovr"]


def test_listing_shows_error_approval_and_time_left(conn):
    _failed(conn)
    [f] = approvals.failed_approvals(conn, clock=lambda: T0 + 600)
    assert f["ticker"] == "CSCO" and "9.6" in f["error"]
    assert f["approval"]["size_base"] == 1000 and f["approval"]["broker"] == "saxo"
    assert f["expires_in_s"] == 3000 and f["attempts"] == 1


def test_retry_resends_the_same_approval(conn):
    _failed(conn)
    sent = []
    out = approvals.retry_failed(conn, "wi_ovr",
                                 lambda b: sent.append(b) or (200, {"ok": True}))
    assert out["ok"]
    assert sent == [{"work_item_id": "wi_ovr", "status": "approved",
                     "token": "tok", "actor": "human", "size_base": 1000,
                     "qty": None, "trail_pct": 8, "broker": "saxo"}]


def test_retry_surfaces_a_second_refusal(conn):
    _failed(conn)
    with pytest.raises(approvals.AuthError, match="still refused"):
        approvals.retry_failed(conn, "wi_ovr", lambda b: (
            500, {"detail": "resume failed: still refused"}))


def test_live_retry_needs_armed_and_typed_live(conn, monkeypatch):
    _failed(conn, broker="saxo_live")
    monkeypatch.setattr("os.environ", {})
    with pytest.raises(approvals.AuthError, match="not armed"):
        approvals.retry_failed(conn, "wi_ovr", lambda b: (200, {}))


def test_cancel_rejects_and_burns_the_token(conn):
    _failed(conn)
    assert approvals.cancel_failed(conn, "wi_ovr", clock=lambda: T0)["ok"]
    row = conn.execute("SELECT state, token_state FROM work_items").fetchone()
    assert (row["state"], row["token_state"]) == ("rejected", "burned")
    with pytest.raises(approvals.AuthError):            # nothing left to retry
        approvals.retry_failed(conn, "wi_ovr", lambda b: (200, {}))


def test_only_failed_approvals_can_be_retried_or_cancelled(conn):
    Repo(conn).create_work_item("wi_x", "pipeline", "CSCO", ts=T0)
    conn.execute("UPDATE work_items SET state='awaiting_approval',"
                 " approval_token='t', token_state='minted'")
    for fn in (lambda: approvals.retry_failed(conn, "wi_x", lambda b: (200, {})),
               lambda: approvals.cancel_failed(conn, "wi_x")):
        with pytest.raises(approvals.AuthError, match="not a failed"):
            fn()


def test_transient_failures_are_still_retried_automatically(conn):
    _failed(conn)
    conn.execute("UPDATE events SET payload=? WHERE to_state='resume_failed'",
                 (json.dumps({"error": "Saxo POST: HTTP 503 service "
                                       "unavailable"}),))
    sent = []
    approvals.retry_pending(conn, lambda b: sent.append(b) or (200, {}),
                            clock=lambda: T0)
    assert len(sent) == 1
    assert approvals.failed_approvals(conn, clock=lambda: T0)[0]["auto_retry"]


@pytest.mark.parametrize("err,transient", [
    ("venue down", True), ("claire-api unreachable", True),
    ("broker rejected order: quantity 9.6 is not a whole multiple", False),
    ("live trading: live trading is disarmed", False),
    ("order needs 1000.00 USD but acct holds 10.00", False),
    ("something nobody anticipated", False),          # unknown → wait
])
def test_error_classification(err, transient):
    assert approvals.is_transient(err) is transient


def test_retry_can_change_size_account_and_floor(conn):
    _failed(conn)
    Repo(conn).create_account("p2", "alpaca", "paper", "USD",
                              fee_model={"type": "flat", "per_trade": "0"},
                              ts=T0)
    sent = []
    approvals.retry_failed(conn, "wi_ovr", lambda b: sent.append(b) or (200, {}),
                           changes={"size_base": 800, "trail_pct": 6,
                                    "broker": "alpaca", "qty": None})
    assert (sent[0]["size_base"], sent[0]["trail_pct"], sent[0]["broker"]) \
        == (800, 6, "alpaca")
    with pytest.raises(approvals.AuthError, match="no account"):
        approvals.retry_failed(conn, "wi_ovr", lambda b: (200, {}),
                               changes={"broker": "moomoo"})


def test_a_parked_refused_trade_does_not_degrade_the_custodian(conn):
    """2026-11-04: the parked STVN trade was flagged 'approved item has no
    order after 10m', marking the custodian degraded. It is not an orphan —
    it is waiting for the human on the Approvals page."""
    from app.accounting.money import to_micro
    from app.custodian import Custodian
    _failed(conn, ts=T0 - 3000)
    conn.execute("UPDATE work_items SET updated_at=?", (T0 - 3000,))
    c = Custodian(conn, Repo(conn), {}, resume_post=lambda b: (200, {}),
                  account_broker=lambda a: "saxo",
                  fx_rate_for=lambda i, a: to_micro("1"), clock=lambda: T0)
    report = {"flags": []}
    c._orphans(report)
    assert not [f for f in report["flags"] if "no order" in f]


def test_retry_can_buy_exact_shares_at_the_current_price(conn):
    # the Approvals retry panel sends the share count it shows and the
    # order type; both reach the resume (execution buys exactly qty)
    _failed(conn)
    sent = []
    approvals.retry_failed(conn, "wi_ovr", lambda b: sent.append(b) or (200, {}),
                           changes={"qty": 27, "size_base": 989.74,
                                    "order_type": "market", "trail_pct": 10})
    b = sent[0]
    assert (b["qty"], b["order_type"], b["trail_pct"]) == (27, "market", 10)
