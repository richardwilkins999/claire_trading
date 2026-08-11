"""Tests for the audit-gap closures: views, splits, drift, archive,
UNPROTECTED flag, max_position_pct, holidays, chat persistence, health gate,
and the Twelve Data provider + Market source chain."""
import json
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.accounting import db
from app.accounting.money import to_micro
from app.accounting.repo import Repo, RiskLimitExceeded
from app.providers import health, registry, seeds
from app.tools.market import Market
from app.tools.twelvedata import Client as TDClient

T0 = 1_786_456_800


@pytest.fixture
def repo():
    conn = db.connect(":memory:")
    db.init(conn)
    r = Repo(conn)
    r.create_account("acct", "alpaca", "paper", "USD",
                     fee_model={"type": "flat", "per_trade": "0"}, ts=T0)
    r.add_instrument("NASDAQ:NVDA", "NVDA", "NASDAQ", "USD")
    r.deposit("acct", to_micro("10000"), ts=T0)
    return r


def fill(r, oid, side, qty, price, stop=None, ts=T0):
    r.create_order(id=oid, account_id="acct", instrument_id="NASDAQ:NVDA",
                   side=side, qty=to_micro(qty), stop_loss=stop,
                   expires_at=ts + 86400, ts=ts)
    r.record_fill(oid, broker_fill_id=f"f{oid}", qty=to_micro(qty),
                  price_native=to_micro(price), fx_rate=to_micro("1"), ts=ts)


def test_views_exist_and_agree_with_the_book(repo):
    fill(repo, "o1", "buy", "10", "200")
    fill(repo, "o2", "sell", "4", "250", ts=T0 + 10)
    v = repo.conn.execute("SELECT * FROM v_positions").fetchone()
    assert (v["qty"], v["cost_base"]) == (6, 1200.0)
    pl = repo.conn.execute("SELECT * FROM v_share_pl").fetchone()
    assert pl["realized_pl"] == 200.0               # (250-200)×4
    bm = repo.conn.execute("SELECT * FROM v_broker_metrics").fetchone()
    assert bm["fills"] == 2
    assert repo.conn.execute("SELECT * FROM v_thesis_outcomes").fetchall() \
        == []                                       # no thesis yet — fine


def test_apply_split_conserves_basis(repo):
    fill(repo, "o1", "buy", "10", "200")
    repo.apply_split("NASDAQ:NVDA", 2, ex_date=T0 + 100, ts=T0 + 100)
    lot = repo.conn.execute("SELECT * FROM lots").fetchone()
    assert lot["qty_remaining"] == to_micro("20")
    assert lot["cost_per_share_base"] == to_micro("100")
    ca = repo.conn.execute("SELECT * FROM corporate_actions").fetchone()
    assert (ca["kind"], ca["ratio"]) == ("split", 2.0)
    repo.assert_invariants("acct")


def test_max_position_pct(repo):
    repo.conn.execute("UPDATE broker_accounts SET risk_limits=?"
                      " WHERE id='acct'",
                      (json.dumps({"max_position_pct": 20}),))
    with pytest.raises(RiskLimitExceeded, match="max_position_pct"):
        repo.create_order(id="big", account_id="acct",
                          instrument_id="NASDAQ:NVDA", side="buy",
                          qty=to_micro("30"), limit_price=to_micro("100"),
                          expires_at=T0 + 86400, ts=T0)   # 3000 of 10000 = 30%
    repo.create_order(id="ok", account_id="acct",
                      instrument_id="NASDAQ:NVDA", side="buy",
                      qty=to_micro("10"), limit_price=to_micro("100"),
                      expires_at=T0 + 86400, ts=T0)       # 10% — fine


def test_holidays_seeded():
    from app import sessions
    xmas = datetime(2026, 12, 25, 15, 0,
                    tzinfo=ZoneInfo("America/New_York"))  # a Friday
    assert not sessions.is_open("NYSE", xmas)
    cny = datetime(2026, 2, 17, 10, 0, tzinfo=ZoneInfo("Asia/Singapore"))
    assert not sessions.is_open("SGX", cny)
    conn = db.connect(":memory:")
    db.init(conn)
    conn.execute("INSERT INTO exchange_sessions (exchange, tz, open_time,"
                 " close_time, holidays) VALUES ('NYSE',"
                 " 'America/New_York', '09:30', '16:00', '[]')")
    sessions.seed_db(conn)                          # upgrades the empty list
    loaded = sessions.load(conn)
    assert "2026-12-25" in loaded["NYSE"].holidays


