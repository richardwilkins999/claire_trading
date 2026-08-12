"""Portfolio assembly + the de-Yahoo'd market sources. Offline."""
import json
from decimal import Decimal

import pytest

from app import portfolio
from app.accounting import db
from app.accounting.money import to_micro
from app.accounting.repo import Repo
from app.tools import tradingview

T0 = 1_786_456_800          # Tue 2026-08-11 14:00 UTC — NASDAQ open


class FakeMarket:
    def metrics(self, symbols):
        return {s: {"symbol": s, "name": f"{s} Inc", "price": Decimal("120"),
                    "currency": "USD", "change_pct": 1.5,
                    "day_open": Decimal("118"), "day_high": Decimal("121"),
                    "day_low": Decimal("117"), "volume": 5e6,
                    "avg_volume": 2e6, "week52_high": Decimal("150"),
                    "week52_low": Decimal("80"), "rsi": 61.0,
                    "src": "twelvedata"} for s in symbols}

    def fx(self, a, b):
        return Decimal(1)

    def chart(self, symbol, range_="5d", interval="60m"):
        return {"symbol": symbol, "timestamps": [T0 - 3600, T0],
                "open": [118, 119], "high": [121, 120], "low": [117, 118],
                "close": [119, 120], "volume": [1e6, 2e6],
                "currency": "USD", "src": "twelvedata"}


@pytest.fixture
def book():
    conn = db.connect(":memory:")
    db.init(conn)
    r = Repo(conn)
    r.create_account("acct", "alpaca", "paper", "USD",
                     fee_model={"type": "flat", "per_trade": "1"}, ts=T0)
    r.add_instrument("NASDAQ:NVDA", "NVDA", "NASDAQ", "USD")
    r.deposit("acct", to_micro("10000"), ts=T0)
    conn.execute(
        "INSERT INTO work_items (id, kind, ticker, state, thread_id,"
        " thesis_json, created_at, updated_at) VALUES ('wi_1','pipeline',"
        "'NVDA','done','wi_1',?,?,?)",
        (json.dumps({"ticker": "NVDA", "direction": "buy", "entry_low": 99,
                     "entry_high": 101, "stop_loss": 92,
                     "take_profit": 130, "currency": "USD"}), T0, T0))
    r.create_order(id="o1", account_id="acct", instrument_id="NASDAQ:NVDA",
                   side="buy", qty=to_micro("10"), work_item_id="wi_1",
                   expires_at=T0 + 999, ts=T0)
    r.record_fill("o1", broker_fill_id="f1", qty=to_micro("10"),
                  price_native=to_micro("100"), fx_rate=to_micro("1"), ts=T0)
    conn.execute("INSERT INTO price_alerts (instrument_id, rule, threshold,"
                 " armed) VALUES ('NASDAQ:NVDA','trail_pct',8,1)")
    return conn


def test_position_carries_book_truth_and_market_metrics(book):
    d = portfolio.build(book, FakeMarket(), clock=lambda: T0 + 86400)
    assert list(d["exchanges"]) == ["NASDAQ"]
    p = d["exchanges"]["NASDAQ"][0]
    # book numbers are exact
    assert p["qty"] == 10
    assert p["cost_base"] == 1000            # 10 × 100
    assert p["commissions"] == 1
    assert p["holding_days"] == 1
    # market metrics rode along
    assert p["market"]["week52_high"] == Decimal("150")
    assert p["market"]["avg_volume"] == 2e6
    # valuation: 10 × 120 = 1200 → +200 on a 1000 cost basis
    assert p["value_base"] == 1200
    assert p["unrealized_base"] == 200
    assert round(p["unrealized_pct"], 2) == 20.0
    assert round(p["weight_pct"], 1) == 100.0
    assert p["is_open"] is True               # NASDAQ open at T0+1d (Wed)


def test_levels_come_from_your_fills_and_your_thesis(book):
    d = portfolio.build(book, FakeMarket(), clock=lambda: T0 + 60)
    lv = d["exchanges"]["NASDAQ"][0]["levels"]
    assert lv["avg_cost_native"] == 100        # what you actually paid
    assert lv["stop_loss"] == 92               # from the approved thesis
    assert lv["take_profit"] == 130
    assert lv["work_item_id"] == "wi_1"
    # no peak recorded yet (the watcher has not ticked), so the floor sits
    # 8% under average cost — its starting position before it trails
    assert lv["alert"] == pytest.approx(92.0)
    fills = d["exchanges"]["NASDAQ"][0]["fills"]
    assert [(f["side"], f["qty"], f["price_native"]) for f in fills] == \
        [("buy", 10, 100)]


def test_the_drawn_floor_follows_the_peak_not_the_entry(book):
    """Once the watcher has ratcheted the peak, the chart must show the floor
    where it actually is — otherwise the line says you are protected at 92
    while the desk would alert at 119."""
    book.execute("UPDATE price_alerts SET peak_base=130 WHERE"
                 " instrument_id='NASDAQ:NVDA'")
    d = portfolio.build(book, FakeMarket(), clock=lambda: T0 + 60)
    lv = d["exchanges"]["NASDAQ"][0]["levels"]
    assert lv["alert"] == pytest.approx(119.6)
    assert "130" in lv["alert_rule"]


