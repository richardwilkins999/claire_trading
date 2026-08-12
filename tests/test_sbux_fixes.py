"""Regressions for everything the SBUX post-mortem turned up."""
import json
from datetime import datetime, timezone

from app.graph.state import AnalystReport, DebateCase, Thesis
from app.tools.lc_tools import MAX_TOOL_FAILURES, build_tool_registry
from app.tools.market import Market

T0 = 1_786_456_800


# 1 ── the formatting slip that cost a paid-for bull case
def test_prose_where_a_list_was_asked_for_is_accepted():
    c = DebateCase(side="bull", conviction=0.6,
                   key_points="Company headlines support the recovery thesis.",
                   rebuttals="Valuation concerns are less relevant here.")
    assert c.key_points == ["Company headlines support the recovery thesis."]
    assert c.rebuttals == ["Valuation concerns are less relevant here."]
    multi = DebateCase(side="bear", conviction=0.5,
                       key_points="- one\n- two\n* three")
    assert multi.key_points == ["one", "two", "three"]
    t = Thesis(ticker="X", direction="pass", conviction=0.3, currency="USD",
               conditions="wait for volume")
    assert t.conditions == ["wait for volume"]
    r = AnalystReport(ticker="X", agent="news", signal="bullish",
                      conviction=0.5, summary="s", key_findings="RSI 38",
                      sources=None, data_asof=datetime.now(timezone.utc))
    assert r.key_findings == ["RSI 38"] and r.sources == []


# 2 ── a tool that keeps failing stops being an option
def test_failing_tool_is_disabled_before_it_eats_the_step_budget():
    class Dead:
        def quote(self, s):
            raise RuntimeError("429 Too Many Requests")

    class St:
        work_item_id = "wi_t"
    tools = {t.name: t for t in
             build_tool_registry(Dead(), None)(["market_quote"], St())}
    q = tools["market_quote"]
    seen = [json.loads(q.invoke({"symbol": "SBUX"}))
            for _ in range(MAX_TOOL_FAILURES + 2)]
    assert all("429" in s["error"] for s in seen[:MAX_TOOL_FAILURES])
    assert "disabled" in seen[MAX_TOOL_FAILURES]["error"]
    assert "STOP calling this tool" in seen[MAX_TOOL_FAILURES]["hint"]
    # the counter is per run, so a fresh run starts clean
    fresh = {t.name: t for t in
             build_tool_registry(Dead(), None)(["market_quote"], St())}
    assert "429" in json.loads(fresh["market_quote"].invoke(
        {"symbol": "SBUX"}))["error"]


# 3 ── a blown step budget must not throw the research away
def test_research_overrun_still_produces_a_report(tmp_path):
    from app.accounting import db
    from app.graph.nodes_analysis import make_analyst
    from app.graph.state import Instrument, PipelineState
    from app.providers import seeds
    from app.tools.files import Narratives
    conn = db.connect(":memory:")
    db.init(conn)
    seeds.seed(conn, ts=T0)
    n = Narratives(tmp_path)
    state = PipelineState(work_item_id="wi_x", ticker="SBUX",
                          instrument=Instrument(id="NASDAQ:SBUX",
                                                ticker="SBUX",
                                                exchange="NASDAQ",
                                                currency="USD"))
    captured = {}

    def factory(agent_id, schema, wi):
        class LLM:
            def invoke(self, messages):
                captured["prompt"] = messages[1][1]
                return AnalystReport(
                    ticker="SBUX", agent="fundamental", signal="neutral",
                    conviction=0.2, summary="partial",
                    data_asof=datetime.now(timezone.utc))
        return LLM(), "sys"

    def blown_loop(agent_id, st, system, task, tools):
        # what _run_agent hands back when the limit is hit mid-loop
        return ("[ai] gathered some figures\n[system] RESEARCH CUT SHORT: "
                "Recursion limit of 60 reached. Report on what was gathered.")
    node = make_analyst(conn, "fundamental", n, lambda names, st: [],
                        structured_factory=factory, agent_factory=blown_loop)
    out = node(state)
    assert out.signal == "neutral"                  # a report still exists
    assert "RESEARCH CUT SHORT" in captured["prompt"]
    assert "fundamental.report.md" in n.list("wi_x")
    row = conn.execute("SELECT payload FROM agent_reports WHERE agent_id="
                       "'fundamental'").fetchone()
    assert row is not None                          # and it is on the record


# 4 ── PASS verdicts are visible instead of silently vanishing
def test_decided_runs_are_surfaced(tmp_path):
    from app.accounting import db
    from app.accounting.repo import Repo
    from app.dashboards import create_server
    import threading
    import httpx
    conn = db.connect(":memory:")
    db.init(conn)
    repo = Repo(conn)
    repo.create_account("a", "alpaca", "paper", "USD",
                        fee_model={"type": "flat", "per_trade": "0"}, ts=T0)
    conn.execute(
        "INSERT INTO work_items (id, kind, ticker, state, thread_id,"
        " thesis_json, trigger, created_at, updated_at) VALUES"
        " ('wi_p','pipeline','SBUX','done','wi_p',?,'event scan',?,?)",
        (json.dumps({"direction": "pass", "conviction": 0.28,
                     "conditions": ["wait for a full-volume session"]}),
         T0, T0))

    class M:
        _stale_keys = set()

        def metrics(self, s):
            return {}
    srv = create_server(conn, repo, M(), clock=lambda: T0, port=0,
                        sse_interval=None, dash_key="k")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        d = httpx.get(f"http://127.0.0.1:{srv.server_address[1]}/api/approvals",
                      headers={"X-Dash-Key": "k"}, timeout=5).json()
        assert d["cards"] == []                     # nothing to approve
        assert d["decided"][0]["ticker"] == "SBUX"  # but it is not invisible
        assert d["decided"][0]["direction"] == "pass"
        assert d["decided"][0]["conditions"]
    finally:
        srv.shutdown()


# 5 ── the keyed provider's quota is checked before it is spent
def test_primary_rate_gate_falls_through_instead_of_429ing():
    class TD:
        calls = 0

        def supports(self, s):
            return True

        def quote(self, syms):
            TD.calls += 1
            return {s: {"symbol": s, "price": 1, "currency": "USD",
                        "stale": False, "series": [], "src": "twelvedata"}
                    for s in syms}

    def yahoo_forbidden(*a, **k):
        raise AssertionError("must not reach Yahoo while TradingView answers")
    m = Market(_get=yahoo_forbidden, primary=TD(),
               tv_quotes=lambda s: {x: {"symbol": x, "price": 2,
                                        "currency": "USD", "stale": False,
                                        "series": [], "src": "tradingview"}
                                    for x in s},
               clock=lambda: 1000)
    srcs = [m.spark([f"S{i}"])[f"S{i}"]["src"] for i in range(10)]
    assert TD.calls == Market.PRIMARY_PER_MIN       # quota respected exactly
    assert srcs[0] == "twelvedata"
    assert srcs[-1] == "tradingview"                # rest degrade, no 429
