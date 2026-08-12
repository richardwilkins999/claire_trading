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
from ..graph.pipeline import build_override_pipeline, build_pipeline
from ..graph.state import Instrument, PipelineState, Thesis


class Desk:
    def __init__(self, conn, deps, checkpointer, repo, *, clock=time.time,
                 session_cal=None):
        self.conn, self.repo = conn, repo
        self.pipeline = build_pipeline(deps, checkpointer)
        self.override_pipeline = build_override_pipeline(deps, checkpointer)
        self.clock = clock
        self.cal = session_cal          # None → sessions.DEFAULTS

    def cfg(self, wi):
        return {"configurable": {"thread_id": wi}}

    # ── starting runs ────────────────────────────────────────────────────
    def start_run(self, instrument, kind="pipeline", *, background=True,
                  trigger=None):
        ts = int(self.clock())
        day = time.strftime("%Y-%m-%d", time.gmtime(ts))
        wi = f"wi_{day}_{instrument.ticker}_{secrets.token_hex(2)}"
        self.repo.create_work_item(wi, kind, instrument.ticker, ts=ts)
        if trigger:
            self.conn.execute("UPDATE work_items SET trigger=? WHERE id=?",
                              (trigger, wi))
        state = PipelineState(work_item_id=wi, kind=kind, trigger=trigger,
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

    # ── human override of a PASS verdict (DESIGN.md §10) ─────────────────
    def override(self, origin_wi, *, direction, entry, stop, target=None,
                 note=None, background=True):
        """Send a concluded PASS to the approval gate anyway.

        The arbiter declining is a decision, not a bug, so overriding it is
        deliberately NOT one click: a pass thesis carries no prices at all,
        and Thesis refuses a buy without an entry and a stop below it. The
        human therefore has to author the levels the desk would not, and the
        same validator that guards an agent's thesis guards theirs. From the
        gate onwards this is an ordinary approval — same token, same cash
        check, same execution path.
        """
        origin = self.work_item(origin_wi)
        if origin is None:
            raise ValueError("unknown work item")
        if not origin["thesis_json"]:
            raise ValueError("that run never produced a thesis to override")
        old = json.loads(origin["thesis_json"])
        if old.get("direction") != "pass":
            raise ValueError(f"only a pass can be overridden — that run is "
                             f"{old.get('direction')!r}")
        if origin["state"] not in ("done", "rejected", "expired"):
            raise ValueError(f"run is still {origin['state']}")
        prior = self.conn.execute(
            "SELECT id, state FROM work_items WHERE override_of=?"
            " AND state NOT IN ('rejected','expired','failed')",
            (origin_wi,)).fetchone()
        if prior:                       # one live override per verdict
            raise ValueError(f"already overridden by {prior['id']} "
                             f"({prior['state']})")

        # raises if the levels are incoherent — that guard is the point
        thesis = Thesis(ticker=origin["ticker"], direction=direction,
                        conviction=old.get("conviction") or 0.0,
                        entry_low=entry, entry_high=entry,
                        stop_loss=stop, take_profit=target,
                        currency=old.get("currency") or "USD",
                        conditions=(old.get("conditions") or [])
                        + [f"HUMAN OVERRIDE of a PASS verdict"
                           f"{': ' + note if note else ''}"])
        inst = self._instrument(origin["ticker"], thesis.currency)
        ts = int(self.clock())
        day = time.strftime("%Y-%m-%d", time.gmtime(ts))
        wi = f"wi_{day}_{origin['ticker']}_ovr_{secrets.token_hex(2)}"
        self.repo.create_work_item(wi, "override", origin["ticker"], ts=ts)
        self.conn.execute(
            "UPDATE work_items SET override_of=?, trigger=?, thesis_json=?"
            " WHERE id=?",
            (origin_wi, f"human override of {origin_wi}",
             thesis.model_dump_json(), wi))
        self.repo._event(wi, ts, "human", "created", "override",
                         payload={"origin": origin_wi, "direction": direction,
                                  "entry": entry, "stop": stop,
                                  "target": target, "note": note})
        state = PipelineState(work_item_id=wi, kind="override",
                              trigger=f"human override of {origin_wi}",
                              ticker=origin["ticker"], instrument=inst,
                              thesis=thesis)
        if background:
            threading.Thread(target=self._run_override, args=(wi, state),
                             daemon=True, name=f"ovr-{wi}").start()
        else:
            self._run_override(wi, state)
        return wi

    def _run_override(self, wi, state):
        try:
            self.override_pipeline.invoke(state, self.cfg(wi))
        except Exception as e:                      # noqa: BLE001
            self.repo.set_state(wi, "failed", actor="system",
                                ts=int(self.clock()),
                                payload={"error": str(e)[:300]})
            return
        self._sync(wi)

    def _instrument(self, ticker, currency):
        r = self.conn.execute(
            "SELECT * FROM instruments WHERE ticker=? LIMIT 1",
            (ticker,)).fetchone()
        if r is None:
            raise ValueError(f"no instrument on record for {ticker}")
        # lot_size is not stored per instrument; the venue's board lot is
        # applied downstream, and fractional venues fill to 0.1
        return Instrument(id=r["id"], ticker=r["ticker"],
                          exchange=r["exchange"],
                          currency=r["currency"] or currency)

    def _pipeline_for(self, wi):
        """An override item lives in the gate-only graph; resuming it against
        the full pipeline would look for nodes its checkpoint never ran."""
        row = self.work_item(wi)
        return (self.override_pipeline
                if row is not None and row["override_of"]
                else self.pipeline)

    # ── resume (validation already done by the API layer) ────────────────
    def resume(self, wi, payload: dict):
        self._pipeline_for(wi).invoke(Command(resume=payload), self.cfg(wi))
        self._sync(wi)

    # ── keep desk.db in step with the graph ──────────────────────────────
    def _sync(self, wi):
        snap = self._pipeline_for(wi).get_state(self.cfg(wi))
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
