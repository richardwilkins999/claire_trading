"""Market data (DESIGN.md §14) — one module, several sources, one policy.

Order of preference, per symbol and per call:
  1. the keyed provider (Twelve Data, or IBKR when the gateway is up)
  2. TradingView's scanner — no key, covers every desk exchange
  3. Yahoo — LAST RESORT ONLY. It rate-limits this machine aggressively, so
     it is reached for exactly one thing the others cannot do: OHLCV history
     for symbols outside the keyed provider's plan.
FX falls back to the ECB. All Yahoo access lives in this file, so a breakage
stays in one place.

Network is injectable (`_get`) so unit tests run offline.
"""
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


class MarketError(Exception):
    pass


def build_market(env=None) -> "Market":
    """Source chain from configuration: IBKR (licensed, needs the gateway) >
    Twelve Data (keyed API) > Yahoo (free, throttled) > TradingView quotes >
    ECB FX. Configure by adding the env vars to etc/claire.env + restart."""
    import os
    env = env if env is not None else os.environ
    primary = None
    if env.get("IBKR_ENABLED"):
        try:
            from . import ibkr
            primary = ibkr.Client(env.get("IBKR_HOST", "127.0.0.1"),
                                  env.get("IBKR_PORT", 4002),
                                  env.get("IBKR_CLIENT_ID", 17))
        except Exception as e:              # noqa: BLE001 — degrade loudly
            print(f"IBKR enabled but unusable: {e}")
    if primary is None and env.get("TWELVEDATA_API_KEY"):
        from . import twelvedata
        primary = twelvedata.Client(
            env["TWELVEDATA_API_KEY"],
            all_exchanges=bool(env.get("TWELVEDATA_ALL_EXCHANGES")))
    return Market(primary=primary)


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
    """Keyed provider > TradingView > Yahoo, with a last-good cache so a
    throttled source degrades to a stale value rather than an error."""

    def __init__(self, *, _get=None, clock=time.time, cache_ttl=300,
                 tv_quotes=None, fx_fallback=None, primary=None):
        self.clock = clock
        self.cache_ttl = cache_ttl
        self._cache = {}
        self._stale_keys = set()
        self._client = None
        self._get = _get or self._http_get
        self._tv_quotes = tv_quotes         # injected in tests
        self._fx_fallback = fx_fallback
        self._primary = primary             # keyed provider (twelvedata/ibkr)
        self._pace_lock = __import__("threading").Lock()
        self._last_call = 0.0
        self._host_i = 0
        self._primary_calls: list = []      # timestamps, for the rate gate

    PRIMARY_PER_MIN = 7                     # Twelve Data free tier allows 8

    def _primary_ok(self) -> bool:
        """Three analysts researching at once will blow an 8-per-minute quota
        and every one of those calls comes back 429. Check the budget BEFORE
        spending it, and fall through to TradingView while it recovers."""
        if self._primary is None:
            return False
        with self._pace_lock:
            now = time.monotonic()
            self._primary_calls = [t for t in self._primary_calls
                                   if now - t < 60]
            if len(self._primary_calls) >= self.PRIMARY_PER_MIN:
                return False
            self._primary_calls.append(now)
            return True

    MIN_GAP = 0.7                           # be a polite Yahoo client

    def _pace(self):
        with self._pace_lock:
            wait = self._last_call + self.MIN_GAP - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()

    def _http_get(self, url, params=None):
        if self._client is None:
            self._client = httpx.Client(headers=UA, timeout=15,
                                        follow_redirects=True)
        for attempt in (1, 2):
            self._pace()
            r = self._client.get(url, params=params)
            if r.status_code == 429 and attempt == 1:   # rotate host + retry
                self._host_i ^= 1
                url = url.replace("query1.", "query2.") if self._host_i \
                    else url.replace("query2.", "query1.")
                time.sleep(2)
                continue
            r.raise_for_status()
            return r.json()

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
        """Per-symbol source partition: the keyed primary serves what its
        plan covers; everything else rides the free chain. Mixed `src`
        fields in one response are normal and honest."""
        out, rest = {}, list(symbols)
        if self._primary is not None:
            covered = tuple(s for s in symbols
                            if getattr(self._primary, "supports",
                                       lambda s: True)(s))
            if covered and (("pq", covered) in self._cache
                            or self._primary_ok()):
                try:
                    out = dict(self._cached(("pq", covered), 60,
                                            lambda: self._primary.quote(
                                                list(covered))))
                except Exception:           # noqa: BLE001 — fall through
                    out = {}
            rest = [s for s in symbols if s not in out]
        if rest:
            out.update(self._spark_free(rest))
        return out

    def _spark_free(self, symbols: list[str]) -> dict:
        """TradingView first, Yahoo only as the last resort — Yahoo rate-limits
        this machine aggressively and TradingView covers every desk exchange."""
        tv = self._tv_quotes
        if tv is None:
            from . import tradingview
            tv = tradingview.quotes
        out = {}
        try:
            out = dict(self._cached(("tvq", tuple(symbols)), 60,
                                    lambda: tv(symbols)))
        except Exception:                   # noqa: BLE001 — fall through
            pass
        missing = [s for s in symbols if s not in out]
        if not missing:
            return out
        try:
            out.update(self._spark_yahoo(missing))
        except Exception:                   # noqa: BLE001
            pass
        still = [s for s in symbols if s not in out]
        if still:
            raise MarketError(
                f"no quote from any source for {', '.join(still)}")
        return out

    def _spark_yahoo(self, symbols: list[str]) -> dict:
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
            series = d.get("close") if isinstance(d.get("close"), list) else []
            out[sym] = {"symbol": sym, "price": px, "currency": ccy,
                        "stale": stale, "src": "yahoo",
                        "series": [x for x in series if x is not None][-40:],
                        "previous_close": d.get("previousClose")}
        return out

    def quote(self, symbol: str) -> dict:
        return self.spark([symbol])[symbol]

    # ── OHLCV ────────────────────────────────────────────────────────────
    def chart(self, symbol: str, range_="6mo", interval="1d") -> dict:
        key = ("pchart", symbol, range_, interval)
        if self._primary is not None and not symbol.endswith("=X") \
                and not symbol.startswith("^") \
                and getattr(self._primary, "supports", lambda s: True)(symbol) \
                and (key in self._cache or self._primary_ok()):
            try:
                return self._cached(key, 900,
                                    lambda: self._primary.chart(
                                        symbol, range_, interval))
            except Exception:               # noqa: BLE001 — fall through
                pass
        return self._chart_yahoo(symbol, range_, interval)

    def _chart_yahoo(self, symbol: str, range_="6mo", interval="1d") -> dict:
        def fetch():
            return self._get(f"{BASE}/v8/finance/chart/{symbol}",
                             {"range": range_, "interval": interval})
        ttl = 900 if interval == "1d" else 300      # daily bars move slowly
        data = self._cached(("chart", symbol, range_, interval), ttl, fetch)
        try:
            res = data["chart"]["result"][0]
            q = res["indicators"]["quote"][0]
            return {"symbol": symbol, "timestamps": res.get("timestamp", []),
                    "open": q.get("open", []), "high": q.get("high", []),
                    "low": q.get("low", []), "close": q.get("close", []),
                    "volume": q.get("volume", []),
                    "currency": res.get("meta", {}).get("currency"),
                    "src": "yahoo"}
        except (KeyError, IndexError, TypeError) as e:
            raise MarketError(f"chart shape changed for {symbol}: {e}") from e

    # ── search & listings ────────────────────────────────────────────────
    def search(self, q: str) -> list[dict]:
        """Twelve Data's reference search (free tier, worldwide, not billed
        as a quote credit); Yahoo only if that is unavailable."""
        if self._primary is not None and hasattr(self._primary,
                                                 "symbol_search"):
            try:
                return self._cached(("search", q), 600,
                                    lambda: self._primary.symbol_search(q))
            except Exception:                   # noqa: BLE001 — fall through
                pass

        def fetch():
            data = self._get(f"{BASE}/v1/finance/search",
                             {"q": q, "quotesCount": 10, "newsCount": 0})
            return [{"symbol": r.get("symbol"), "name": r.get("shortname"),
                     "exchange": r.get("exchDisp"), "type": r.get("quoteType"),
                     "src": "yahoo"}
                    for r in data.get("quotes", [])]
        return self._cached(("searchy", q), 600, fetch)

    def screener(self, exchange: str, sort="mcap", start=0,
                 count=100) -> dict:
        """Full exchange listing from the TradingView scanner — no key, no
        cookie/crumb dance, and it carries more columns than Yahoo's did."""
        from . import tradingview
        return self._cached(
            ("listing", exchange, sort, start, count), 300,
            lambda: tradingview.listing(exchange, sort, start, count))

    def metrics(self, symbols: list[str]) -> dict:
        """Full trader metric set per symbol: day OHLC, volume vs average,
        52-week range. Keyed provider where its plan covers the symbol,
        TradingView (all exchanges) otherwise."""
        from . import tradingview
        out, rest = {}, list(symbols)
        if self._primary is not None and hasattr(self._primary, "metrics"):
            covered = tuple(s for s in symbols
                            if getattr(self._primary, "supports",
                                       lambda s: True)(s))
            if covered and (("m", covered) in self._cache
                            or self._primary_ok()):
                try:                            # batched: one request for all
                    out = dict(self._cached(
                        ("m", covered), 300,
                        lambda: self._primary.metrics(list(covered))))
                except Exception:               # noqa: BLE001
                    out = {}
            rest = [s for s in symbols if s not in out]
        if rest:
            try:
                out.update(self._cached(("tvm", tuple(rest)), 300,
                                        lambda: tradingview.metrics(rest)))
            except Exception:                   # noqa: BLE001
                pass
        return out

    # ── exchange overview (index + listings), best-effort per §1.7 ───────
    def exchange_metrics(self, exchange: str) -> dict:
        from . import tradingview
        out = {"exchange": exchange, "index": None, "listings": None,
               "src": "tradingview"}
        try:
            out["index"] = self._cached(
                ("idx", exchange), 120,
                lambda: tradingview.index_quote(exchange))
        except Exception as e:                  # noqa: BLE001 — best-effort
            out["index"] = {"error": str(e)[:120]}
        try:
            out["listings"] = self.screener(exchange, count=1)["total"]
        except Exception:                       # noqa: BLE001
            pass
        return out

    # ── FX ───────────────────────────────────────────────────────────────
    def fx(self, from_ccy: str, to_ccy: str) -> Decimal:
        """Live rate via Yahoo's <FROM><TO>=X symbol, cached ~5 min; ECB
        (Frankfurter, keyless) as fallback — daily rates, fine for paper."""
        if from_ccy == to_ccy:
            return Decimal(1)
        pair = f"{from_ccy}{to_ccy}=X"
        if self._primary is not None and (("pfx", pair) in self._cache
                                          or self._primary_ok()):
            try:
                return Decimal(self._cached(
                    ("pfx", pair), self.cache_ttl,
                    lambda: self._primary.fx(from_ccy, to_ccy)))
            except Exception:               # noqa: BLE001 — fall through
                pass

        def fetch():
            c = self.chart(pair, range_="1d", interval="5m")
            closes = [x for x in c["close"] if x is not None]
            if not closes:
                raise MarketError(f"no FX data for {pair}")
            return str(closes[-1])
        try:
            return Decimal(self._cached(("fx", pair), self.cache_ttl, fetch))
        except Exception:                   # noqa: BLE001 — fall back to ECB
            fb = self._fx_fallback or self._frankfurter
            return Decimal(self._cached(("fxecb", pair), 3600,
                                        lambda: fb(from_ccy, to_ccy)))

    def primary_name(self):
        return type(self._primary).__module__.rsplit(".", 1)[-1] \
            if self._primary else None

    def _frankfurter(self, from_ccy, to_ccy) -> str:
        if self._client is None:
            self._client = httpx.Client(headers=UA, timeout=15,
                                        follow_redirects=True)
        r = self._client.get("https://api.frankfurter.app/latest",
                             params={"from": from_ccy, "to": to_ccy})
        r.raise_for_status()
        return str(r.json()["rates"][to_ccy])
