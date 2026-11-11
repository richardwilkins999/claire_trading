"""The second half of REVIEW-2026-11-11 B6: does the venue hold the shares
the book says it does?"""
from decimal import Decimal

import httpx
import pytest

from app.accounting import db
from app.accounting.money import to_micro
from app.accounting.repo import Repo
from app.custodian import Custodian
from app.tools.saxo_broker import SaxoBroker

T0 = 1_786_456_800


class Venue:
    def __init__(self, held, local=()):
        self.held, self.local, self.error = held, set(local), None

    def positions(self):
        if self.error:
            raise RuntimeError(self.error)
        return self.held

    def is_local(self, sym):
        return sym in self.local


@pytest.fixture
def desk():
    conn = db.connect(":memory:")
    db.init(conn)
    repo = Repo(conn)
    repo.create_account("saxo-paper", "saxo", "paper", "SGD",
                        fee_model={"type": "flat", "per_trade": "0"}, ts=T0)
    repo.deposit("saxo-paper", to_micro("100000"), ts=T0)
    for n, (iid, tk, ex, ccy, qty) in enumerate((
            ("NASDAQ:AAPL", "AAPL", "NASDAQ", "USD", "6"),
            ("SGX:D05", "D05", "SGX", "SGD", "100"))):
        repo.add_instrument(iid, tk, ex, ccy)
        repo.create_order(id=f"o{n}", account_id="saxo-paper",
                          instrument_id=iid, side="buy", qty=to_micro(qty),
                          expires_at=T0 + 999, ts=T0)
        repo.record_fill(f"o{n}", broker_fill_id=f"f{n}", qty=to_micro(qty),
                         price_native=to_micro("10"), fx_rate=to_micro("1"),
                         ts=T0)
    return conn, repo


def _custodian(conn, repo, venue):
    return Custodian(conn, repo, {"saxo": venue},
                     resume_post=lambda b: (200, {}),
                     account_broker=lambda a: "saxo",
                     fx_rate_for=lambda i, a: to_micro("1"), clock=lambda: T0)


def _drift(report):
    return [f for f in report["flags"] if f.startswith("POSITION DRIFT")]


def test_matching_holdings_raise_nothing(desk):
    conn, repo = desk
    c = _custodian(conn, repo, Venue({"AAPL": Decimal(6)}, local={"D05.SI"}))
    assert "_position_drift" in Custodian.DUTIES
    for _ in range(3):
        assert _drift(c.run_once()) == []


def test_shares_missing_at_the_venue_are_flagged_on_the_second_run(desk):
    conn, repo = desk
    venue = Venue({"AAPL": Decimal(2)}, local={"D05.SI"})   # 4 were sold there
    c = _custodian(conn, repo, venue)
    assert _drift(c.run_once()) == []           # once could be a fill in flight
    (flag,) = _drift(c.run_once())
    assert "saxo-paper" in flag and "holds 6 AAPL" in flag
    assert "venue holds 2" in flag and "reconcile" in flag
    # it stays flagged while it stays true, and clears when it is fixed
    assert len(_drift(c.run_once())) == 1
    venue.held = {"AAPL": Decimal(6)}
    assert _drift(c.run_once()) == []


def test_a_name_missing_altogether_counts_as_zero(desk):
    conn, repo = desk
    c = _custodian(conn, repo, Venue({}, local={"D05.SI"}))
    c.run_once()
    (flag,) = _drift(c.run_once())
    assert "holds 6 AAPL" in flag and "venue holds 0" in flag


def test_the_venue_holding_more_is_not_drift(desk):
    """The owner's own shares sit on the same account."""
    conn, repo = desk
    c = _custodian(conn, repo, Venue({"AAPL": Decimal(50), "TSLA": Decimal(9)},
                                     local={"D05.SI"}))
    c.run_once()
    assert _drift(c.run_once()) == []


def test_locally_simulated_names_are_not_expected_at_the_venue(desk):
    conn, repo = desk
    c = _custodian(conn, repo, Venue({"AAPL": Decimal(6)}, local={"D05.SI"}))
    c.run_once()
    assert not any("D05" in f for f in _drift(c.run_once()))
    # ...and without that exemption the SGX holding WOULD be drift
    c2 = _custodian(conn, repo, Venue({"AAPL": Decimal(6)}))
    c2.run_once()
    assert any("D05.SI" in f for f in _drift(c2.run_once()))


def test_a_changing_mismatch_waits_for_it_to_settle(desk):
    conn, repo = desk
    venue = Venue({"AAPL": Decimal(5)}, local={"D05.SI"})
    c = _custodian(conn, repo, venue)
    c.run_once()
    venue.held = {"AAPL": Decimal(4)}           # still moving
    assert _drift(c.run_once()) == []
    assert len(_drift(c.run_once())) == 1       # now seen twice at 4


