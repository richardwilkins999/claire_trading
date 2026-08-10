"""TradingView scanner (unofficial, like Yahoo — isolated in this one file).
Provides the analyst-consensus "recommended buys" list per exchange via the
public scanner endpoint's Recommend.All technical rating.
"""
import httpx

# exchange → (tradingview market, tradingview exchange code)
MARKETS = {"NASDAQ": ("america", "NASDAQ"), "NYSE": ("america", "NYSE"),
           "LSE": ("uk", "LSE"), "SGX": ("singapore", "SGX"),
           "HKEX": ("hongkong", "HKEX"), "TSE": ("japan", "TSE"),
           "ASX": ("australia", "ASX"), "XETRA": ("germany", "XETR"),
           "PARIS": ("france", "EURONEXTPAR"), "NSE": ("india", "NSE")}

COLUMNS = ["name", "description", "close", "change", "volume",
           "Recommend.All", "market_cap_basic", "RSI"]


class TradingViewError(Exception):
    pass


def rating_label(x) -> str:
    if x is None:
        return "—"
    if x >= 0.5:
        return "strong buy"
    if x >= 0.1:
        return "buy"
    if x > -0.1:
        return "neutral"
    if x > -0.5:
        return "sell"
    return "strong sell"


def recommendations(exchange: str, count: int = 30, *, _client=None) -> dict:
    if exchange not in MARKETS:
        raise TradingViewError(f"no TradingView mapping for {exchange}")
    market, code = MARKETS[exchange]
    client = _client or httpx.Client(
        timeout=20, headers={"User-Agent": "Mozilla/5.0"})

    def scan(with_exchange_filter):
        filters = [{"left": "Recommend.All", "operation": "nempty"},
                   # liquidity floor: no SPAC-unit / micro-cap noise at the top
                   {"left": "volume", "operation": "greater", "right": 500_000},
                   {"left": "market_cap_basic", "operation": "greater",
                    "right": 500_000_000}]
        if with_exchange_filter:
            filters.append({"left": "exchange", "operation": "equal",
                            "right": code})
        r = client.post(
            f"https://scanner.tradingview.com/{market}/scan",
            json={"filter": filters, "columns": COLUMNS,
                  "sort": {"sortBy": "Recommend.All", "sortOrder": "desc"},
                  "range": [0, count]})
        r.raise_for_status()
        return r.json()

    data = scan(True)
    rows = data.get("data") or []
    if not rows:                    # exchange-code mismatch → market-wide list
        data = scan(False)
        rows = data.get("data") or []
    out = []
    for row in rows:
        d = dict(zip(COLUMNS, row.get("d", [])))
        sym = (row.get("s") or ":").split(":", 1)[1]
        out.append({"ticker": sym, "name": d.get("description"),
                    "price": d.get("close"), "change_pct": d.get("change"),
                    "volume": d.get("volume"), "mcap": d.get("market_cap_basic"),
                    "rsi": d.get("RSI"),
                    "rating": d.get("Recommend.All"),
                    "rating_label": rating_label(d.get("Recommend.All"))})
    return {"exchange": exchange, "total": data.get("totalCount"),
            "rows": out}
