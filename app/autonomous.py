"""Autonomous pre-open runs (DESIGN.md §16): `python -m app.autonomous
--region asia|eu|us`. Starting work needs no LLM — this CLI just asks
claire-api to run the pipeline; verdicts land awaiting_approval like any
other run.
"""
import argparse
import json

import httpx

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
        except Exception:                       # noqa: BLE001 — screener is
            continue                            # unofficial; skip, don't die
        for r in rows:
            sym = r.get("symbol") or ""
            ticker = sym.split(".")[0]
            if ticker and ticker not in held:
                out.append((ticker, ex))
                break
    return out[:limit]


def main(argv=None):
    from .api.server import load_env
    from .tools.market import Market

    ap = argparse.ArgumentParser()
    ap.add_argument("--region", choices=list(REGIONS), required=True)
    ap.add_argument("--api", default="http://127.0.0.1:7788")
    ap.add_argument("--ignore-hours", action="store_true",
                    help="scan even when the market is closed")
    args = ap.parse_args(argv)
    load_env()

    from datetime import datetime, timezone

    from . import sessions
    from .accounting import db
    from .api.server import ROOT
    conn = db.connect(ROOT / "var" / "desk.db")
    held = {r["ticker"] for r in conn.execute(
        "SELECT DISTINCT i.ticker FROM lots l JOIN instruments i"
        " ON i.id=l.instrument_id WHERE l.qty_remaining > 0")}

    # the screener only scans markets that are actually trading
    exchanges = REGIONS[args.region]
    if not args.ignore_hours:
        cal = sessions.load(conn) or None
        now = datetime.now(timezone.utc)
        open_ex = [ex for ex in exchanges if sessions.is_open(ex, now, cal)]
        skipped = sorted(set(exchanges) - set(open_ex))
        if skipped:
            print(json.dumps({"skipped_closed": skipped}))
        exchanges = open_ex

    started = []
    for ticker, ex in pick_candidates(Market(), exchanges, held):
        r = httpx.post(f"{args.api}/api/run",
                       json={"ticker": ticker, "exchange": ex,
                             "currency": CCY[ex],
                             "lot_size": 100 if ex == "SGX" else 1},
                       timeout=10)
        if r.status_code == 200:
            started.append(r.json()["work_item_id"])
    print(json.dumps({"region": args.region, "started": started}))


if __name__ == "__main__":
    main()