def test_an_unreadable_venue_is_flagged_but_not_called_drift(desk):
    conn, repo = desk
    venue = Venue({}, local={"D05.SI"})
    venue.error = "HTTP 503"
    r = _custodian(conn, repo, venue).run_once()
    assert _drift(r) == []
    assert any("cannot list the venue's positions" in f for f in r["flags"])


def test_an_adapter_without_positions_is_skipped(desk):
    conn, repo = desk

    class Sim:                                   # the in-process paper sim
        pass
    r = _custodian(conn, repo, Sim()).run_once()
    assert _drift(r) == []
    assert not any("position check" in f for f in r["flags"])


def test_live_position_drift_disarms_live_trading(desk):
    from app import live_trading
    conn, repo = desk
    repo.create_account("saxo-live", "saxo_live", "live", "SGD",
                        fee_model={"type": "flat", "per_trade": "0"}, ts=T0,
                        allow_live=True, external_ref="k")
    repo.deposit("saxo-live", to_micro("5000"), ts=T0)
    repo.create_order(id="oL", account_id="saxo-live",
                      instrument_id="NASDAQ:AAPL", side="buy",
                      qty=to_micro("3"), expires_at=T0 + 999, ts=T0)
    repo.record_fill("oL", broker_fill_id="fL", qty=to_micro("3"),
                     price_native=to_micro("10"), fx_rate=to_micro("1"), ts=T0)
    conn.execute("INSERT OR IGNORE INTO live_trading (id, armed, limits)"
                 " VALUES (1, 0, '{}')")
    conn.execute("UPDATE live_trading SET armed=1 WHERE id=1")
    c = Custodian(conn, repo, {"saxo_live": Venue({"AAPL": Decimal(1)}),
                               "saxo": Venue({"AAPL": Decimal(6)},
                                             local={"D05.SI"})},
                  resume_post=lambda b: (200, {}),
                  account_broker=lambda a: "saxo_live" if a == "saxo-live"
                  else "saxo",
                  fx_rate_for=lambda i, a: to_micro("1"), clock=lambda: T0)
    c.run_once()
    r = c.run_once()
    assert any(f.startswith("POSITION DRIFT saxo-live:") for f in r["flags"])
    assert any("LIVE TRADING DISARMED" in f for f in r["flags"])
    assert conn.execute("SELECT armed FROM live_trading").fetchone()[0] == 0
    assert live_trading.status(conn, {"CLAIRE_LIVE_TRADING": "enabled"}
                               )["armed"] is False


# ── the adapter: what Saxo's answer becomes ─────────────────────────────────
def test_saxo_positions_are_summed_per_desk_symbol_for_this_account():
    rows = [
        {"PositionBase": {"AccountKey": "KEY", "Amount": 4, "AssetType": "Stock",
                          "Status": "Open"},
         "DisplayAndFormat": {"Symbol": "AAPL:xnas"}},
        {"PositionBase": {"AccountKey": "KEY", "Amount": 2, "AssetType": "Stock",
                          "Status": "Open"},
         "DisplayAndFormat": {"Symbol": "AAPL:xnas"}},          # a second buy
        {"PositionBase": {"AccountKey": "KEY", "Amount": 500, "AssetType": "Stock",
                          "Status": "Open"},
         "DisplayAndFormat": {"Symbol": "00700:xhkg"}},
        {"PositionBase": {"AccountKey": "OTHER", "Amount": 99, "AssetType": "Stock",
                          "Status": "Open"},
         "DisplayAndFormat": {"Symbol": "AAPL:xnas"}},          # another account
        {"PositionBase": {"AccountKey": "KEY", "Amount": 7, "AssetType": "CfdOnStock",
                          "Status": "Open"},
         "DisplayAndFormat": {"Symbol": "D05:xses"}},           # not shares
        {"PositionBase": {"AccountKey": "KEY", "Amount": 3, "AssetType": "Stock",
                          "Status": "Closed"},
         "DisplayAndFormat": {"Symbol": "GOOG:xnas"}},
        {"PositionBase": {"AccountKey": "KEY", "Amount": 1, "AssetType": "Stock",
                          "Status": "Open"},
         "DisplayAndFormat": {"Symbol": "XYZ:xzzz"}},           # unmapped market
    ]
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, str(request.url.query)))
        return httpx.Response(200, json={"Data": rows})
    b = SaxoBroker("sim", token=lambda: "t", account_key="KEY",
                   client=httpx.Client(transport=httpx.MockTransport(handler),
                                       base_url="https://gateway.saxobank.com/sim/openapi"))
    assert b.positions() == {"AAPL": Decimal(6), "0700.HK": Decimal(500)}
    method, path, query = seen[0]
    assert method == "GET" and path.endswith("/port/v1/positions/me")
    assert "PositionBase" in query and "DisplayAndFormat" in query
