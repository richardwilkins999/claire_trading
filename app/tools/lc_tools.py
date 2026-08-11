"""LangChain tool objects over the tools layer (DESIGN.md §9). Built per
work-item so narrative writes land in the right jail directory. The roster an
agent actually receives comes from its `agents.tools` JSON."""
import json

from langchain_core.tools import tool

from .market import yahoo_symbol
from .sandbox import run_python as _run_python
from .search import fetch_page as _fetch_page
from .search import web_search as _web_search


def _safe(fn):
    """A tool that raises kills the whole agent node. A blocked page (403),
    a dead link (404) or a throttled feed is normal weather on the open web —
    hand the failure BACK to the model as text so it can try another route."""
    import functools

    @functools.wraps(fn)
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except Exception as e:              # noqa: BLE001 — deliberate
            return json.dumps({
                "error": f"{type(e).__name__}: {str(e)[:300]}",
                "hint": "this source failed — try a different source, "
                        "symbol or tool; do not retry it unchanged"})
    return wrapper


def build_tool_registry(market, narratives, env=None):
    """Returns tool_builder(names, state) -> [BaseTool] for analyst agents."""

    def tool_builder(names, state):
        wi = state.work_item_id

        @tool
        @_safe
        def market_quote(symbol: str) -> str:
            """Live quote for a Yahoo symbol (e.g. NVDA, C07.SI)."""
            q = market.quote(symbol)
            return json.dumps(q, default=str)

        @tool
        @_safe
        def market_chart(symbol: str, range_: str = "6mo",
                         interval: str = "1d") -> str:
            """OHLCV history for a Yahoo symbol. range_: 1mo|3mo|6mo|1y|2y."""
            c = market.chart(symbol, range_, interval)
            return json.dumps(c, default=str)[:40000]

        @tool
        @_safe
        def market_search(query: str) -> str:
            """Worldwide symbol search by company name or ticker."""
            return json.dumps(market.search(query), default=str)

        @tool
        @_safe
        def market_screener(exchange: str, sort: str = "intradaymarketcap",
                            start: int = 0) -> str:
            """Top listings for an exchange (NASDAQ, NYSE, LSE, SGX, HKEX,
            TSE, ASX, XETRA, PARIS, NSE), server-side sorted."""
            return json.dumps(market.screener(exchange, sort, start),
                              default=str)[:40000]

        @tool
        @_safe
        def web_search(query: str) -> str:
            """Search the web (Tavily if configured, else DuckDuckGo)."""
            return json.dumps(_web_search(query, env=env or {}), default=str)

        @tool
        @_safe
        def fetch_page(url: str) -> str:
            """Fetch a web page as readable text (size-capped)."""
            return _fetch_page(url)

        @tool
        @_safe
        def run_python(code: str, symbol: str = "", range_: str = "6mo") -> str:
            """Run numpy/pandas analysis code in a sandbox. The tool fetches
            OHLCV for `symbol` into DATA (dict of open/high/low/close/volume
            lists); your code must set RESULT. No network, no secrets."""
            data = {}
            if symbol:
                data = {k: v for k, v in market.chart(symbol, range_).items()}
            return json.dumps(_run_python(code, data), default=str)[:20000]

        @tool
        @_safe
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
