"""app/accounting/holdings.py — "what does the book hold" asked one way
(REVIEW-2026-11-11 §3.1)."""
from decimal import Decimal

import pytest

from app.accounting import db, holdings
from app.accounting.money import to_micro
from app.accounting.repo import Repo

T0 = 1_786_456_800


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init(c)
    r = Repo(c)
    for acct, ccy in (("a", "USD"), ("b", "SGD")):
        r.create_account(acct, "alpaca", "paper", ccy,
                         fee_model={"type": "flat", "per_trade": "0"}, ts=T0)
        r.deposit(acct, to_micro("100000"), ts=T0)
    r.add_instrument("NASDAQ:NVDA", "NVDA", "NASDAQ", "USD")
    r.add_instrument("SGX:D05", "D05", "SGX", "SGD")
    r.add_instrument("LSE:NVDA", "NVDA", "LSE", "GBP")       # a namesake
    n = 0

    def trade(acct, inst, side, qty):
        nonlocal n
        n += 1
        r.create_order(id=f"o{n}", account_id=acct, instrument_id=inst,
                       side=side, qty=to_micro(qty), expires_at=T0 + 9, ts=T0)
        r.record_fill(f"o{n}", broker_fill_id=f"f{n}", qty=to_micro(qty),
                      price_native=to_micro("10"), fx_rate=to_micro("1"), ts=T0)
    trade("a", "NASDAQ:NVDA", "buy", "10")
    trade("a", "NASDAQ:NVDA", "buy", "2.5")      # a second lot, fractional
    trade("a", "NASDAQ:NVDA", "sell", "4")       # FIFO takes from the first
    trade("b", "NASDAQ:NVDA", "buy", "3")
    trade("b", "SGX:D05", "buy", "100")
    trade("b", "SGX:D05", "sell", "100")         # flat again
    trade("b", "LSE:NVDA", "sell", "7")          # a short
    return c


def test_net_is_exact_and_signed(conn):
    assert holdings.net(conn, ticker="NVDA", exchange="NASDAQ") == Decimal("11.5")
    assert holdings.net(conn, ticker="NVDA", exchange="NASDAQ",
                        account_id="a") == Decimal("8.5")
    assert holdings.net(conn, instrument_id="NASDAQ:NVDA") == Decimal("11.5")
    assert holdings.net(conn, ticker="NVDA", exchange="LSE") == Decimal("-7")
    # a ticker alone is that ticker on any exchange (as the callers meant)
    assert holdings.net(conn, ticker="NVDA") == Decimal("4.5")
    assert holdings.net(conn, ticker="D05") == 0            # sold out
    assert holdings.net(conn, ticker="NOPE") == 0
    assert isinstance(holdings.net(conn, ticker="NVDA"), Decimal)


def test_by_account_leaves_out_accounts_that_hold_none(conn):
    assert holdings.by_account(conn, ticker="NVDA", exchange="NASDAQ") == {
        "a": Decimal("8.5"), "b": Decimal("3")}
    assert holdings.by_account(conn, ticker="D05") == {}
    assert holdings.by_account(conn, instrument_id="LSE:NVDA") == {
        "b": Decimal("-7")}


def test_positions_lists_every_open_one(conn):
    rows = holdings.positions(conn)
    assert [(p["account_id"], p["instrument_id"], p["qty"]) for p in rows] == [
        ("a", "NASDAQ:NVDA", Decimal("8.5")),
        ("b", "LSE:NVDA", Decimal("-7")),
        ("b", "NASDAQ:NVDA", Decimal("3"))]
    assert rows[0]["ticker"] == "NVDA" and rows[0]["exchange"] == "NASDAQ"
    assert [p["instrument_id"] for p in holdings.positions(conn, "a")] == [
        "NASDAQ:NVDA"]
    assert holdings.positions(conn, "nobody") == []


def test_it_agrees_with_the_ledgers_own_position(conn):
    repo = Repo(conn)
    for acct, inst in (("a", "NASDAQ:NVDA"), ("b", "LSE:NVDA")):
        qty_micro, _, _ = repo.position(acct, inst)
        assert holdings.net(conn, instrument_id=inst, account_id=acct) \
            == Decimal(qty_micro) / 1_000_000


def test_the_callers_use_it():
    from pathlib import Path
    app = Path(holdings.__file__).resolve().parents[1]
    for name in ("api/desk.py", "telegram_approvals.py", "price_triggers.py",
                 "dash/views_runs.py", "custodian.py"):
        src = (app / name).read_text()
        assert "holdings." in src or "_book." in src, name
        assert "THEN -l.qty_remaining" not in src.replace("\n", ""), name
