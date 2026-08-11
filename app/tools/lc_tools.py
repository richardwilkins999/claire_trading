"""LangChain tool objects over the tools layer (DESIGN.md §9). Built per
work-item so narrative writes land in the right jail directory. The roster an
agent actually receives comes from its `agents.tools` JSON."""
import json

from langchain_core.tools import tool

from .market import yahoo_symbol
from .sandbox import run_python as _run_python
from .search import fetch_page as _fetch_page
from .search import web_search as _web_search


MAX_TOOL_FAILURES = 3

# Whatever a tool returns is re-sent on every later step of the research loop,
# so an oversized result is not paid for once — it is paid for once per
# remaining step. These caps are deliberately far below the old 20-40k.
MAX_RESULT_CHARS = 6000
CHART_TAIL = 60                          # closes the model actually reads


def summarise_chart(c: dict, tail: int = CHART_TAIL) -> dict:
    """A year of OHLCV as raw arrays is ~4,400 tokens of timestamps and
    six-decimal floats, and the model only ever uses the derived numbers.
    Hand back the indicators plus a short tail of closes — same analytical
    value, roughly a twenty-fifth of the tokens. run_python still gets the
    full series server-side, so nothing is actually lost."""
    close = [x for x in (c.get("close") or []) if x is not None]
    if not close:
        return c
    high = [x for x in (c.get("high") or []) if x is not None] or close
    low = [x for x in (c.get("low") or []) if x is not None] or close
    vol = [x for x in (c.get("volume") or []) if x is not None]

    def sma(n):
        return round(sum(close[-n:]) / n, 4) if len(close) >= n else None
    return {
        "symbol": c.get("symbol"), "currency": c.get("currency"),
        "src": c.get("src"), "bars": len(close),
        "range": c.get("range"), "interval": c.get("interval"),
        "last": round(close[-1], 4),
        "first": round(close[0], 4),
        "change_pct": round((close[-1] / close[0] - 1) * 100, 2),
        "high_period": round(max(high), 4), "low_period": round(min(low), 4),
        "sma20": sma(20), "sma50": sma(50), "sma200": sma(200),
        "avg_volume": int(sum(vol) / len(vol)) if vol else None,
        "last_volume": vol[-1] if vol else None,
        f"last_{tail}_close": [round(x, 4) for x in close[-tail:]],
        "note": f"Summary of {len(close)} bars. For anything needing the full "
                f"series (regressions, custom indicators), use run_python — it "
                f"loads every bar into DATA server-side.",
    }


def _safe(fn, failures=None):
    """A tool that raises kills the whole agent node. A blocked page (403),
    a dead link (404) or a throttled feed is normal weather on the open web —
    hand the failure BACK to the model as text so it can try another route.

    But returning errors turned hard failures into retry loops (SBUX burned
    its whole step budget that way), so after a few failures the same tool
    stops being an option and says so.
    """
    import functools

    @functools.wraps(fn)
    def wrapper(*a, **kw):
        seen = failures if failures is not None else {}
        name = fn.__name__
        if seen.get(name, 0) >= MAX_TOOL_FAILURES:
            return json.dumps({
                "error": f"{name} has failed {MAX_TOOL_FAILURES} times and is "
                         f"now disabled for this run",
                "hint": "STOP calling this tool. Use another source, or "
                        "finish your analysis with what you already have and "
                        "state plainly what is missing."})
        try:
            out = fn(*a, **kw)
        except Exception as e:              # noqa: BLE001 — deliberate
            seen[name] = seen.get(name, 0) + 1
            left = MAX_TOOL_FAILURES - seen[name]
            return json.dumps({
                "error": f"{type(e).__name__}: {str(e)[:300]}",
                "hint": f"this source failed ({left} attempt(s) left before "
                        f"it is disabled) — try a DIFFERENT source or tool; "
                        f"do not retry it unchanged"})
        # An empty result is not an exception, so the breaker never saw it —
        # and DuckDuckGo returns empty far more often than it raises. Each
        # barren call still costs a full re-send of the transcript on every
        # step that follows, so it has to count against the same budget.
        if _is_barren(out):
            seen[name] = seen.get(name, 0) + 1
            left = MAX_TOOL_FAILURES - seen[name]
            return json.dumps({
                "result": "empty",
                "hint": f"{name} returned nothing ({left} attempt(s) left "
                        f"before it is disabled). Rephrasing the same query "
                        f"rarely helps — change source, or proceed and say "
                        f"plainly what you could not find."})
        return out
    return wrapper


