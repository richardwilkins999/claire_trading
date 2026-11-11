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


# The only env vars a providers row may name as its key (review C1). The
# dashboards validate writes against this same set.
PROVIDER_KEY_REFS = frozenset({"ANTHROPIC_API_KEY", "OPENAI_API_KEY",
                               "DEEPSEEK_API_KEY", "XAI_API_KEY"})


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


NO_TICKER = "the instrument in question"


def render_prompt(prompt, ticker=None) -> str:
    """A stored system prompt, ready for a model: `{ticker}` filled in.
    Every caller goes through here, so the braces never reach a model
    literally — with no ticker in hand (an edited prompt on a role that has
    none) a neutral phrase stands in."""
    return (prompt or "").replace("{ticker}", ticker or NO_TICKER)


def check_capabilities(conn, agent_id, provider_id):
    """Reject an incompatible assignment with the reason — at assignment time,
    not mid-run (DESIGN.md §8)."""
    need = json.loads(agent_row(conn, agent_id)["requires"])
    have = json.loads(provider_row(conn, provider_id)["capabilities"])
    missing = [c for c in need if not have.get(c)]
    if missing:
        raise CapabilityError(
            f"provider {provider_id!r} lacks {missing} required by {agent_id!r}")


def check_health_gate(conn, provider_id):
    """§8: a provider must pass a health probe before it becomes selectable.
    The currently-assigned provider is exempt (you can always keep what you
    have); new assignments need a passing probe within 24h."""
    row = conn.execute(
        "SELECT ok FROM provider_health WHERE provider_id=?"
        " ORDER BY checked_at DESC LIMIT 1", (provider_id,)).fetchone()
    if row is None or not row["ok"]:
        raise CapabilityError(
            f"provider {provider_id!r} has not passed a health probe — "
            "probe it first (Providers page)")


def assign(conn, agent_id, provider_id, model, *, fallback_provider_id=None,
           fallback_model=None, ts=None, require_health=True):
    check_capabilities(conn, agent_id, provider_id)
    if require_health and \
            agent_row(conn, agent_id)["provider_id"] != provider_id:
        check_health_gate(conn, provider_id)
    if fallback_provider_id:
        check_capabilities(conn, agent_id, fallback_provider_id)
    conn.execute(
        "UPDATE agents SET provider_id=?, model=?, fallback_provider_id=?,"
        " fallback_model=?, updated_at=? WHERE id=?",
        (provider_id, model, fallback_provider_id, fallback_model,
         ts or int(time.time()), agent_id))


# Anthropic list prices per 1k tokens, keyed by model. The providers table
# carries ONE price per provider, which is wrong the moment a desk runs more
# than one model on it — as this one always has. Rates as published
# 2026-06-24; the sonnet-5 introductory rate lapses 2026-08-31, so re-check
# these when a bill looks off. provider_models rows override this table.
MODEL_PRICES = {
    "claude-fable-5":   (0.010, 0.050),
    "claude-mythos-5":  (0.010, 0.050),
    "claude-opus-5":    (0.005, 0.025),
    "claude-opus-4-8":  (0.005, 0.025),
    "claude-opus-4-7":  (0.005, 0.025),
    "claude-opus-4-6":  (0.005, 0.025),
    "claude-sonnet-5":  (0.003, 0.015),
    "claude-sonnet-4-6": (0.003, 0.015),
    "claude-haiku-4-5": (0.001, 0.005),
}
CACHE_WRITE_MULTIPLIER = 1.25            # 5-minute TTL; a 1h write costs 2x
CACHE_READ_MULTIPLIER = 0.10


def price_for(conn, provider_id, model):
    """(in, out) per 1k tokens. A per-model row wins, then the published
    table, then the provider's blanket rate as a last resort."""
    row = conn.execute(
        "SELECT cost_per_1k_in, cost_per_1k_out FROM provider_models"
        " WHERE provider_id=? AND model=?", (provider_id, model)).fetchone()
    if row and row["cost_per_1k_in"] is not None:
        return row["cost_per_1k_in"], row["cost_per_1k_out"] or 0.0
    if model in MODEL_PRICES:
        return MODEL_PRICES[model]
    p = provider_row(conn, provider_id)
    return (p["cost_per_1k_in"] or 0.0), (p["cost_per_1k_out"] or 0.0)


