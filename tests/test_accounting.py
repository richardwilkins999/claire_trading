"""Accounting layer tests (DESIGN.md §18 layer 1) — offline, sub-second.
All assertions are exact integer micro-units."""
import random
from decimal import Decimal

import pytest

from app.accounting import db
from app.accounting.money import from_micro, to_micro
from app.accounting.repo import (InsufficientCash, LedgerError, Repo,
                                 RiskLimitExceeded)

T0 = 1_754_900_000  # arbitrary fixed epoch; tests never read the clock
USD1 = to_micro("1")


@pytest.fixture
def repo():
    conn = db.connect(":memory:")
    db.init(conn)
    r = Repo(conn)
    r.create_account("acct", "alpaca", "paper", "USD",
                     fee_model={"type": "flat", "per_trade": "0"}, ts=T0)
    r.add_instrument("NASDAQ:NVDA", "NVDA", "NASDAQ", "USD")
    r.add_instrument("SGX:C07", "C07", "SGX", "SGD")
    return r


def buy(r, oid, qty, price, *, commission=None, fill_qty=None, ts=T0,
        instrument="NASDAQ:NVDA", fx="1", fill_id=None, side="buy"):
    r.create_order(id=oid, account_id="acct", instrument_id=instrument,
                   side=side, qty=to_micro(qty), expires_at=ts + 86400, ts=ts)
    return r.record_fill(
        oid, broker_fill_id=fill_id or f"f_{oid}", qty=to_micro(fill_qty or qty),
        price_native=to_micro(price), fx_rate=to_micro(fx), ts=ts,
        commission=None if commission is None else to_micro(commission))


def sell(r, oid, qty, price, **kw):
    return buy(r, oid, qty, price, side="sell", **kw)


def test_worked_example_fifo(repo):
    """DESIGN.md §7: 10@200(+1), 5@220(+1), 10@180(+1), sell 12@240(−1.50)
    → realized +437.10; 13 shares remain at 189.35 average cost."""
    repo.deposit("acct", to_micro("10000"), ts=T0)
    buy(repo, "o1", "10", "200", commission="1", ts=T0 + 1)
    buy(repo, "o2", "5", "220", commission="1", ts=T0 + 2)
    buy(repo, "o3", "10", "180", commission="1", ts=T0 + 3)
    repo.assert_invariants("acct")
    sell(repo, "o4", "12", "240", commission="1.50", ts=T0 + 4)
    repo.assert_invariants("acct")

    assert repo.realized_pl("acct") == to_micro("437.10")
    qty, cost, comm = repo.position("acct", "NASDAQ:NVDA")
    assert qty == to_micro("13")
    assert cost + comm == to_micro("2461.60")
    avg = from_micro(cost + comm) / from_micro(qty)
    assert avg.quantize(Decimal("0.01")) == Decimal("189.35")
    # cash: 10000 − 4900 gross − 3 fees + 2880 − 1.50 = 7975.50
    assert repo.cash_balance("acct") == to_micro("7975.50")
    # FIFO shape: lot A fully closed, lot B partially
    rows = repo.conn.execute(
        "SELECT qty, realized_pl_base FROM lot_closures ORDER BY id").fetchall()
    assert [r["qty"] for r in rows] == [to_micro("10"), to_micro("2")]
    assert rows[0]["realized_pl_base"] == to_micro("397.75")
    assert rows[1]["realized_pl_base"] == to_micro("39.35")


def test_partial_fills_flat_fee_charged_once(repo):
    repo.create_account("moomoo", "moomoo", "paper", "USD",
                        fee_model={"type": "flat", "per_trade": "0.99"}, ts=T0)
    repo2 = repo
    repo2.create_order(id="o1", account_id="moomoo",
                       instrument_id="NASDAQ:NVDA", side="buy",
                       qty=to_micro("10"), expires_at=T0 + 86400, ts=T0)
    repo2.deposit("moomoo", to_micro("5000"), ts=T0)
    repo2.record_fill("o1", broker_fill_id="f1", qty=to_micro("4"),
                      price_native=to_micro("100"), fx_rate=USD1, ts=T0 + 1)
    assert repo2.order("o1")["status"] == "partially_filled"
    repo2.record_fill("o1", broker_fill_id="f2", qty=to_micro("6"),
                      price_native=to_micro("101"), fx_rate=USD1, ts=T0 + 2)
    assert repo2.order("o1")["status"] == "filled"
    comms = [r["commission"] for r in repo2.conn.execute(
        "SELECT commission FROM executions WHERE order_id='o1' ORDER BY recorded_at")]
    assert comms == [to_micro("0.99"), 0]               # flat fee bills once
    repo2.assert_invariants("moomoo")


