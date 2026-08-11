"""Regressions for the token-usage audit: 93% of every token bought was
input, and the three analysts were 90% of the bill."""
import json

from app.accounting import db
from app.providers import registry, seeds
from app.tools import lc_tools
from app.tools.lc_tools import (MAX_RESULT_CHARS, MAX_TOOL_FAILURES,
                                build_tool_registry, summarise_chart)

T0 = 1_786_456_800


def _conn():
    c = db.connect(":memory:")
    db.init(c)
    seeds.seed(c, ts=T0)
    return c


# 1 ── the cache marker has to actually reach the request body
def test_anthropic_requests_ask_for_prompt_caching():
    llm = registry.build_llm(_conn(), "anthropic", "claude-sonnet-5",
                             env={"ANTHROPIC_API_KEY": "sk-test"})
    assert llm.model_kwargs["cache_control"] == {"type": "ephemeral"}


# 2 ── price is per model, and cached tokens are billed at their own rate
def test_per_model_pricing_and_cache_rates():
    conn = _conn()
    assert registry.price_for(conn, "anthropic", "claude-haiku-4-5") \
        == (0.001, 0.005)
    assert registry.price_for(conn, "anthropic", "claude-opus-5") \
        == (0.005, 0.025)
    # an unknown model still falls back rather than crashing a live run
    assert registry.price_for(conn, "anthropic", "made-up-model")

    # a per-model row overrides the published table
    conn.execute("INSERT INTO provider_models (provider_id, model,"
                 " cost_per_1k_in, cost_per_1k_out) VALUES"
                 " ('anthropic','claude-opus-5',0.009,0.045)")
    assert registry.price_for(conn, "anthropic", "claude-opus-5") \
        == (0.009, 0.045)


def test_cached_tokens_are_not_billed_twice():
    """langchain reports input_tokens INCLUDING cache; Anthropic reports it
    excluding. Both must produce the same, correct bill."""
    conn = _conn()
    registry.assign(conn, "news", "anthropic", "claude-haiku-4-5",
                    ts=T0, require_health=False)
    rate_in, rate_out = 0.001, 0.005
    expect = (1000 / 1000 * rate_in                       # fresh
              + 4000 / 1000 * rate_in * registry.CACHE_WRITE_MULTIPLIER
              + 18000 / 1000 * rate_in * registry.CACHE_READ_MULTIPLIER
              + 300 / 1000 * rate_out)

    class Resp:
        llm_output = None

        def __init__(self, usage):
            class M:
                usage_metadata = usage

            class G:
                message = M()
            self.generations = [[G()]]

    for usage in (
        # langchain shape — input_tokens is the TOTAL
        {"input_tokens": 23000, "output_tokens": 300,
         "input_token_details": {"cache_creation": 4000, "cache_read": 18000}},
        # raw Anthropic shape — input_tokens is the uncached remainder
        {"input_tokens": 1000, "output_tokens": 300,
         "cache_creation_input_tokens": 4000,
         "cache_read_input_tokens": 18000},
    ):
        m = registry.MeterCallback(conn, "news", "wi_c")
        m.on_llm_start({}, [])
        m.on_llm_end(Resp(usage))
        row = conn.execute("SELECT tokens_in, cache_write_tokens,"
                           " cache_read_tokens, cost_usd FROM agent_runs"
                           " WHERE id=?", (m.run_id,)).fetchone()
        assert row["tokens_in"] == 23000        # true prompt size, both ways
        assert row["cache_write_tokens"] == 4000
        assert row["cache_read_tokens"] == 18000
        assert abs(row["cost_usd"] - expect) < 1e-9
    # and caching is worth having: same tokens uncached would cost far more
    uncached = 23000 / 1000 * rate_in + 300 / 1000 * rate_out
    assert expect < uncached * 0.5


# 3 ── a year of bars must not enter the conversation as raw arrays
def test_chart_summary_keeps_the_analysis_and_drops_the_bulk():
    n = 252
    raw = {"symbol": "SBUX", "currency": "USD", "src": "yahoo",
           "timestamps": [1753833600 + i * 86400 for i in range(n)],
           "open": [100 + i * 0.01 for i in range(n)],
           "high": [107.5 + i * 0.01 for i in range(n)],
           "low": [95.25 + i * 0.01 for i in range(n)],
           "close": [100 + i * 0.02 for i in range(n)],
           "volume": [7_000_000 + i for i in range(n)]}
    s = summarise_chart(raw)
    assert s["bars"] == n
    assert s["last"] == 105.02 and s["first"] == 100.0
    assert s["high_period"] == 110.01 and s["low_period"] == 95.25
    assert s["sma20"] and s["sma50"] and s["sma200"]
    assert len(s["last_60_close"]) == 60
    assert s["avg_volume"] == 7_000_125
    assert "run_python" in s["note"]            # the full series is still there
    assert len(json.dumps(s)) < len(json.dumps(raw)) / 8
    # a chart with no usable closes is passed through, not mangled
    assert summarise_chart({"symbol": "X", "close": []}) == {"symbol": "X",
                                                             "close": []}


def test_tool_results_are_capped():
    class M:
        def chart(self, *a, **k):
            n = 3000
            return {"symbol": "X", "currency": "USD",
                    "close": [1.0 + i for i in range(n)],
                    "high": [2.0 + i for i in range(n)],
                    "low": [0.5 + i for i in range(n)],
                    "volume": [10 + i for i in range(n)]}

    class St:
        work_item_id = "wi_cap"
    tools = {t.name: t for t in
             build_tool_registry(M(), None)(["market_chart"], St())}
    out = tools["market_chart"].invoke({"symbol": "X"})
    assert len(out) <= MAX_RESULT_CHARS
    assert json.loads(out)["bars"] == 3000      # still truthful about size


# 4 ── an empty result costs a full context re-send, so it must count
def test_empty_results_trip_the_breaker():
    class M:
        calls = 0

        def search(self, q):
            M.calls += 1
            return []                           # what DuckDuckGo kept doing

    class St:
        work_item_id = "wi_e"
    tools = {t.name: t for t in
             build_tool_registry(M(), None)(["market_search"], St())}
    seen = [json.loads(tools["market_search"].invoke({"query": "sbux"}))
            for _ in range(MAX_TOOL_FAILURES + 1)]
    assert all(s.get("result") == "empty" for s in seen[:MAX_TOOL_FAILURES])
    assert "disabled" in seen[MAX_TOOL_FAILURES]["error"]
    # the tool stopped being called once disabled — that is the saving
    assert M.calls == MAX_TOOL_FAILURES


def test_real_results_are_untouched():
    assert not lc_tools._is_barren('{"price": 106.04}')
    assert not lc_tools._is_barren("var/narratives/wi_1/notes.md")
    assert lc_tools._is_barren("[]") and lc_tools._is_barren("{}")
    assert lc_tools._is_barren("  ") and lc_tools._is_barren(None)


# 5 ── the agent is told its budget instead of discovering it by dying
def test_agents_are_given_an_explicit_step_budget():
    from app.graph import nodes_analysis as na
    seen = {}

    def fake(agent_id, state, system, task, tools):
        seen["task"] = task
        return "[ai] done"

    class St:
        work_item_id = "wi_b"
    na._run_agent(None, "fundamental", St(), "sys", "Analyse SBUX.", [],
                  fake, None)
    assert "Analyse SBUX." in seen["task"]
    assert f"roughly {na.STEP_BUDGET} tool calls" in seen["task"]
    assert na.RECURSION_LIMIT == 30
