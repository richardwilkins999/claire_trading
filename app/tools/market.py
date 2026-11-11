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
    fallback = saxo_search = None
    if not env.get("CLAIRE_NO_SAXO_CHARTS"):
        from . import saxo_chart
        fallback, saxo_search = saxo_chart.chart, saxo_chart.search
    return Market(primary=primary, chart_fallback=fallback,
                  search_fallback=combined_search(saxo_search))


def combined_search(saxo_search):
    """Saxo's hits first (instruments Saxo can trade, marked so), then
    TradingView's for the desk's other listings (SGX, HKEX… that Saxo SIM
    does not offer as shares). Either may fail; both failing raises."""
    from . import tradingview

    def search(q):
        out, seen, errors = [], set(), []
        for name, fn in (("saxo", saxo_search), ("tradingview",
                                                 tradingview.search)):
            if fn is None:
                continue
            try:
                for r in fn(q):
                    if r["symbol"] not in seen:
                        seen.add(r["symbol"])
                        r["at_saxo"] = name == "saxo"
                        out.append(r)
            except Exception as e:              # noqa: BLE001 — try the next
                errors.append(f"{name}: {str(e)[:80]}")
        if not out and errors:
            raise MarketError("search unavailable — " + "; ".join(errors))
        return out
    return search


def yahoo_symbol(ticker: str, exchange: str) -> str:
    suf = SUFFIX.get(exchange, "")
    if suf and ticker.upper().endswith(suf.upper()):
        return ticker              # already carries it ("APC.DE" on XETRA)
    return ticker + suf


def desk_ticker(ticker: str, exchange: str) -> str:
    """The desk's own ticker: no Yahoo suffix (XETRA "APC", not "APC.DE").
    A suffixed one was stored from search results and then got the suffix
    twice ("APC.DE.DE"), so it never priced (2026-11-06)."""
    suf = SUFFIX.get(exchange, "")
    if suf and ticker.upper().endswith(suf.upper()) and len(ticker) > len(suf):
        return ticker[:-len(suf)]
    return ticker


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


def _mark_stale(val):
    """Copy of a cached value flagged stale. Per-symbol maps get the flag on
    every record; a single record gets it at top level. Scalars and lists
    cannot carry a flag and are returned as-is — the key is still listed in
    Market._stale_keys for the health view. (FX rates no longer come this
    way: Market.fx applies its own age limit, FX_MAX_AGE.)"""
    if isinstance(val, dict) and val:
        if all(isinstance(v, dict) for v in val.values()):
            return {k: {**v, "stale": True} for k, v in val.items()}
        return {**val, "stale": True}
    return val