def test_pct_fee_with_min_cumulative(repo):
    repo.create_account("saxo", "saxo", "sim", "USD",
                        fee_model={"type": "pct", "pct": "0.0008", "min": "5"},
                        ts=T0)
    repo.deposit("saxo", to_micro("50000"), ts=T0)
    repo.create_order(id="s1", account_id="saxo", instrument_id="NASDAQ:NVDA",
                      side="buy", qty=to_micro("200"), expires_at=T0 + 86400, ts=T0)
    # two 10k halves: first bills max(8,5)=8; cumulative 20k → 16, so +8
    repo.record_fill("s1", broker_fill_id="f1", qty=to_micro("100"),
                     price_native=to_micro("100"), fx_rate=USD1, ts=T0 + 1)
    repo.record_fill("s1", broker_fill_id="f2", qty=to_micro("100"),
                     price_native=to_micro("100"), fx_rate=USD1, ts=T0 + 2)
    comms = [r["commission"] for r in repo.conn.execute(
        "SELECT commission FROM executions WHERE order_id='s1' ORDER BY recorded_at")]
    assert comms == [to_micro("8"), to_micro("8")]
    # small notional hits the min
    repo.create_order(id="s2", account_id="saxo", instrument_id="NASDAQ:NVDA",
                      side="buy", qty=to_micro("10"), expires_at=T0 + 86400, ts=T0)
    repo.record_fill("s2", broker_fill_id="f3", qty=to_micro("10"),
                     price_native=to_micro("100"), fx_rate=USD1, ts=T0 + 3)
    assert repo.conn.execute(
        "SELECT commission FROM executions WHERE order_id='s2'"
    ).fetchone()["commission"] == to_micro("5")


def test_fill_idempotent(repo):
    repo.deposit("acct", to_micro("10000"), ts=T0)
    ex1 = buy(repo, "o1", "10", "100", ts=T0 + 1)
    ex2 = repo.record_fill("o1", broker_fill_id="f_o1", qty=to_micro("10"),
                           price_native=to_micro("100"), fx_rate=USD1, ts=T0 + 9)
    assert ex1 == ex2
    (n,) = repo.conn.execute("SELECT COUNT(*) FROM executions").fetchone()
    assert n == 1
    assert repo.cash_balance("acct") == to_micro("9000")


def test_overfill_refused(repo):
    repo.deposit("acct", to_micro("10000"), ts=T0)
    buy(repo, "o1", "10", "100", fill_qty="10", ts=T0 + 1)
    with pytest.raises(LedgerError, match="overrun"):
        repo.record_fill("o1", broker_fill_id="extra", qty=to_micro("1"),
                         price_native=to_micro("100"), fx_rate=USD1, ts=T0 + 2)


def test_insufficient_cash_rolls_back(repo):
    repo.deposit("acct", to_micro("500"), ts=T0)
    with pytest.raises(InsufficientCash):
        buy(repo, "o1", "10", "100", ts=T0 + 1)
    (n,) = repo.conn.execute("SELECT COUNT(*) FROM executions").fetchone()
    assert n == 0
    assert repo.cash_balance("acct") == to_micro("500")
    repo.assert_invariants("acct")


def test_short_open_and_cover(repo):
    repo.deposit("acct", to_micro("1000"), ts=T0)
    sell(repo, "o1", "5", "100", ts=T0 + 1)             # sell-to-open
    qty, _, _ = repo.position("acct", "NASDAQ:NVDA")
    assert qty == -to_micro("5")
    buy(repo, "o2", "5", "90", ts=T0 + 2)               # buy-to-cover
    qty, _, _ = repo.position("acct", "NASDAQ:NVDA")
    assert qty == 0
    assert repo.realized_pl("acct") == to_micro("50")   # (100−90)×5, zero fees
    repo.assert_invariants("acct")