def test_chart_bundles_history_fills_and_levels(book):
    c = portfolio.chart_for(book, FakeMarket(), "NASDAQ:NVDA")
    assert c["symbol"] == "NVDA"
    assert c["chart"]["close"] == [119, 120]
    assert c["fills"][0]["price_native"] == 100
    assert c["levels"]["stop_loss"] == 92


def test_price_outage_never_hides_a_position(book):
    class Dead(FakeMarket):
        def metrics(self, symbols):
            return {}
    d = portfolio.build(book, Dead(), clock=lambda: T0)
    p = d["exchanges"]["NASDAQ"][0]
    assert p["price_unavailable"] is True
    assert p["value_base"] is None
    assert p["qty"] == 10 and p["cost_base"] == 1000   # book still exact


def test_closed_position_leaves_the_columns(book):
    r = Repo(book)
    r.create_order(id="o2", account_id="acct", instrument_id="NASDAQ:NVDA",
                   side="sell", qty=to_micro("10"), expires_at=T0 + 999,
                   ts=T0 + 100)
    r.record_fill("o2", broker_fill_id="f2", qty=to_micro("10"),
                  price_native=to_micro("130"), fx_rate=to_micro("1"),
                  ts=T0 + 100)
    d = portfolio.build(book, FakeMarket(), clock=lambda: T0 + 200)
    assert d["exchanges"] == {}                 # card disappears
    assert d["position_count"] == 0
    assert d["closed"][0]["ticker"] == "NVDA"   # and shows in closed
    assert d["closed"][0]["realized"] > 0


# ── the de-Yahoo'd sources ───────────────────────────────────────────────
class FakeTV:
    def __init__(self, payload):
        self.payload = payload
        self.body = None

    def post(self, url, json=None):
        self.body = json
        self.url = url

        class R:
            status_code = 200

            def raise_for_status(self_):
                pass

            def json(self_):
                return self.payload
        return R()


def test_listing_replaces_the_yahoo_screener():
    c = FakeTV({"totalCount": 527, "data": [
        {"s": "SGX:D05", "d": ["D05", "DBS Group Holdings Ltd", 76.99, 0.86,
                               8213600, 2.1e11, 19.5, 75.2, "Finance"]},
        {"s": "SGX:JPM/PM", "d": ["JPM/PM", "Preferred", 16.0, 0.1, 1e6,
                                  5e9, None, 31.0, "Finance"]}]})
    out = tradingview.listing("SGX", sort="price", start=0, count=100,
                              _client=c)
    assert out["total"] == 527
    assert [r["ticker"] for r in out["rows"]] == ["D05"]   # preferred dropped
    assert out["rows"][0]["symbol"] == "D05.SI"            # yahoo-style symbol
    assert out["rows"][0]["pe"] == 19.5
    assert out["src"] == "tradingview"
    assert c.body["sort"]["sortBy"] == "close"             # price sort mapped
    assert c.body["range"] == [0, 100]
    # the old Yahoo sort names still work from the existing UI
    tradingview.listing("SGX", sort="intradaymarketcap", _client=c)
    assert c.body["sort"]["sortBy"] == "market_cap_basic"


def test_index_quote_uses_the_global_scanner():
    c = FakeTV({"data": [{"s": "TVC:STI", "d": ["STI", "STRAITS TIMES INDEX",
                                                5754.18, 0.978, None]}]})
    idx = tradingview.index_quote("SGX", _client=c)
    assert "global/scan" in c.url
    assert c.body["symbols"]["tickers"] == ["TVC:STI"]
    assert idx["level"] == 5754.18 and idx["name"] == "STRAITS TIMES INDEX"
    assert tradingview.index_quote("NOWHERE", _client=c) is None


def test_metrics_cover_every_exchange():
    c = FakeTV({"data": [{"s": "SGX:D05",
                          "d": ["D05", "DBS", 76.99, 0.86, 8213600, 5526776,
                                77.97, 49.37, 77.97, 75.98, 76.59, 75.2,
                                2.1e11, "SGD"]}]})
    m = tradingview.metrics(["D05.SI"], _client=c)["D05.SI"]
    assert m["week52_high"] == Decimal("77.97")
    assert m["avg_volume"] == 5526776
    assert m["day_low"] == Decimal("75.98")
    assert m["currency"] == "SGD"


def test_search_prefers_twelvedata_reference_endpoint():
    from app.tools.market import Market

    class TD:
        def supports(self, s):
            return True

        def symbol_search(self, q, limit=12):
            return [{"symbol": "D05", "name": "DBS", "exchange": "SGX",
                     "src": "twelvedata"}]

    def yahoo_forbidden(*a, **k):
        raise AssertionError("Yahoo must not be used for search")
    m = Market(_get=yahoo_forbidden, primary=TD(), clock=lambda: 1000)
    assert m.search("dbs")[0]["src"] == "twelvedata"
