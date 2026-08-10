"""DB-backed provider/model resolution (DESIGN.md §8).

Adding a provider — including a local LLM — is one `providers` row plus an env
var; `openai_compatible` covers OpenAI, DeepSeek, Grok/xAI, Ollama, LM Studio,
vLLM, OpenRouter. Secrets never enter the DB: `api_key_ref` is an env var NAME.
"""
import json
import os
import time
import uuid

from langchain_core.callbacks import BaseCallbackHandler


class CapabilityError(Exception):
    pass


class ProviderError(Exception):
    pass


# capability names an agent may require: tool_calling, structured_output
def provider_row(conn, provider_id):
    row = conn.execute("SELECT * FROM providers WHERE id=?", (provider_id,)).fetchone()
    if row is None:
        raise ProviderError(f"no provider {provider_id!r}")
    return row


def agent_row(conn, agent_id):
    row = conn.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
    if row is None:
        raise ProviderError(f"no agent {agent_id!r}")
    return row


def check_capabilities(conn, agent_id, provider_id):
    """Reject an incompatible assignment with the reason — at assignment time,
    not mid-run (DESIGN.md §8)."""
    need = json.loads(agent_row(conn, agent_id)["requires"])
    have = json.loads(provider_row(conn, provider_id)["capabilities"])
    missing = [c for c in need if not have.get(c)]
    if missing:
        raise CapabilityError(
            f"provider {provider_id!r} lacks {missing} required by {agent_id!r}")


def assign(conn, agent_id, provider_id, model, *, fallback_provider_id=None,
           fallback_model=None, ts=None):
    check_capabilities(conn, agent_id, provider_id)
    if fallback_provider_id:
        check_capabilities(conn, agent_id, fallback_provider_id)
    conn.execute(
        "UPDATE agents SET provider_id=?, model=?, fallback_provider_id=?,"
        " fallback_model=?, updated_at=? WHERE id=?",
        (provider_id, model, fallback_provider_id, fallback_model,
         ts or int(time.time()), agent_id))


class MeterCallback(BaseCallbackHandler):
    """Every LLM call lands in agent_runs: tokens, cost, latency (§1.6)."""

    def __init__(self, conn, agent_id, work_item_id=None):
        self.conn, self.agent_id, self.work_item_id = conn, agent_id, work_item_id
        a = agent_row(conn, agent_id)
        self.provider_id, self.model = a["provider_id"], a["model"]
        p = provider_row(conn, self.provider_id)
        self.cost_in = p["cost_per_1k_in"] or 0.0
        self.cost_out = p["cost_per_1k_out"] or 0.0
        self._start = None
        self.run_id = None

    def on_llm_start(self, serialized, prompts, **kw):
        self._start = time.time()
        self.run_id = f"run_{uuid.uuid4().hex[:12]}"
        self.conn.execute(
            "INSERT INTO agent_runs (id, agent_id, work_item_id, provider_id,"
            " model, started_at, status) VALUES (?,?,?,?,?,?,?)",
            (self.run_id, self.agent_id, self.work_item_id, self.provider_id,
             self.model, int(self._start), "running"))

    on_chat_model_start = on_llm_start

    def on_llm_end(self, response, **kw):
        usage = {}
        try:
            usage = response.llm_output.get("usage") or response.llm_output.get(
                "token_usage") or {}
        except AttributeError:
            pass
        if not usage:
            for gens in response.generations:
                for g in gens:
                    meta = getattr(g, "message", None)
                    meta = getattr(meta, "usage_metadata", None)
                    if meta:
                        usage = {"input_tokens": meta.get("input_tokens", 0),
                                 "output_tokens": meta.get("output_tokens", 0)}
        tin = usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0
        tout = usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0
        cost = tin / 1000 * self.cost_in + tout / 1000 * self.cost_out
        self.conn.execute(
            "UPDATE agent_runs SET ended_at=?, tokens_in=?, tokens_out=?,"
            " cost_usd=?, status='ok' WHERE id=?",
            (int(time.time()), tin, tout, cost, self.run_id))

    def on_llm_error(self, error, **kw):
        self.conn.execute(
            "UPDATE agent_runs SET ended_at=?, status='error', error=? WHERE id=?",
            (int(time.time()), str(error)[:500], self.run_id))


def build_llm(conn, provider_id, model, temperature=None, max_tokens=None,
              env=os.environ):
    from langchain.chat_models import init_chat_model
    p = provider_row(conn, provider_id)
    key = env.get(p["api_key_ref"] or "", "")
    kw = {}
    if temperature is not None:
        kw["temperature"] = temperature
    if max_tokens is not None:
        kw["max_tokens"] = max_tokens
    if p["kind"] == "anthropic":
        if not key:
            raise ProviderError(
                f"provider {provider_id!r} needs env {p['api_key_ref']} — not set")
        return init_chat_model(model, model_provider="anthropic", api_key=key, **kw)
    if p["kind"] == "openai_compatible":
        return init_chat_model(model, model_provider="openai",
                               base_url=p["base_url"], api_key=key or "local", **kw)
    raise ProviderError(f"unknown provider kind {p['kind']!r}")


def model_for(conn, agent_id, *, work_item_id=None, env=os.environ,
              _build=build_llm):
    """The per-agent LLM: DB-configured provider+model, optional fallback,
    always metered. `_build` is injectable for tests."""
    a = agent_row(conn, agent_id)
    if not a["enabled"]:
        raise ProviderError(f"agent {agent_id!r} is disabled")
    llm = _build(conn, a["provider_id"], a["model"], a["temperature"],
                 a["max_tokens"], env=env)
    if a["fallback_provider_id"]:
        fb = _build(conn, a["fallback_provider_id"], a["fallback_model"],
                    a["temperature"], a["max_tokens"], env=env)
        llm = llm.with_fallbacks([fb])
    meter = MeterCallback(conn, agent_id, work_item_id)
    return llm.with_config(callbacks=[meter])
