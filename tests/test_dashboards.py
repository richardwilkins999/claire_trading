"""Dashboards server tests — real HTTP against the stdlib server, offline
(fake market, no claire-api: the approve path 502s but authorization rules
still enforce)."""
import json
import threading
from decimal import Decimal

import httpx
import pytest

from app.accounting import db
from app.accounting.money import to_micro
from app.accounting.repo import Repo
from app.dashboards import create_server

T0 = 1_786_456_800


class FakeMarket:
    def spark(self, symbols):
        return {s: {"symbol": s, "price": Decimal("101"), "currency": "USD",
                    "stale": False, "previous_close": 100} for s in symbols}

    def search(self, q):
        return [{"symbol": "NVDA", "name": "NVIDIA", "exchange": "NASDAQ"}]

    def chart(self, symbol, range_="6mo", interval="1d"):
        return {"symbol": symbol, "close": [1, 2, 3], "currency": "USD"}


@pytest.fixture
def web():
    conn = db.connect(":memory:")
    db.init(conn)
    repo = Repo(conn)
    repo.create_account("acct", "alpaca", "paper", "USD",
                        fee_model={"type": "flat", "per_trade": "0"}, ts=T0)
    repo.deposit("acct", to_micro("1000"), ts=T0)
    conn.execute(
        "INSERT INTO work_items (id, kind, ticker, state, thread_id,"
        " approval_token, token_state, expires_at, thesis_json, created_at,"
        " updated_at) VALUES ('wi_1', 'pipeline', 'NVDA', 'awaiting_approval',"
        " 'wi_1', 'tok_abc', 'minted', ?, ?, ?, ?)",
        (T0 + 3600, json.dumps({"ticker": "NVDA", "direction": "buy",
                                "conviction": 0.7, "entry_low": 99,
                                "entry_high": 101, "stop_loss": 95,
                                "currency": "USD"}), T0, T0))
    srv = create_server(conn, repo, FakeMarket(), secret="s",
                        api_base="http://127.0.0.1:9",  # discard port: always down
                        clock=lambda: T0, port=0)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}", conn
    srv.shutdown()


def test_pages_and_apis_serve(web):
    base, conn = web
    with httpx.Client(base_url=base, timeout=5) as c:
        for page in ("/", "/portfolio", "/markets", "/trading", "/agents",
                     "/providers"):
            assert c.get(page).status_code == 200, page
        ov = c.get("/api/overview").json()
        assert ov["pending"] == 1
        assert ov["cash"][0]["cash"] == 1000
        cards = c.get("/api/approvals").json()
        assert cards[0]["token"] == "tok_abc"       # the card carries the token
        runs = c.get("/api/runs").json()
        assert "approval_token" not in runs[0]      # …and ONLY the card
        assert c.get("/api/quote?symbols=NVDA").json()["NVDA"]["price"] == "101"


def test_thesis_action_requires_token_over_http(web):
    base, conn = web
    with httpx.Client(base_url=base, timeout=5) as c:
        r = c.post("/api/thesis-action",
                   json={"work_item_id": "wi_1", "action": "approve",
                         "size_base": 500, "broker": "alpaca"})
        assert r.status_code == 403                 # no token → no approval
        r = c.post("/api/thesis-action",
                   json={"work_item_id": "wi_1", "action": "approve",
                         "size_base": 500, "broker": "alpaca",
                         "token": "wrong"})
        assert r.status_code == 403
        # right token but claire-api is down: authorized, saved, 502 surfaced
        r = c.post("/api/thesis-action",
                   json={"work_item_id": "wi_1", "action": "approve",
                         "size_base": 500, "broker": "alpaca",
                         "token": "tok_abc"})
        assert r.status_code == 502
        row = conn.execute("SELECT token_state FROM work_items"
                           " WHERE id='wi_1'").fetchone()
        assert row["token_state"] == "pending_resume"