def _cache_tokens(usage, which):
    """Cache counts arrive under two different spellings depending on whether
    langchain handed us the raw Anthropic usage or its own normalised
    metadata — check both rather than guess which layer we are behind."""
    raw = usage.get(f"cache_{which}_input_tokens")     # Anthropic's spelling
    if raw:
        return int(raw)
    details = usage.get("input_token_details") or {}   # langchain's
    return int(details.get(f"cache_{which}") or details.get(which) or 0)


def _usage_of(response):
    """(usage dict, layer). The shape says which convention it follows, so
    classify by keys, not by where the dict was found:
    - 'anthropic': raw cache_*_input_tokens keys — input_tokens EXCLUDES the
      cached spans;
    - 'langchain': input_token_details — input_tokens INCLUDES them;
    - 'plain': no cache information at all (OpenAI-style, fakes)."""
    usage = None
    try:
        usage = (response.llm_output or {}).get("usage") or \
            (response.llm_output or {}).get("token_usage")
    except AttributeError:
        pass
    if not usage:
        for gens in response.generations:
            for g in gens:
                meta = getattr(getattr(g, "message", None), "usage_metadata",
                               None)
                if meta and not usage:
                    usage = meta            # keep input_token_details too
    usage = dict(usage or {})
    if "cache_creation_input_tokens" in usage or \
            "cache_read_input_tokens" in usage:
        return usage, "anthropic"
    if usage.get("input_token_details"):
        return usage, "langchain"
    return usage, "plain"


class MeterCallback(BaseCallbackHandler):
    """Every LLM call lands in agent_runs: tokens, cost, latency (§1.6)."""

    def __init__(self, conn, agent_id, work_item_id=None):
        self.conn, self.agent_id, self.work_item_id = conn, agent_id, work_item_id
        a = agent_row(conn, agent_id)
        self.provider_id, self.model = a["provider_id"], a["model"]
        self.cost_in, self.cost_out = price_for(conn, self.provider_id,
                                                self.model)
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
        usage, layer = _usage_of(response)
        tin = usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0
        tout = usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0
        cwrite = _cache_tokens(usage, "creation")
        cread = _cache_tokens(usage, "read")
        # The two layers disagree about what input_tokens means: Anthropic's
        # raw usage reports the UNCACHED remainder, langchain's usage_metadata
        # reports the total including cached spans. Which one we hold is
        # known from WHERE it came from — guessing from the magnitudes (the
        # old approach) subtracted the cache twice whenever the uncached
        # remainder happened to exceed the cached spans.
        fresh = max(tin - cwrite - cread, 0) if layer == "langchain" else tin
        tin = fresh + cwrite + cread            # store the true prompt size
        # A call served by the fallback is priced (and recorded) as the model
        # that actually answered, not the primary the agent is assigned.
        provider_id, model = self._served_by(response)
        cost_in, cost_out = (self.cost_in, self.cost_out) \
            if (provider_id, model) == (self.provider_id, self.model) \
            else price_for(self.conn, provider_id, model)
        cost = (fresh / 1000 * cost_in
                + cwrite / 1000 * cost_in * CACHE_WRITE_MULTIPLIER
                + cread / 1000 * cost_in * CACHE_READ_MULTIPLIER
                + tout / 1000 * cost_out)
        self.conn.execute(
            "UPDATE agent_runs SET ended_at=?, tokens_in=?, tokens_out=?,"
            " cache_write_tokens=?, cache_read_tokens=?, cost_usd=?,"
            " provider_id=?, model=?, status='ok' WHERE id=?",
            (int(time.time()), tin, tout, cwrite, cread, cost, provider_id,
             model, self.run_id))

    def _served_by(self, response):
        """(provider, model) that produced this response. Providers echo the
        model id in their metadata; it may carry a date suffix, so match the
        agent's configured ids by prefix."""
        served = None
        try:
            served = (response.llm_output or {}).get("model_name") or \
                (response.llm_output or {}).get("model")
        except AttributeError:
            pass
        if not served:
            for gens in response.generations:
                for g in gens:
                    meta = getattr(getattr(g, "message", None),
                                   "response_metadata", None) or {}
                    served = served or meta.get("model_name") or \
                        meta.get("model")
        if not served:
            return self.provider_id, self.model
        a = agent_row(self.conn, self.agent_id)
        fb_model, fb_provider = a["fallback_model"], a["fallback_provider_id"]
        if fb_model and fb_model != self.model and \
                str(served).startswith(fb_model):
            return fb_provider or self.provider_id, fb_model
        return self.provider_id, self.model

    def on_llm_error(self, error, **kw):
        self.conn.execute(
            "UPDATE agent_runs SET ended_at=?, status='error', error=? WHERE id=?",
            (int(time.time()), str(error)[:500], self.run_id))


