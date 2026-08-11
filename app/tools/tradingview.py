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


SUFFIX_MARKET = {".SI": ("singapore", "SGX", "SGD"),
                 ".HK": ("hongkong", "HKEX", "HKD"),
                 ".T": ("japan", "TSE", "JPY"),
                 ".AX": ("australia", "ASX", "AUD"),
                 ".DE": ("germany", "XETR", "EUR"),
                 ".PA": ("france", "EURONEXTPAR", "EUR"),
                 ".NS": ("india", "NSE", "INR"),
                 ".L": ("uk", "LSE", "GBp")}


def _tv_ticker(yahoo_symbol: str) -> tuple[str, str, str]:
    """yahoo symbol → (tv market, tv ticker, currency). HK quirk: Yahoo pads
    to 0700.HK, TradingView wants bare 700."""
    for suf, (mkt, _exch, ccy) in SUFFIX_MARKET.items():
        if yahoo_symbol.upper().endswith(suf):
            t = yahoo_symbol[: -len(suf)]
            if suf == ".HK":
                t = t.lstrip("0") or "0"
            return mkt, t, ccy
    return "america", yahoo_symbol, "USD"


def quotes(symbols: list[str], *, _client=None) -> dict:
    """Batch quotes via the scanner — the fallback when Yahoo rate-limits.
    Delayed-ish but honest; series is empty (no sparkline from this source)."""
    from decimal import Decimal

    from .market import normalize_price
    client = _client or httpx.Client(
        timeout=15, headers={"User-Agent": "Mozilla/5.0"})
    by_market: dict = {}
    for sym in symbols:
        mkt, ticker, ccy = _tv_ticker(sym)
        by_market.setdefault(mkt, []).append((sym, ticker, ccy))
    out = {}
    for mkt, entries in by_market.items():
        r = client.post(
            f"https://scanner.tradingview.com/{mkt}/scan",
            json={"filter": [{"left": "name", "operation": "in_range",
                              "right": [t for _, t, _ in entries]}],
                  "columns": ["name", "close", "change", "volume"],
                  "range": [0, len(entries) + 5]})
        r.raise_for_status()
        got = {}
        for row in r.json().get("data", []):
            d = dict(zip(["name", "close", "change", "volume"], row["d"]))
            got[str(d["name"])] = d
        for sym, ticker, ccy in entries:
            d = got.get(ticker)
            if d is None or d.get("close") is None:
                continue
            px, ccy2 = normalize_price(d["close"], ccy)
            out[sym] = {"symbol": sym, "price": px, "currency": ccy2,
                        "stale": False, "series": [],
                        "change_pct": d.get("change"),
                        "volume": d.get("volume"),
                        "previous_close": None, "src": "tradingview"}
    return out


# ── multi-factor discovery scans (screener phase 1) ─────────────────────
# Market-cap sort has no signal — it proposes the same mega-caps forever.
# These three presets are orthogonal: strength, something-is-happening-now,
# and mean-reversion. Each returns WHY a name surfaced, with numbers.
FACTOR_COLUMNS = ["name", "description", "close", "change", "volume",
                  "market_cap_basic", "relative_volume_10d_calc",
                  "price_52_week_high", "price_52_week_low", "Perf.1M",
                  "Perf.W", "RSI", "sector", "Recommend.All"]

SCANS = {
    "momentum": {
        "label": "near 52w high, 1-month strength",
        "filter": [{"left": "Perf.1M", "operation": "greater", "right": 5},
                   {"left": "RSI", "operation": "in_range",
                    "right": [50, 72]}],
        "sort": ("Perf.1M", "desc")},
    "unusual_volume": {
        "label": "unusual volume vs 10-day average",
        "filter": [{"left": "relative_volume_10d_calc", "operation":
                    "greater", "right": 2}],
        "sort": ("relative_volume_10d_calc", "desc")},
    "oversold": {
        "label": "oversold large-cap (mean reversion)",
        "filter": [{"left": "RSI", "operation": "less", "right": 32},
                   {"left": "market_cap_basic", "operation": "greater",
                    "right": 5_000_000_000}],
        "sort": ("market_cap_basic", "desc")},
}


def factor_scan(exchange: str, preset: str, count: int = 12, *,
                min_volume=500_000, min_mcap=1_000_000_000,
                _client=None) -> list[dict]:
    """One scan preset against one exchange. Common stock only — preferred
    lines and depositary receipts (JPM/PM …) are not tradeable theses."""
    if exchange not in MARKETS:
        raise TradingViewError(f"no TradingView mapping for {exchange}")
    if preset not in SCANS:
        raise TradingViewError(f"unknown scan preset {preset!r}")
    market, code = MARKETS[exchange]
    scan = SCANS[preset]
    client = _client or httpx.Client(
        timeout=20, headers={"User-Agent": "Mozilla/5.0"})
    sort_by, sort_order = scan["sort"]
    body = {
        "filter": [
            {"left": "exchange", "operation": "equal", "right": code},
            {"left": "volume", "operation": "greater", "right": min_volume},
            {"left": "market_cap_basic", "operation": "greater",
             "right": min_mcap},
            {"left": "typespecs", "operation": "has", "right": ["common"]},
            *scan["filter"]],
        "columns": FACTOR_COLUMNS,
        "sort": {"sortBy": sort_by, "sortOrder": sort_order},
        "range": [0, count]}
    r = client.post(f"https://scanner.tradingview.com/{market}/scan", json=body)
    r.raise_for_status()
    out = []
    for row in r.json().get("data") or []:
        d = dict(zip(FACTOR_COLUMNS, row.get("d", [])))
        ticker = str(d.get("name") or "")
        if not ticker or "/" in ticker:         # preferred/rights lines
            continue
        hi = d.get("price_52_week_high")
        close = d.get("close")
        off_high = ((close - hi) / hi * 100) if hi and close else None
        out.append({
            "ticker": ticker, "exchange": exchange,
            "name": d.get("description"), "price": close,
            "change_pct": d.get("change"), "volume": d.get("volume"),
            "mcap": d.get("market_cap_basic"),
            "rel_volume": d.get("relative_volume_10d_calc"),
            "off_52w_high_pct": off_high,
            "perf_1m": d.get("Perf.1M"), "perf_w": d.get("Perf.W"),
            "rsi": d.get("RSI"), "sector": d.get("sector"),
            "tv_rating": rating_label(d.get("Recommend.All")),
            "scan": preset, "scan_label": scan["label"]})
    return out


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