class Market:
    """Keyed provider > TradingView > Yahoo, with a last-good cache so a
    throttled source degrades to a stale value rather than an error."""

    def __init__(self, *, _get=None, clock=time.time, cache_ttl=300,
                 tv_quotes=None, fx_fallback=None, primary=None,
                 chart_fallback=None, search_fallback=None):
        self.clock = clock
        self._search_fallback = search_fallback
        self.cache_ttl = cache_ttl
        self._cache = {}
        self._stale_keys = set()
        self._client = None
        self._get = _get or self._http_get
        self._tv_quotes = tv_quotes         # injected in tests
        self._fx_fallback = fx_fallback
        self._primary = primary             # keyed provider (twelvedata/ibkr)
        self._chart_fallback = chart_fallback   # Saxo bars when Yahoo refuses
        self._pace_lock = __import__("threading").Lock()
        self._last_call = 0.0
        self._host_i = 0
        self._primary_calls: list = []      # timestamps, for the rate gate
        self._yahoo_quiet_until = 0.0       # set when Yahoo refuses us

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

    YAHOO_QUIET = 300                       # s: back off after a refusal

    def _http_get(self, url, params=None):
        """Yahoo. After it refuses us (429 twice) we stop asking for a few
        minutes — every further call would be refused too, and each one
        cost ~3.5 s of a page load (the Watchlist took ~20 s)."""
        wait = self._yahoo_quiet_until - time.monotonic()
        if wait > 0:
            raise MarketError(f"Yahoo is rate-limiting this machine — not "
                              f"asking again for {int(wait // 60) + 1} min")
        try:
            return self._http_get_once(url, params)
        except httpx.HTTPStatusError as e:
            if e.response is not None and e.response.status_code == 429:
                self._yahoo_quiet_until = time.monotonic() + self.YAHOO_QUIET
            raise

    def _http_get_once(self, url, params=None):
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

    def _fresh(self, key, ttl) -> bool:
        hit = self._cache.get(key)
        return hit is not None and self.clock() - hit[0] < ttl

    def _primary_gate(self, key, ttl) -> bool:
        """Only a FRESH cache hit may skip the rate budget; an expired entry
        means a real call to the keyed provider, so it must be budgeted."""
        return self._fresh(key, ttl) or self._primary_ok()

    def _cached_flag(self, key, ttl, fn):
        """TTL cache with a last-good fallback: when the unofficial endpoints
        rate-limit or hiccup, serve the previous value rather than erroring
        the UI (honest degradation, §1.7). Returns (value, is_stale)."""
        now = self.clock()
        hit = self._cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1], False
        try:
            val = fn()
        except Exception:
            if hit is not None:
                self._stale_keys.add(key)
                return hit[1], True
            raise
        self._cache[key] = (now, val)
        self._stale_keys.discard(key)
        return val, False

    def _cached(self, key, ttl, fn):
        """As _cached_flag, but a stale fallback is returned visibly marked
        `stale: True` (on each per-symbol record of a quote/metric map, or on
        the record itself) — never silently passed off as fresh."""
        val, stale = self._cached_flag(key, ttl, fn)
        return _mark_stale(val) if stale else val

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
            if covered and self._primary_gate(("pq", covered), 60):
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
        data, cache_stale = self._cached_flag(("spark", tuple(symbols)), 60,
                                              fetch)
        out = {}
        for sym in symbols:
            d = data.get(sym) or {}
            price = d.get("close")
            if isinstance(price, list):             # some variants return series
                price = next((p for p in reversed(price) if p is not None), None)
            stale = price is None or cache_stale
            if price is None:
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
                and self._primary_gate(key, 900):
            try:
                return self._cached(key, 900,
                                    lambda: self._primary.chart(
                                        symbol, range_, interval))
            except Exception:               # noqa: BLE001 — fall through
                pass
        fb = ("fbchart", symbol, range_, interval)
        if self._chart_fallback is not None and \
                self._fresh(fb, 900 if interval == "1d" else 300):
            return self._cache[fb][1]       # recent Saxo bars: no Yahoo trip
        try:
            return self._chart_yahoo(symbol, range_, interval)
        except Exception as yahoo_err:
            if self._chart_fallback is None or symbol.endswith("=X") \
                    or symbol.startswith("^"):
                raise
            try:
                return self._cached(fb, 900 if interval == "1d" else 300,
                                    lambda: self._chart_fallback(
                                        symbol, range_, interval))
            except Exception as e:
                raise MarketError(f"no chart for {symbol}: Yahoo "
                                  f"{str(yahoo_err)[:80]}; Saxo {e}") from e

    def _chart_yahoo(self, symbol: str, range_="6mo", interval="1d") -> dict:
        def fetch():
            return self._get(f"{BASE}/v8/finance/chart/{symbol}",
                             {"range": range_, "interval": interval})
        ttl = 900 if interval == "1d" else 300      # daily bars move slowly
        data, stale = self._cached_flag(("chart", symbol, range_, interval),
                                        ttl, fetch)
        try:
            res = data["chart"]["result"][0]
            q = res["indicators"]["quote"][0]
            return {"symbol": symbol, "timestamps": res.get("timestamp", []),
                    "open": q.get("open", []), "high": q.get("high", []),
                    "low": q.get("low", []), "close": q.get("close", []),
                    "volume": q.get("volume", []),
                    "currency": res.get("meta", {}).get("currency"),
                    "src": "yahoo", "stale": stale}
        except (KeyError, IndexError, TypeError) as e:
            raise MarketError(f"chart shape changed for {symbol}: {e}") from e

    # ── search & listings ────────────────────────────────────────────────
    def search(self, q: str) -> list[dict]:
        """Twelve Data's reference search (free tier, worldwide, not billed
        as a quote credit); else Saxo's instrument search (the desk's SIM
        sign-in — and only instruments Saxo can trade); Yahoo last, as it
        rate-limits (429) unofficial callers."""
        if self._primary is not None and hasattr(self._primary,
                                                 "symbol_search"):
            try:
                return self._cached(("search", q), 600,
                                    lambda: self._primary.symbol_search(q))
            except Exception:                   # noqa: BLE001 — fall through
                pass
        if self._search_fallback is not None:
            try:
                hits = self._cached(("searchs", q), 600,
                                    lambda: self._search_fallback(q))
                if hits:
                    return hits
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
            if covered and self._primary_gate(("m", covered), 300):
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
    # How long a last-good FX rate may stand in once EVERY source is failing.
    # A rate is a bare number: it cannot carry a `stale` flag the way a quote
    # does, and it is written into immutable execution rows, order sizing
    # and the watcher's floor. Past this age fx() raises instead, and each
    # caller's own "no rate" policy applies (a fill waits for the next run).
    FX_MAX_AGE = 4 * 3600

    def fx(self, from_ccy: str, to_ccy: str) -> Decimal:
        """Keyed provider, then Yahoo's <FROM><TO>=X chart (both cached
        ~5 min), then the ECB (Frankfurter, keyless; daily rates, cached 1 h).

        Every source is asked for a FRESH rate before any old one is
        reused. It used to return the first source's last-good value on a
        failure — with no age limit, and without trying the next source."""
        if from_ccy == to_ccy:
            return Decimal(1)
        pair = f"{from_ccy}{to_ccy}=X"

        def yahoo():
            c = self.chart(pair, range_="1d", interval="5m")
            if c.get("stale"):      # chart()'s own last-good fallback: not
                raise MarketError(f"only a stale chart for {pair}")  # fresh
            closes = [x for x in c["close"] if x is not None]
            if not closes:
                raise MarketError(f"no FX data for {pair}")
            return str(closes[-1])

        def ecb():
            return (self._fx_fallback or self._frankfurter)(from_ccy, to_ccy)

        sources = [(("fx", pair), self.cache_ttl, yahoo),
                   (("fxecb", pair), 3600, ecb)]
        if self._primary is not None:
            sources.insert(0, (("pfx", pair), self.cache_ttl,
                               lambda: self._primary.fx(from_ccy, to_ccy)))
        errors = []
        for key, ttl, fn in sources:
            if key[0] == "pfx" and not self._primary_gate(key, ttl):
                continue                    # over the keyed provider's budget
            now = self.clock()
            hit = self._cache.get(key)
            if hit and now - hit[0] < ttl:
                return Decimal(hit[1])
            try:
                val = fn()
            except Exception as e:          # noqa: BLE001 — try the next one
                errors.append(f"{key[0]}: {str(e)[:60]}")
                continue
            self._cache[key] = (now, val)
            self._stale_keys.discard(("fx", pair))
            return Decimal(val)
        # every source failed: the newest last-good rate, if recent enough
        now = self.clock()
        hits = [self._cache[k] for k, _, _ in sources if k in self._cache]
        if hits:
            ts, val = max(hits, key=lambda h: h[0])
            if now - ts <= self.FX_MAX_AGE:
                self._stale_keys.add(("fx", pair))      # for the health view
                return Decimal(val)
            errors.append(f"last good rate is {int((now - ts) // 60)} min "
                          f"old (limit {self.FX_MAX_AGE // 60})")
        raise MarketError(f"no {from_ccy}→{to_ccy} rate — "
                          + "; ".join(errors))

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
