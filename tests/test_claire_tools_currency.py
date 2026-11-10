"""Claire misread two tool answers (2026-11-10): a desk cost per share in
SGD taken as USD ("226.82 vs Saxo's 177.57"), and Saxo SIM's CurrentPrice
0 turned into a "-100%" loss on every position."""
import json

import pytest

from app.accounting import db
from app.accounting.money import to_micro
from app.accounting.repo import Repo
from app.graph.claire_agent import build_tools
from app.tools.saxo_live import _pct, _price_from_pl


def test_zero_price_is_recovered_from_saxos_own_pl():
    view = {"CurrentPrice": 0.0, "CurrentPriceType": "None",
            "AverageOpenPrice": 14.62, "ProfitLossOnTrade": -95.2,
            "ProfitLossOnTradeInBaseCurrency": -85.07,
            "ExposureInBaseCurrency": 0.0}
    v = _price_from_pl(view, 68)
    assert v["CurrentPrice"] == pytest.approx(13.22)
    assert v["MarketValue"] == pytest.approx(68 * 13.22, abs=0.01)
    assert v["ExposureInBaseCurrency"] == pytest.approx(
        68 * 13.22 * 85.07 / 95.2, abs=0.01)
    assert v["_price_src"] == "Saxo's P/L"
    # a short: negative amount, profit when the price falls
    s = _price_from_pl({"CurrentPrice": 0, "AverageOpenPrice": 10.0,
                        "ProfitLossOnTrade": 50.0}, -100)
    assert s["CurrentPrice"] == pytest.approx(9.5)
    # a real price is left alone; no P/L means no guess
    assert _price_from_pl({"CurrentPrice": 12.0}, 5) == {"CurrentPrice": 12.0}
    assert _price_from_pl({"CurrentPrice": 0, "AverageOpenPrice": 10},
                          5)["CurrentPrice"] == 0


def test_no_price_is_not_a_minus_100_percent_loss():
    assert _pct(0.0, 14.62, 68) is None
    assert _pct(13.22, 14.62, 68) == pytest.approx(-9.58, abs=0.01)


def test_portfolio_summary_labels_both_currencies():
    conn = db.connect(":memory:")
    db.init(conn)
    repo = Repo(conn)
    repo.create_account("saxo-paper", "saxo", "paper", "SGD",
                        fee_model={"type": "flat", "per_trade": "0"},
                        ts=1)
    repo.add_instrument("NASDAQ:DDOG", "DDOG", "NASDAQ", "USD")
    repo.deposit("saxo-paper", to_micro("100000"), ts=1)
    repo.create_order(id="b", account_id="saxo-paper",
                      instrument_id="NASDAQ:DDOG", side="buy",
                      qty=to_micro("208"), limit_price=to_micro("177.57"),
                      status="placed", expires_at=100, ts=1,
                      fx_rate=to_micro("1.2773"))
    repo.record_fill("b", broker_fill_id="f", qty=to_micro("208"),
                     price_native=to_micro("177.57"),
                     fx_rate=to_micro("1.2773"), ts=2)
    tools = {t.name: t for t in build_tools(conn, repo, None, None)}
    p = json.loads(tools["portfolio_summary"].invoke({}))[
        "saxo-paper"]["positions"][0]
    assert p["avg_cost_per_share"] == pytest.approx(177.57)
    assert p["stock_currency"] == "USD" and p["base_currency"] == "SGD"
    assert p["cost_total_base"] == pytest.approx(208 * 177.57 * 1.2773,
                                                 abs=0.01)