def _is_barren(out) -> bool:
    """True for a result that carries no information — [] or {} or blank."""
    if out is None:
        return True
    s = str(out).strip()
    if not s or s in ("[]", "{}", "null", '""'):
        return True
    try:
        v = json.loads(s)
    except (ValueError, TypeError):
        return False
    return isinstance(v, (list, dict, str)) and len(v) == 0


def build_tool_registry(market, narratives, env=None):
    """Returns tool_builder(names, state) -> [BaseTool] for analyst agents."""

    def tool_builder(names, state):
        wi = state.work_item_id
        failures = {}                    # per-run, shared by every tool below

        def _safe_run(fn):
            return _safe(fn, failures)

        @tool
        @_safe_run
        def market_quote(symbol: str) -> str:
            """Live quote for a Yahoo symbol (e.g. NVDA, C07.SI)."""
            q = market.quote(symbol)
            return json.dumps(q, default=str)

        @tool
        @_safe_run
        def market_chart(symbol: str, range_: str = "6mo",
                         interval: str = "1d") -> str:
            """Price history for a symbol, summarised: moving averages,
            period high/low, average volume and the last 60 closes.
            range_: 1mo|3mo|6mo|1y|2y. For the full bar-by-bar series use
            run_python, which loads it server-side."""
            c = market.chart(symbol, range_, interval)
            return json.dumps(summarise_chart(c),
                              default=str)[:MAX_RESULT_CHARS]

        @tool
        @_safe_run
        def market_search(query: str) -> str:
            """Worldwide symbol search by company name or ticker."""
            return json.dumps(market.search(query),
                              default=str)[:MAX_RESULT_CHARS]

        @tool
        @_safe_run
        def market_screener(exchange: str, sort: str = "intradaymarketcap",
                            start: int = 0) -> str:
            """Top listings for an exchange (NASDAQ, NYSE, LSE, SGX, HKEX,
            TSE, ASX, XETRA, PARIS, NSE), server-side sorted."""
            return json.dumps(market.screener(exchange, sort, start),
                              default=str)[:MAX_RESULT_CHARS]

        @tool
        @_safe_run
        def web_search(query: str) -> str:
            """Search the web (Tavily if configured, else DuckDuckGo)."""
            return json.dumps(_web_search(query, env=env or {}),
                              default=str)[:MAX_RESULT_CHARS]

        @tool
        @_safe_run
        def fetch_page(url: str) -> str:
            """Fetch a web page as readable text (size-capped)."""
            return _fetch_page(url, max_chars=MAX_RESULT_CHARS)

        @tool
        @_safe_run
        def run_python(code: str, symbol: str = "", range_: str = "6mo") -> str:
            """Run numpy/pandas analysis code in a sandbox. The tool fetches
            OHLCV for `symbol` into DATA (dict of open/high/low/close/volume
            lists); your code must set RESULT. No network, no secrets."""
            data = {}
            if symbol:
                data = {k: v for k, v in market.chart(symbol, range_).items()}
            return json.dumps(_run_python(code, data),
                              default=str)[:MAX_RESULT_CHARS]

        @tool
        @_safe_run
        def write_narrative(name: str, markdown: str) -> str:
            """Save your full written analysis (markdown) to the run's
            narrative folder. Returns the stored path."""
            return narratives.write(wi, name, markdown)

        registry = {t.name: t for t in [
            market_quote, market_chart, market_search, market_screener,
            web_search, fetch_page, run_python, write_narrative]}
        return [registry[n] for n in names if n in registry]

    return tool_builder


def default_symbol(state) -> str:
    return yahoo_symbol(state.instrument.ticker, state.instrument.exchange)
