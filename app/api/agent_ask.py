"""Direct agent Q&A (dashboard drawer): one metered, tool-less LLM call using
THAT agent's own model and system prompt, grounded in its actual recent
activity from desk.db. The agent can explain itself; it cannot act — no tools,
no writes, and approvals stay token-gated on the dashboard.
"""
import json
import time

from ..providers import registry


def gather_context(conn, narratives, agent_id, *, clock=time.time) -> str:
    parts = []
    runs = [dict(r) for r in conn.execute(
        "SELECT r.*, w.ticker, w.state AS wi_state FROM agent_runs r"
        " LEFT JOIN work_items w ON w.id = r.work_item_id"
        " WHERE r.agent_id=? ORDER BY r.started_at DESC LIMIT 8", (agent_id,))]
    if runs:
        parts.append("Your recent runs (newest first). NOTE: the dollar "
                     "figure is the API cost of the call — it is NOT your "
                     "conviction:")
        for r in runs:
            when = int(clock()) - (r["started_at"] or 0)
            parts.append(
                f"- {when // 60}m ago · {r['ticker'] or r['work_item_id'] or '?'}"
                f" · status={r['status']} · api_cost=${r['cost_usd'] or 0:.4f}"
                f" · work item state: {r['wi_state'] or '?'}"
                + (f" · error: {r['error']}" if r["error"] else ""))
    else:
        parts.append("You have never run yet.")

    # your actual verdict on the most recent run — signal, conviction and the
    # one-line summary you emitted, so you never have to guess at your own
    # conclusion (the run summary is the only place these are written down)
    for r in runs:
        if not r["work_item_id"] or not narratives:
            continue
        try:
            summary = narratives.read(r["work_item_id"], "summary")
        except OSError:
            continue
        mine = [ln for ln in summary.splitlines()
                if f"**{agent_id}**" in ln or
                (agent_id == "arbiter" and ln.startswith("**Arbiter**"))]
        if mine:
            parts.append(f"\nWhat you actually concluded on "
                         f"{r['ticker'] or r['work_item_id']}:\n"
                         + "\n".join(mine))
        break
    current = next((r for r in runs if r["status"] == "running"), None)
    parts.append(f"Right now you are "
                 f"{'RUNNING on ' + str(current['ticker']) if current else 'idle'}.")
    for r in runs:
        if r["work_item_id"] and narratives:
            try:
                text = narratives.read(r["work_item_id"], agent_id)
                parts.append(f"\nYour latest narrative ({r['work_item_id']}):\n"
                             f"{text[:1800]}")
                break
            except OSError:
                continue
    return "\n".join(parts)


def make_agent_ask(conn, narratives, *, llm_factory=None, clock=time.time):
    """Returns handler(body) -> NDJSON line generator."""

    def handler(body):
        agent_id = (body or {}).get("agent_id", "")
        text = (body or {}).get("text", "")

        def gen():
            try:
                row = registry.agent_row(conn, agent_id)
            except registry.ProviderError as e:
                yield json.dumps({"kind": "error", "text": str(e)}) + "\n"
                yield json.dumps({"kind": "done"}) + "\n"
                return
            context = gather_context(conn, narratives, agent_id, clock=clock)
            system = (row["system_prompt"] +
                      "\n\nRichard is asking you a DIRECT question from the "
                      "dashboard. You are not running your pipeline role right "
                      "now. Answer plainly and first-person from the activity "
                      "context below; say so when something is not in your "
                      "context rather than guessing.\n\n=== YOUR ACTIVITY ===\n"
                      + context)
            try:
                llm = (llm_factory or (lambda a: registry.model_for(conn, a)))(
                    agent_id)
                acc = ""
                for chunk in llm.stream([("system", system), ("user", text)]):
                    piece = getattr(chunk, "content", "")
                    if isinstance(piece, list):
                        piece = "".join(p.get("text", "") for p in piece
                                        if isinstance(p, dict))
                    if piece:
                        acc += piece
                        yield json.dumps({"kind": "text", "text": acc}) + "\n"
                yield json.dumps({"kind": "done"}) + "\n"
            except Exception as e:              # noqa: BLE001 — no key, outage
                yield json.dumps({"kind": "error",
                                  "text": str(e)[:400]}) + "\n"
                yield json.dumps({"kind": "done"}) + "\n"
        return gen()

    return handler
