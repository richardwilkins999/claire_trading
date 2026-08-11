"""Run lifecycle around the pipeline graph (DESIGN.md §10).

The Desk starts runs, notices interrupts, mints single-use approval tokens
with session-aware expiry, and applies authorized resumes. It never decides —
authorization happened upstream (dashboards) and validation happens in the
API layer; the Desk just keeps graph and desk.db in step.
"""
import json
import secrets
import threading
import time
from datetime import datetime, timezone

from langgraph.types import Command

from .. import sessions
from ..graph.pipeline import build_pipeline
from ..graph.state import PipelineState


class Desk:
    def __init__(self, conn, deps, checkpointer, repo, *, clock=time.time,
                 session_cal=None):
        self.conn, self.repo = conn, repo
        self.pipeline = build_pipeline(deps, checkpointer)
        self.clock = clock
        self.cal = session_cal          # None → sessions.DEFAULTS

    def cfg(self, wi):
        return {"configurable": {"thread_id": wi}}

    # ── starting runs ────────────────────────────────────────────────────
    def start_run(self, instrument, kind="pipeline", *, background=True):
        ts = int(self.clock())
        day = time.strftime("%Y-%m-%d", time.gmtime(ts))
        wi = f"wi_{day}_{instrument.ticker}_{secrets.token_hex(2)}"
        self.repo.create_work_item(wi, kind, instrument.ticker, ts=ts)
        state = PipelineState(work_item_id=wi, kind=kind,
                              ticker=instrument.ticker, instrument=instrument)
        if background:
            threading.Thread(target=self._run, args=(wi, state),
                             daemon=True, name=f"run-{wi}").start()
        else:
            self._run(wi, state)
        return wi

    def _run(self, wi, state):
        try:
            self.pipeline.invoke(state, self.cfg(wi))
        except Exception as e:                      # noqa: BLE001
            self.repo.set_state(wi, "failed", actor="system",
                                ts=int(self.clock()),
                                payload={"error": str(e)[:300]})
            return
        self._sync(wi)

    # ── resume (validation already done by the API layer) ────────────────
    def resume(self, wi, payload: dict):
        self.pipeline.invoke(Command(resume=payload), self.cfg(wi))
        self._sync(wi)

    # ── keep desk.db in step with the graph ──────────────────────────────
    def _sync(self, wi):
        snap = self.pipeline.get_state(self.cfg(wi))
        ts = int(self.clock())
        if snap.next:                               # suspended at the gate
            vals = snap.values
            thesis = vals.get("thesis")
            instrument = vals.get("instrument")
            token = secrets.token_hex(16)
            self.conn.execute(
                "UPDATE work_items SET state='awaiting_approval',"
                " approval_token=?, token_state='minted', expires_at=?,"
                " thesis_json=?, updated_at=? WHERE id=?",
                (token, self._expiry(instrument, ts),
                 thesis.model_dump_json() if thesis is not None else None,
                 ts, wi))
            self.repo._event(wi, ts, "system", "running", "awaiting_approval")
            return
        vals = snap.values
        # a `pass` verdict is still a thesis worth keeping: it feeds
        # v_thesis_outcomes, the run drill-down and the screener's own
        # track record. Previously only gated runs persisted one.
        thesis = vals.get("thesis")
        if thesis is not None:
            self.conn.execute(
                "UPDATE work_items SET thesis_json=? WHERE id=?"
                " AND thesis_json IS NULL",
                (thesis.model_dump_json(), wi))
        ap = vals.get("approval")
        status = ap.status if ap is not None else None
        errors = vals.get("errors") or []
        if status == "approved":
            final = "executing" if vals.get("order_ids") else "done"
        elif status in ("rejected", "expired"):
            final = status
        elif errors and vals.get("thesis") is None:
            final = "failed"
        else:
            final = "done"                          # pass verdict, clean finish
        self.repo.set_state(wi, final, actor="system", ts=ts,
                            payload={"errors": errors} if errors else None)

    def _expiry(self, instrument, ts) -> int:
        """End of the instrument's next trading session (§10) — never a bare
        wall-clock offset. Unknown exchange degrades to +24 h, loudly stored
        the same way."""
        now = datetime.fromtimestamp(ts, tz=timezone.utc)
        try:
            return int(sessions.close_of(instrument.exchange, now,
                                         self.cal).timestamp())
        except (KeyError, AttributeError):
            return ts + 86400

    def work_item(self, wi):
        return self.conn.execute(
            "SELECT * FROM work_items WHERE id=?", (wi,)).fetchone()
