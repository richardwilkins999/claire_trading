"""Provider health probes (DESIGN.md §8) — a provider must pass a probe before
it becomes selectable on the dashboard."""
import time

from . import registry


def probe(conn, provider_id, *, _build=registry.build_llm, env=None):
    """One tiny round-trip; records a provider_health row either way."""
    import os
    env = env if env is not None else os.environ
    t0 = time.time()
    ok, err = 0, None
    try:
        llm = _build(conn, provider_id, _probe_model(conn, provider_id), env=env)
        llm.invoke("Reply with the single word: ok")
        ok = 1
    except Exception as e:              # noqa: BLE001 — any failure is the answer
        err = str(e)[:300]
    conn.execute(
        "INSERT INTO provider_health (provider_id, checked_at, ok, latency_ms,"
        " error) VALUES (?,?,?,?,?)",
        (provider_id, int(t0), ok, int((time.time() - t0) * 1000), err))
    return bool(ok)


def _probe_model(conn, provider_id):
    row = conn.execute(
        "SELECT model FROM provider_models WHERE provider_id=?"
        " ORDER BY last_seen DESC LIMIT 1", (provider_id,)).fetchone()
    if row:
        return row["model"]
    row = conn.execute(
        "SELECT model FROM agents WHERE provider_id=? LIMIT 1",
        (provider_id,)).fetchone()
    if row:
        return row["model"]
    return "claude-haiku-4-5" if provider_id == "anthropic" else "gpt-4o-mini"


def latest(conn):
    return {r["provider_id"]: r for r in conn.execute(
        "SELECT provider_id, MAX(checked_at) AS checked_at, ok, latency_ms, error"
        " FROM provider_health GROUP BY provider_id")}