def supports_temperature(model: str) -> bool:
    """The Claude 5 family rejects `temperature` outright (verified against
    the API: opus-5 and sonnet-5 return 400, haiku-4-5 accepts). Match the
    tier immediately followed by -5, so claude-haiku-4-5 is NOT caught."""
    import re
    return not re.match(r"^claude-(opus|sonnet|haiku|fable|mythos)-5(-|$)",
                        model or "")


def build_llm(conn, provider_id, model, temperature=None, max_tokens=None,
              env=os.environ):
    p = provider_row(conn, provider_id)
    if p["kind"] == "claude_sdk":
        # Claude Agent SDK on the claude.ai (Max) login — no API key, no
        # per-token bill. temperature/max_tokens are not SDK options; the
        # Claude 5 models reject temperature anyway.
        from .claude_sdk import SdkChat
        return SdkChat(model, provider_id=provider_id)
    from langchain.chat_models import init_chat_model
    ref = p["api_key_ref"] or ""
    if ref and ref not in PROVIDER_KEY_REFS:
        # a providers row is sent `env[api_key_ref]` as a bearer token, so a
        # row naming CLAIRE_INTERNAL_SECRET (or any non-LLM secret) would leak
        # it to whatever base_url the row points at — refuse, whatever wrote it
        raise ProviderError(f"provider {provider_id!r} names key {ref!r}, "
                            "which is not an LLM provider key")
    key = env.get(ref, "")
    kw = {"max_retries": 3}                 # ride out transient 429/529s
    if temperature is not None and supports_temperature(model):
        kw["temperature"] = temperature
    if max_tokens is not None:
        kw["max_tokens"] = max_tokens
    if p["kind"] == "anthropic":
        if not key:
            raise ProviderError(
                f"provider {provider_id!r} needs env {p['api_key_ref']} — not set")
        # Automatic prompt caching. A research loop re-sends the whole
        # conversation on every step, so by step 10 we were paying full price
        # ten times for the same prefix — 93% of all tokens billed were input.
        # The top-level marker caches the last cacheable block, which walks
        # forward as the transcript grows: each step writes at 1.25x and the
        # next reads at 0.1x. Anthropic prices this per model and it needs no
        # placement decisions from us, unlike per-block cache_control.
        kw["model_kwargs"] = {"cache_control": {"type": "ephemeral"}}
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
        llm = with_fallback(llm, fb)
    meter = MeterCallback(conn, agent_id, work_item_id)
    return llm.with_config(callbacks=[meter])


def with_fallback(llm, fb):
    """Attach a fallback model. A Claude SDK (subscription) model and a
    LangChain (API) model cannot be chained — and silently falling back from
    the subscription onto the metered API is exactly what must not happen —
    so a mixed pair keeps the primary alone."""
    from .claude_sdk import is_sdk
    if is_sdk(llm) != is_sdk(fb):
        return llm
    return llm.with_fallbacks([fb])
