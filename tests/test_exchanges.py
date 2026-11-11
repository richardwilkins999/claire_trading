"""One table of exchange facts (REVIEW-2026-11-11 §3.1): app/exchanges.py
for Python, DESK in web/nav.js for the pages — and they must agree."""
import re
from pathlib import Path

from app import exchanges, sessions
from app.tools import market

ROOT = Path(__file__).resolve().parents[1]


def _desk_from_nav():
    src = (ROOT / "web" / "nav.js").read_text()
    block = re.search(r"const DESK = \{(.*?)\n\};", src, re.S).group(1)
    out = {}
    for ex, body in re.findall(r"(\w+): \{(.*?)\}", block):
        f = dict(re.findall(r"(\w+): '?([^',}]*)'?", body))
        out[ex] = {"currency": f["ccy"], "suffix": f["suffix"],
                   "lot": int(f["lot"]), "tz": f["tz"]}
    return out


def test_the_pages_and_python_share_one_exchange_table():
    py = {ex: {k: e[k] for k in ("currency", "suffix", "lot", "tz")}
          for ex, e in exchanges.EXCHANGES.items()}
    assert _desk_from_nav() == py


def test_registry_agrees_with_the_session_calendar():
    assert set(exchanges.EXCHANGES) == set(sessions.DEFAULTS)
    for ex, s in sessions.DEFAULTS.items():
        assert exchanges.EXCHANGES[ex]["tz"] == s.tz, ex


def test_derived_tables_are_what_they_were():
    assert exchanges.lot_size("SGX") == 100
    assert exchanges.lot_size("NASDAQ") == exchanges.lot_size("??") == 1
    assert "NASDAQ" not in exchanges.SUFFIX and exchanges.SUFFIX["SGX"] == ".SI"
    assert exchanges.REGIONS == {"asia": ["SGX", "HKEX", "TSE", "ASX"],
                                 "eu": ["LSE", "XETRA", "PARIS"],
                                 "us": ["NASDAQ", "NYSE"]}
    # quotes from London arrive in pence; the ACCOUNT currency is pounds
    assert market.SUFFIX_CCY[".L"] == "GBp" and exchanges.CCY["LSE"] == "GBP"
    assert market.SUFFIX_CCY == {".SI": "SGD", ".HK": "HKD", ".T": "JPY",
                                 ".AX": "AUD", ".DE": "EUR", ".PA": "EUR",
                                 ".NS": "INR", ".L": "GBp"}
    assert market.currency_for("D05.SI") == "SGD"
    assert market.currency_for("NVDA") == "USD"


def test_no_page_or_module_retypes_the_tables():
    for page in (ROOT / "web").glob("*.html"):
        src = page.read_text()
        assert not re.search(r"'SGX'\)? \? 100", src), page.name
        assert not re.search(r"SGX:\s*'SGD'|SGX:\s*'\.SI'|SGX:\s*'Asia", src), \
            page.name
    for mod in (ROOT / "app").rglob("*.py"):
        if mod.name == "exchanges.py":
            continue
        src = mod.read_text()
        assert not re.search(r'100 if .{0,40}== "SGX"', src), mod.name
        assert '"SGX": "SGD"' not in src, mod.name
