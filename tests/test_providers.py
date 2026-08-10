"""Provider layer tests (DESIGN.md §18 layer 4) — offline, fake LLMs."""
import pytest
from langchain_core.language_models.fake_chat_models import (
    FakeListChatModel, FakeMessagesListChatModel)

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
    assert registry.agent_row(conn, "arbiter")["provider_id"] == "anthropic"


def test_assign_updates_row(conn):
    registry.assign(conn, "news", "deepseek", "deepseek-chat", ts=1)
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
                    fallback_provider_id="deepseek", fallback_model="deepseek-chat")
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
    assert row["provider_id"] == "anthropic"


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