def test_provider_health_gate_and_model_capture():
    conn = db.connect(":memory:")
    db.init(conn)
    seeds.seed(conn, ts=T0)
    with pytest.raises(registry.CapabilityError, match="health probe"):
        registry.assign(conn, "news", "deepseek", "deepseek-chat")

    class OkLLM:
        def invoke(self, *_):
            return "ok"
    assert health.probe(conn, "deepseek",
                        _build=lambda *a, **k: OkLLM(), env={}) is True
    registry.assign(conn, "news", "deepseek", "deepseek-chat")   # now passes
    row = conn.execute("SELECT * FROM provider_models WHERE"
                       " provider_id='deepseek'").fetchone()
    assert row is not None                          # probe recorded the model
    # keeping the CURRENT provider needs no probe
    registry.assign(conn, "news", "deepseek", "other-model")


def test_chat_threads_persist():
    from app.graph.claire_agent import Claire
    conn = db.connect(":memory:")
    db.init(conn)
    c1 = Claire(conn, None, None, None)
    c1._load("t1").extend([{"role": "user", "content": "hello"}])
    c1._save("t1")
    c2 = Claire(conn, None, None, None)             # "restart"
    assert c2._load("t1") == [{"role": "user", "content": "hello"}]


class FakeTD:
    def get(self, url, params=None):
        class R:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                if "quote" in url:
                    assert params["exchange"] == "SGX"
                    return {"symbol": "D05", "close": "41.20",
                            "currency": "SGD", "percent_change": "1.1",
                            "volume": "1000", "is_market_open": True}
                if "exchange_rate" in url:
                    return {"rate": 0.7815}
                return {"values": [
                    {"datetime": "2026-08-11", "open": "40", "high": "42",
                     "low": "39", "close": "41", "volume": "500"}],
                    "meta": {"currency": "SGD"}}
        return R()


def test_twelvedata_client_and_market_chain():
    td = TDClient("k", _client=FakeTD())
    q = td.quote(["D05.SI"])
    assert q["D05.SI"]["price"] == Decimal("41.20")
    assert q["D05.SI"]["src"] == "twelvedata"
    c = td.chart("D05.SI")
    assert c["close"] == [41.0]
    assert td.fx("SGD", "USD") == "0.7815"
    # market prefers the primary source and never touches yahoo
    def yahoo_forbidden(*a, **k):
        raise AssertionError("yahoo should not be called")
    m = Market(_get=yahoo_forbidden, primary=td, clock=lambda: 1000)
    assert m.spark(["D05.SI"])["D05.SI"]["src"] == "twelvedata"
    assert m.fx("SGD", "USD") == Decimal("0.7815")
    assert m.primary_name() == "twelvedata"


def test_custodian_drift_archive_unprotected(repo):
    from app.custodian import Custodian, _account_broker_from_db
    fill(repo, "o1", "buy", "10", "200")            # order without stop_loss
    conn = repo.conn
    conn.execute("INSERT INTO broker_snapshots (account_id, taken_at, cash,"
                 " positions_json) VALUES ('acct', ?, ?, ?)",
                 (T0, to_micro("5000"),                # book says 8000
                  json.dumps({"positions": {"NASDAQ:NVDA": 99}})))
    conn.execute("INSERT INTO work_items (id, kind, ticker, state, thread_id,"
                 " created_at, updated_at) VALUES ('wi_old', 'pipeline', 'X',"
                 " 'done', 'wi_old', ?, ?)", (T0 - 40 * 86400,
                                              T0 - 40 * 86400))
    c = Custodian(conn, repo, {}, resume_post=lambda b: (200, {}),
                  account_broker=_account_broker_from_db(conn),
                  fx_rate_for=lambda i, a: to_micro("1"),
                  clock=lambda: T0 + 60)
    report = c.run_once()
    flags = " | ".join(report["flags"])
    assert "CASH DRIFT" in flags
    assert "POSITION DRIFT" in flags
    assert "UNPROTECTED" in flags
    assert report["archived"] == 1
    row = conn.execute("SELECT archived_at FROM work_items WHERE id='wi_old'"
                       ).fetchone()
    assert row["archived_at"] is not None
