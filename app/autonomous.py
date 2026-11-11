"""Regions and the deterministic fallback picker for scheduled runs
(DESIGN.md §16). The scheduler inside claire-api starts the runs; the
`python -m app.autonomous` CLI that systemd timers were meant to call was
never installed and is gone.
"""
import logging

from .logsetup import swallowed

log = logging.getLogger(__name__)

REGIONS = {
    "asia": ["SGX", "HKEX", "TSE", "ASX"],
    "eu": ["LSE", "XETRA", "PARIS"],
    "us": ["NASDAQ", "NYSE"],
}
CCY = {"NASDAQ": "USD", "NYSE": "USD", "LSE": "GBP", "SGX": "SGD",
       "HKEX": "HKD", "TSE": "JPY", "ASX": "AUD", "XETRA": "EUR",
       "PARIS": "EUR", "NSE": "INR"}


def pick_candidates(market, exchanges, held, limit=2):
    """Top liquid names from the region's screeners, excluding held tickers."""
    out = []
    for ex in exchanges:
        try:
            rows = market.screener(ex, count=25)["rows"]
        except Exception as e:                  # noqa: BLE001 — screener is
            swallowed(log, "exchange listing failed while picking candidates",
                      e)
            continue                            # unofficial; skip, don't die
        for r in rows:
            sym = r.get("symbol") or ""
            ticker = sym.split(".")[0]
            if ticker and ticker not in held:
                out.append((ticker, ex))
                break
    return out[:limit]
