"""Yahoo Finance market data (DESIGN.md §14). ALL Yahoo access lives here —
the endpoints are unofficial; a breakage is one file.

Network is injectable (`_get`) so unit tests run offline; live smoke tests
exercise the real endpoints when services are up.
"""
import json
import time
from decimal import Decimal

import httpx

UA = {"User-Agent": ("Mozilla/5.0 (X11; Linux x86_64; rv:130.0) "
                     "Gecko/20100101 Firefox/130.0")}
BASE = "https://query1.finance.yahoo.com"

# exchange → yahoo suffix (DESIGN.md §14); NASDAQ/NYSE have none
SUFFIX = {"SGX": ".SI", "HKEX": ".HK", "TSE": ".T", "ASX": ".AX",
          "XETRA": ".DE", "PARIS": ".PA", "NSE": ".NS", "LSE": ".L"}
# suffix → native currency; .L is GBp — PENCE, divide by 100 before FX
SUFFIX_CCY = {".SI": "SGD", ".HK": "HKD", ".T": "JPY", ".AX": "AUD",
              ".DE": "EUR", ".PA": "EUR", ".NS": "INR", ".L": "GBp"}
SCREENER_EXCH = {"NASDAQ": "NMS", "NYSE": "NYQ", "LSE": "LSE", "SGX": "SES",
                 "HKEX": "HKG", "TSE": "JPX", "ASX": "ASX", "XETRA": "GER",
                 "PARIS": "PAR", "NSE": "NSI"}
# benchmark index per exchange — the number a trader glances at first
INDEX = {"NASDAQ": "^IXIC", "NYSE": "^NYA", "LSE": "^FTSE", "SGX": "^STI",
         "HKEX": "^HSI", "TSE": "^N225", "ASX": "^AXJO", "XETRA": "^GDAXI",
         "PARIS": "^FCHI", "NSE": "^NSEI"}


class MarketError(Exception):
    pass


def yahoo_symbol(ticker: str, exchange: str) -> str:
    return ticker + SUFFIX.get(exchange, "")


def currency_for(symbol: str) -> str:
    for suf, ccy in SUFFIX_CCY.items():
        if symbol.upper().endswith(suf.upper()):
            return ccy
    return "USD"


def normalize_price(price, currency: str):
    """GBp → GBP: LSE quotes are in PENCE (§14) — forget this and every UK
    position is wrong by 100×."""
    if currency == "GBp":
        return (Decimal(str(price)) / 100, "GBP")
    return Decimal(str(price)), currency


