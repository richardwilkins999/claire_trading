"""The desk's ten exchanges — the ONE table of what each one is.

Currency, Yahoo suffix, board lot, time zone and region used to be typed
out in eleven tables across seven Python files, and again in five web pages
("100 if SGX else 1" alone appeared nine times) — REVIEW-2026-11-11 §3.1.
Python reads this module; the pages read the same table from web/nav.js
(DESK), and tests/test_exchanges.py fails if the two ever differ.

Opening hours and holidays are not here: they are app/sessions.py.
"""

EXCHANGES = {
    #           account ccy  yahoo   board  IANA zone             region
    "NASDAQ": {"currency": "USD", "suffix": "",    "lot": 1,
               "tz": "America/New_York", "region": "us"},
    "NYSE":   {"currency": "USD", "suffix": "",    "lot": 1,
               "tz": "America/New_York", "region": "us"},
    "LSE":    {"currency": "GBP", "suffix": ".L",  "lot": 1,
               "tz": "Europe/London", "region": "eu"},
    "XETRA":  {"currency": "EUR", "suffix": ".DE", "lot": 1,
               "tz": "Europe/Berlin", "region": "eu"},
    "PARIS":  {"currency": "EUR", "suffix": ".PA", "lot": 1,
               "tz": "Europe/Paris", "region": "eu"},
    # SGX trades in board lots of 100; an odd lot is refused after approval
    "SGX":    {"currency": "SGD", "suffix": ".SI", "lot": 100,
               "tz": "Asia/Singapore", "region": "asia"},
    "HKEX":   {"currency": "HKD", "suffix": ".HK", "lot": 1,
               "tz": "Asia/Hong_Kong", "region": "asia"},
    "TSE":    {"currency": "JPY", "suffix": ".T",  "lot": 1,
               "tz": "Asia/Tokyo", "region": "asia"},
    "ASX":    {"currency": "AUD", "suffix": ".AX", "lot": 1,
               "tz": "Australia/Sydney", "region": "asia"},
    "NSE":    {"currency": "INR", "suffix": ".NS", "lot": 1,
               "tz": "Asia/Kolkata", "region": None},
}

CCY = {ex: e["currency"] for ex, e in EXCHANGES.items()}
# exchange → Yahoo suffix; NASDAQ/NYSE have none and are left out, so code
# that loops over the suffixes never matches the empty string
SUFFIX = {ex: e["suffix"] for ex, e in EXCHANGES.items() if e["suffix"]}
REGIONS = {r: [ex for ex, e in EXCHANGES.items() if e["region"] == r]
           for r in ("asia", "eu", "us")}


def lot_size(exchange) -> int:
    """Shares per board lot; 1 for an exchange the desk does not know."""
    return EXCHANGES.get(exchange, {}).get("lot", 1)


def currency(exchange, default=None):
    return EXCHANGES.get(exchange, {}).get("currency", default)
