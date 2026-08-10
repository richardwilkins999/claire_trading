"""Tools layer tests — market (offline via injected fetcher), narrative jail,
run_python sandbox."""
import os
from decimal import Decimal

import pytest

from app.tools.files import JailError, Narratives
from app.tools.market import (Market, currency_for, normalize_price,
                              yahoo_symbol)
from app.tools.sandbox import run_python


def test_symbol_and_currency_mapping():
    assert yahoo_symbol("C07", "SGX") == "C07.SI"
    assert yahoo_symbol("NVDA", "NASDAQ") == "NVDA"
    assert currency_for("SHEL.L") == "GBp"
    assert currency_for("C07.SI") == "SGD"
    assert currency_for("NVDA") == "USD"


def test_gbp_pence_normalization():
    px, ccy = normalize_price(3550, "GBp")      # LSE quotes are PENCE
    assert (px, ccy) == (Decimal("35.5"), "GBP")
    px, ccy = normalize_price(101.5, "USD")
    assert (px, ccy) == (Decimal("101.5"), "USD")


def make_market(responses, clock=None):
    calls = []

    def fake_get(url, params=None, need_crumb=False):
        calls.append(url)
        for frag, data in responses.items():
            if frag in url:
                return data
        raise AssertionError(f"unexpected URL {url}")
    t = {"now": 1000}
    m = Market(_get=fake_get, clock=lambda: t["now"])
    return m, calls, t


def test_spark_flat_shape_and_stale_fallback():
    m, calls, _ = make_market({"spark": {
        "NVDA": {"close": 181.5, "previousClose": 180.0, "currency": "USD"},
        "C07.SI": {"close": None, "previousClose": 3.42},   # market shut
    }})
    q = m.spark(["NVDA", "C07.SI"])
    assert q["NVDA"]["price"] == Decimal("181.5")
    assert q["NVDA"]["stale"] is False
    assert q["C07.SI"]["price"] == Decimal("3.42")          # previousClose
    assert q["C07.SI"]["stale"] is True
    assert q["C07.SI"]["currency"] == "SGD"                 # from suffix


def test_fx_via_chart_symbol_and_cache():
    chart = {"chart": {"result": [{"timestamp": [1], "indicators": {"quote": [
        {"open": [1], "high": [1], "low": [1], "close": [0.74],
         "volume": [0]}]}, "meta": {"currency": "USD"}}]}}
    m, calls, t = make_market({"chart/SGDUSD=X": chart})
    assert m.fx("SGD", "USD") == Decimal("0.74")
    n = len(calls)
    assert m.fx("SGD", "USD") == Decimal("0.74")
    assert len(calls) == n                      # cached ~5 min
    t["now"] += 400
    m.fx("SGD", "USD")
    assert len(calls) == n + 1                  # cache expired → refetched
    assert m.fx("USD", "USD") == Decimal(1)


def test_narratives_jail(tmp_path):
    n = Narratives(tmp_path / "narratives")
    n.write("wi_1", "fundamental", "# analysis")
    assert n.read("wi_1", "fundamental") == "# analysis"
    assert n.list("wi_1") == ["fundamental.md"]
    with pytest.raises(JailError):
        n.write("wi_1", "../../../etc/passwd", "nope")
    with pytest.raises(JailError):
        n.write("../escape", "f", "nope")


def test_sandbox_computes_with_numpy():
    out = run_python(
        "import numpy as np\n"
        "closes = np.array(DATA['close'], dtype=float)\n"
        "RESULT = {'sma3': float(closes[-3:].mean())}\n",
        {"close": [10, 11, 12, 13, 14]})
    assert out["result"]["sma3"] == 13.0


def test_sandbox_blocks_network_and_scrubs_env():
    os.environ["CLAIRE_INTERNAL_SECRET"] = "supersecret"
    try:
        out = run_python(
            "import socket\n"
            "try:\n"
            "    socket.socket()\n"
            "    net = 'open'\n"
            "except Exception as e:\n"
            "    net = 'blocked'\n"
            "import os\n"
            "RESULT = {'net': net,"
            " 'secret_visible': 'CLAIRE_INTERNAL_SECRET' in os.environ}\n",
            {})
    finally:
        del os.environ["CLAIRE_INTERNAL_SECRET"]
    assert out["result"] == {"net": "blocked", "secret_visible": False}


def test_sandbox_timeout():
    out = run_python("while True: pass", {}, timeout=2)
    assert "error" in out