class Market:
    def __init__(self, *, _get=None, clock=time.time, cache_ttl=300):
        self.clock = clock
        self.cache_ttl = cache_ttl
        self._cache = {}
        self._stale_keys = set()
        self._crumb = None
        self._client = None
        self._get = _get or self._http_get

    def _http_get(self, url, params=None, need_crumb=False):
        if self._client is None:
            self._client = httpx.Client(headers=UA, timeout=15,
                                        follow_redirects=True)
        if need_crumb:
            params = dict(params or {}, crumb=self._get_crumb())
        for attempt in (1, 2):
            r = self._client.get(url, params=params)
            if r.status_code == 429 and attempt == 1:   # unofficial endpoints
                time.sleep(2)                           # rate-limit: one retry
                continue
            r.raise_for_status()
            return r.json()

    def _get_crumb(self):
        """The screener cookie+crumb dance (§14)."""
        if self._crumb:
            return self._crumb
        self._client.get("https://finance.yahoo.com/", headers=UA)
        r = self._client.get(f"{BASE}/v1/test/getcrumb", headers=UA)
        self._crumb = r.text.strip()
        return self._crumb

    def _cached(self, key, ttl, fn):
        """TTL cache with a last-good fallback: when the unofficial endpoints
        rate-limit or hiccup, serve the previous value marked stale rather
        than erroring the UI (honest degradation, §1.7)."""
        now = self.clock()
        hit = self._cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
        try:
            val = fn()
        except Exception:
            if hit is not None:
                self._stale_keys.add(key)
                return hit[1]
            raise
        self._cache[key] = (now, val)
        self._stale_keys.discard(key)
        return val

    # ── quotes ───────────────────────────────────────────────────────────
    def spark(self, symbols: list[str]) -> dict:
        """Batch quotes. The v8 spark response is a FLAT dict
        {SYM: {close, previousClose, ...}} — not the nested spark.result shape
        older docs suggest. When a market is closed `close` can be null →
        fall back to previousClose."""
        def fetch():
            return self._get(f"{BASE}/v8/finance/spark",
                             {"symbols": ",".join(symbols), "range": "1d",
                              "interval": "5m"})
        data = self._cached(("spark", tuple(symbols)), 60, fetch)
        out = {}
        for sym in symbols:
            d = data.get(sym) or {}
            price = d.get("close")
            if isinstance(price, list):             # some variants return series
                price = next((p for p in reversed(price) if p is not None), None)
            stale = price is None
            if stale:
                price = d.get("previousClose")
            if price is None:
                raise MarketError(f"no price for {sym}")
            ccy = d.get("currency") or currency_for(sym)
            px, ccy = normalize_price(price, ccy)
            out[sym] = {"symbol": sym, "price": px, "currency": ccy,
                        "stale": stale,
                        "previous_close": d.get("previousClose")}
        return out

    def quote(self, symbol: str) -> dict:
        return self.spark([symbol])[symbol]

    # ── OHLCV ────────────────────────────────────────────────────────────
    def chart(self, symbol: str, range_="6mo", interval="1d") -> dict:
        def fetch():
            return self._get(f"{BASE}/v8/finance/chart/{symbol}",
                             {"range": range_, "interval": interval})
        data = self._cached(("chart", symbol, range_, interval), 300, fetch)
        try:
            res = data["chart"]["result"][0]
            q = res["indicators"]["quote"][0]
            return {"symbol": symbol, "timestamps": res.get("timestamp", []),
                    "open": q.get("open", []), "high": q.get("high", []),
                    "low": q.get("low", []), "close": q.get("close", []),
                    "volume": q.get("volume", []),
                    "currency": res.get("meta", {}).get("currency")}
        except (KeyError, IndexError, TypeError) as e:
            raise MarketError(f"chart shape changed for {symbol}: {e}") from e

    # ── search & screener ────────────────────────────────────────────────
    def search(self, q: str) -> list[dict]:
        data = self._get(f"{BASE}/v1/finance/search",
                         {"q": q, "quotesCount": 10, "newsCount": 0})
        return [{"symbol": r.get("symbol"), "name": r.get("shortname"),
                 "exchange": r.get("exchDisp"), "type": r.get("quoteType")}
                for r in data.get("quotes", [])]

    def screener(self, exchange: str, sort="intradaymarketcap", start=0,
                 count=100) -> dict:
        code = SCREENER_EXCH.get(exchange)
        if not code:
            raise MarketError(f"no screener code for {exchange}")

        def fetch():
            body = {"size": min(count, 250), "offset": start,
                    "sortField": sort, "sortType": "DESC",
                    "quoteType": "EQUITY",
                    "query": {"operator": "EQ",
                              "operands": ["exchange", code]}}
            if self._client is None:
                self._client = httpx.Client(headers=UA, timeout=20,
                                            follow_redirects=True)
            r = self._client.post(
                f"{BASE}/v1/finance/screener",
                params={"crumb": self._get_crumb()}, json=body)
            r.raise_for_status()
            return r.json()
        data = self._cached(("scr", exchange, sort, start, count), 600, fetch)

        def num(r, key):
            v = r.get(key)
            return v.get("raw") if isinstance(v, dict) else v
        try:
            res = data["finance"]["result"][0]
            rows = [{"symbol": r.get("symbol"), "name": r.get("shortName"),
                     "price": num(r, "regularMarketPrice"),
                     "change_pct": num(r, "regularMarketChangePercent"),
                     "volume": num(r, "regularMarketVolume"),
                     "mcap": num(r, "marketCap"),
                     "pe": num(r, "trailingPE")}
                    for r in res.get("quotes", [])]
            return {"total": res.get("total"), "rows": rows}
        except (KeyError, IndexError, TypeError) as e:
            raise MarketError(f"screener shape changed: {e}") from e

    # ── exchange overview (index + breadth), best-effort per §1.7 ────────
    def exchange_metrics(self, exchange: str) -> dict:
        out = {"exchange": exchange, "index": None, "listings": None}
        idx = INDEX.get(exchange)
        if idx:
            try:
                c = self.chart(idx, range_="1d", interval="5m")
                closes = [x for x in c["close"] if x is not None]
                vols = [v for v in c["volume"] if v]
                prev = None
                try:
                    d = self.chart(idx, range_="5d", interval="1d")
                    dc = [x for x in d["close"] if x is not None]
                    prev = dc[-2] if len(dc) >= 2 else None
                except MarketError:
                    pass
                if closes:
                    last = closes[-1]
                    out["index"] = {
                        "symbol": idx, "level": round(last, 2),
                        "change_pct": round((last - prev) / prev * 100, 2)
                        if prev else None,
                        "day_volume": sum(vols) or None}
            except Exception as e:              # noqa: BLE001 — best-effort
                out["index"] = {"symbol": idx, "error": str(e)[:120]}
        try:
            out["listings"] = self.screener(exchange, count=1)["total"]
        except Exception:                       # noqa: BLE001
            pass
        return out

    # ── FX ───────────────────────────────────────────────────────────────
    def fx(self, from_ccy: str, to_ccy: str) -> Decimal:
        """Live rate via the <FROM><TO>=X chart symbol, cached ~5 min."""
        if from_ccy == to_ccy:
            return Decimal(1)
        pair = f"{from_ccy}{to_ccy}=X"

        def fetch():
            c = self.chart(pair, range_="1d", interval="5m")
            closes = [x for x in c["close"] if x is not None]
            if not closes:
                raise MarketError(f"no FX data for {pair}")
            return str(closes[-1])
        return Decimal(self._cached(("fx", pair), self.cache_ttl, fetch))