def test_fx_snapshot(repo):
    repo.deposit("acct", to_micro("1000"), ts=T0)
    buy(repo, "o1", "100", "3.50", instrument="SGX:C07", fx="0.74", ts=T0 + 1)
    ex = repo.conn.execute("SELECT * FROM executions").fetchone()
    assert ex["gross_base"] == to_micro("259")          # 100 × 3.50 × 0.74
    assert ex["currency"] == "SGD"
    lot = repo.conn.execute("SELECT * FROM lots").fetchone()
    assert lot["cost_per_share_base"] == to_micro("2.59")
    assert repo.cash_balance("acct") == to_micro("741")


def test_live_environment_unrepresentable(repo):
    with pytest.raises(LedgerError):
        repo.create_account("x", "alpaca", "live", "USD",
                            fee_model={"type": "flat"}, ts=T0)


def test_risk_limits(repo):
    repo.create_account("capped", "alpaca", "paper", "USD",
                        fee_model={"type": "flat", "per_trade": "0"},
                        risk_limits={"max_order_base": 1000,
                                     "max_trades_per_day": 1}, ts=T0)
    repo.deposit("capped", to_micro("100000"), ts=T0)
    with pytest.raises(RiskLimitExceeded, match="max_order_base"):
        repo.create_order(id="big", account_id="capped",
                          instrument_id="NASDAQ:NVDA", side="buy",
                          qty=to_micro("100"), limit_price=to_micro("100"),
                          expires_at=T0 + 86400, ts=T0)
    repo.create_order(id="ok1", account_id="capped",
                      instrument_id="NASDAQ:NVDA", side="buy",
                      qty=to_micro("5"), limit_price=to_micro("100"),
                      expires_at=T0 + 86400, ts=T0)
    with pytest.raises(RiskLimitExceeded, match="max_trades_per_day"):
        repo.create_order(id="ok2", account_id="capped",
                          instrument_id="NASDAQ:NVDA", side="buy",
                          qty=to_micro("5"), limit_price=to_micro("100"),
                          expires_at=T0 + 86400, ts=T0)


def test_work_item_audit_trail(repo):
    repo.create_work_item("wi_1", "pipeline", "NVDA", ts=T0)
    repo.set_state("wi_1", "awaiting_approval", actor="system", ts=T0 + 10)
    repo.set_state("wi_1", "approved", actor="human", ts=T0 + 20,
                   payload={"size_base": 1000})
    states = [r["to_state"] for r in repo.conn.execute(
        "SELECT to_state FROM events WHERE item_id='wi_1' ORDER BY id")]
    assert states == ["running", "awaiting_approval", "approved"]


def test_property_random_sequences_hold_invariants(repo):
    """Random buys/sells (incl. partial fills and shorts) never break the
    books: lots reconcile, commissions stay non-negative, cash never dips
    below zero mid-history."""
    rng = random.Random(20260811)
    repo.deposit("acct", to_micro("100000"), ts=T0)
    ts = T0
    for i in range(150):
        ts += 60
        side = rng.choice(["buy", "sell"])
        qty = str(rng.randint(1, 20))
        price = str(rng.randint(50, 250))
        oid = f"r{i}"
        try:
            repo.create_order(id=oid, account_id="acct",
                              instrument_id="NASDAQ:NVDA", side=side,
                              qty=to_micro(qty), expires_at=ts + 86400, ts=ts)
            fills = rng.choice([1, 2])
            total = to_micro(qty)
            first = total if fills == 1 else (total // 2 // 10**6) * 10**6 or total
            for j, fq in enumerate([first, total - first]):
                if fq <= 0:
                    continue
                repo.record_fill(oid, broker_fill_id=f"{oid}_f{j}", qty=fq,
                                 price_native=to_micro(price), fx_rate=USD1,
                                 ts=ts + j,
                                 commission=to_micro(rng.choice(["0", "1", "0.99"])))
        except InsufficientCash:
            pass
        repo.assert_invariants("acct")
    assert repo.cash_balance("acct") >= 0
