"""Provider layer tests (DESIGN.md §18 layer 4) — offline, fake LLMs."""
import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from app.accounting import db
from app.providers import health, registry, seeds


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init(c)
    seeds.seed(c, ts=1_754_900_000)
    return c


def test_capability_gate_rejects_with_reason(conn):
    conn.execute(
        "INSERT INTO providers (id, display_name, kind, base_url, capabilities)"
        " VALUES ('dumb', 'No-tools LLM', 'openai_compatible', 'http://x/v1',"
        " '{\"tool_calling\": false, \"structured_output\": false}')")
    with pytest.raises(registry.CapabilityError, match="structured_output"):
        registry.assign(conn, "arbiter", "dumb", "some-model")
    # the failed assignment must not have changed the row
    assert registry.agent_row(conn, "arbiter")["provider_id"] == "claude_max"


def test_assign_updates_row(conn):
    registry.assign(conn, "news", "deepseek", "deepseek-chat", ts=1,
                    require_health=False)     # gate has its own test
    a = registry.agent_row(conn, "news")
    assert (a["provider_id"], a["model"]) == ("deepseek", "deepseek-chat")


def test_missing_key_fails_loudly(conn):
    with pytest.raises(registry.ProviderError, match="ANTHROPIC_API_KEY"):
        registry.build_llm(conn, "anthropic", "claude-haiku-4-5", env={})


def test_fallback_fires_on_provider_failure(conn):
    class Boom(FakeListChatModel):
        def _call(self, *a, **k):
            raise RuntimeError("provider down")

    calls = []

    def fake_build(c, provider_id, model, *a, **k):
        calls.append(provider_id)
        if provider_id == "anthropic":
            return Boom(responses=["never"])
        return FakeListChatModel(responses=["from fallback"])

    registry.assign(conn, "news", "anthropic", "claude-haiku-4-5",
                    fallback_provider_id="deepseek", fallback_model="deepseek-chat",
                    require_health=False)     # gate has its own test
    llm = registry.model_for(conn, "news", _build=fake_build, env={})
    out = llm.invoke("hi")
    assert out.content == "from fallback"
    assert calls == ["anthropic", "deepseek"]


def test_meter_records_run(conn):
    def fake_build(c, provider_id, model, *a, **k):
        return FakeListChatModel(responses=["metered reply"])

    llm = registry.model_for(conn, "news", work_item_id="wi_x", _build=fake_build,
                             env={})
    llm.invoke("hello")
    row = conn.execute("SELECT * FROM agent_runs").fetchone()
    assert row["agent_id"] == "news"
    assert row["work_item_id"] == "wi_x"
    assert row["status"] == "ok"
    assert row["provider_id"] == "claude_max"


def test_health_probe_records_failure_and_success(conn):
    def broken(c, provider_id, model, **k):
        raise RuntimeError("no route to host")
    assert health.probe(conn, "local", _build=broken, env={}) is False

    def working(c, provider_id, model, **k):
        return FakeListChatModel(responses=["ok"])
    assert health.probe(conn, "local", _build=working, env={}) is True
    rows = list(conn.execute(
        "SELECT ok FROM provider_health WHERE provider_id='local' ORDER BY rowid"))
    assert [r["ok"] for r in rows] == [0, 1]


def test_disabled_agent_refused(conn):
    conn.execute("UPDATE agents SET enabled=0 WHERE id='news'")
    with pytest.raises(registry.ProviderError, match="disabled"):
        registry.model_for(conn, "news", env={})


def test_provider_row_cannot_name_a_non_llm_secret(conn):
    """Review C1: a row naming CLAIRE_INTERNAL_SECRET would send it as the
    bearer token to the row's base_url. build_llm refuses whatever wrote it."""
    conn.execute(
        "INSERT INTO providers (id, display_name, kind, base_url, api_key_ref,"
        " capabilities) VALUES ('evil','x','openai_compatible',"
        " 'http://attacker/v1','CLAIRE_INTERNAL_SECRET','{}')")
    with pytest.raises(registry.ProviderError, match="not an LLM provider key"):
        registry.build_llm(conn, "evil", "m",
                           env={"CLAIRE_INTERNAL_SECRET": "s3cret"})


# ── watchlist search when Yahoo refuses us (2026-11-10) ──────────────────
def test_saxo_search_maps_to_desk_symbols(monkeypatch):
    from app.tools import saxo_chart
    monkeypatch.setattr(saxo_chart, "_get", lambda *a, **k: {"Data": [
        {"Symbol": "IBM:xnys", "Description": "IBM Corp.", "AssetType": "Stock"},
        {"Symbol": "DBK:xetr", "Description": "Deutsche Bank", "AssetType": "Stock"},
        {"Symbol": "00700:xhkg", "Description": "Tencent", "AssetType": "Stock"},
        {"Symbol": "XYZ:xmil", "Description": "not a desk market"}]})
    out = saxo_chart.search("x", client=object())
    assert [(r["symbol"], r["exchange"]) for r in out] == [
        ("IBM", "NYSE"), ("DBK.DE", "XETRA"), ("0700.HK", "HKEX")]


def test_tradingview_search_keeps_desk_markets_only():
    from app.tools import tradingview

    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"symbols": [
                {"symbol": "700", "exchange": "HKEX", "description": "Tencent"},
                {"symbol": "D05", "exchange": "SGX", "description": "DBS"},
                {"symbol": "MC", "exchange": "EURONEXT", "country": "FR"},
                {"symbol": "TCEHY", "exchange": "OTC", "description": "ADR"},
                {"symbol": "<em>DBK</em>", "exchange": "XETR"}]}

    class C:
        def get(self, *a, **k):
            return R()
    out = tradingview.search("x", _client=C())
    assert [(r["symbol"], r["exchange"]) for r in out] == [
        ("0700.HK", "HKEX"), ("D05.SI", "SGX"), ("MC.PA", "PARIS"),
        ("DBK.DE", "XETRA")]


def test_combined_search_puts_saxo_first_and_survives_one_failure():
    from app.tools import market, tradingview
    saxo = lambda q: [{"symbol": "IBM", "exchange": "NYSE"}]    # noqa: E731
    orig = tradingview.search
    try:
        tradingview.search = lambda q: [{"symbol": "IBM", "exchange": "NYSE"},
                                        {"symbol": "D05.SI", "exchange": "SGX"}]
        out = market.combined_search(saxo)("x")
        assert [(r["symbol"], r["at_saxo"]) for r in out] == [
            ("IBM", True), ("D05.SI", False)]

        def boom(q):
            raise RuntimeError("down")
        assert market.combined_search(boom)("x")[0]["symbol"] == "IBM"
        tradingview.search = boom
        with pytest.raises(market.MarketError, match="search unavailable"):
            market.combined_search(boom)("x")
    finally:
        tradingview.search = orig
