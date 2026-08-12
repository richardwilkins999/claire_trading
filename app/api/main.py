"""claire-api (:7788) — graph runtime, resume authorization seam (DESIGN.md §10).

`Command(resume=…)` will resume for ANY caller; LangGraph performs no
authorization. Therefore /internal/resume: localhost only, shared secret,
token verified against the DB, single-use enforced here. It is not a tool any
agent can call.
"""
import time
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}

# intended → acceptable already-terminal states (idempotent replays)
DONE_STATES = {"approved": ("approved", "executing", "done"),
               "rejected": ("rejected",),
               "expired": ("expired",)}


class ResumeBody(BaseModel):
    work_item_id: str
    status: Literal["approved", "rejected", "expired"]
    token: str
    actor: str
    size_base: float | None = None
    qty: float | None = None
    trail_pct: float | None = None
    broker: Literal["alpaca", "saxo", "moomoo"] | None = None


class OverrideBody(BaseModel):
    work_item_id: str                       # the PASS being overturned
    direction: Literal["buy", "sell"]
    entry: float = Field(gt=0)              # a pass thesis carries no prices,
    stop: float = Field(gt=0)               # so the human must author them
    target: float | None = Field(default=None, gt=0)
    note: str | None = None
    actor: str


class RunBody(BaseModel):
    ticker: str
    exchange: str
    currency: str
    instrument_id: str | None = None
    kind: Literal["pipeline", "sell_review"] = "pipeline"
    lot_size: int = 1
    trigger: str | None = None


def create_app(desk, conn, secret: str, *, clock=time.time,
               ask_handler=None, agent_ask_handler=None) -> FastAPI:
    app = FastAPI(title="claire-api")

    def _guard(request: Request):
        host = request.client.host if request.client else ""
        if host not in LOCAL_HOSTS:
            raise HTTPException(403, "localhost only")
        if not secret or request.headers.get("x-claire-secret") != secret:
            raise HTTPException(403, "bad internal secret")

    @app.post("/internal/resume")
    def internal_resume(body: ResumeBody, request: Request):
        _guard(request)
        row = desk.work_item(body.work_item_id)
        if row is None:
            raise HTTPException(404, "unknown work item")
        if body.status == "expired":
            if body.actor != "reaper":
                raise HTTPException(403, "only the reaper may expire")
        elif body.actor != "human":
            raise HTTPException(403, "approve/reject requires a human actor")

        if body.token != (row["approval_token"] or ""):
            raise HTTPException(403, "token mismatch")
        if row["token_state"] == "burned":
            if row["state"] in DONE_STATES[body.status]:
                return {"ok": True, "idempotent": True, "state": row["state"]}
            raise HTTPException(409, "token already used")
        if row["state"] != "awaiting_approval":
            raise HTTPException(409, f"item is {row['state']}")
        now = int(clock())
        if body.status != "expired" and row["expires_at"] and \
                now >= row["expires_at"]:
            raise HTTPException(410, "approval expired")

        payload = {"status": body.status, "size_base": body.size_base,
                   "qty": body.qty, "trail_pct": body.trail_pct,
                   "broker": body.broker, "actor": body.actor,
                   "token": body.token,
                   "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))}
        if body.status == "approved":
            desk.repo.set_state(body.work_item_id, "approved",
                                actor=body.actor, ts=now,
                                payload={"size_base": body.size_base,
                                         "qty": body.qty,
                                         "broker": body.broker})
        desk.resume(body.work_item_id, payload)
        conn.execute("UPDATE work_items SET token_state='burned',"
                     " updated_at=? WHERE id=?", (now, body.work_item_id))
        return {"ok": True, "state": desk.work_item(body.work_item_id)["state"]}

    @app.post("/internal/override")
    def internal_override(body: OverrideBody, request: Request):
        """Overturn a PASS. Same trust boundary as /internal/resume — the
        dashboard has already checked pairing; this creates no position by
        itself, it only puts the item in front of the human at the gate."""
        _guard(request)
        if body.actor != "human":
            raise HTTPException(403, "an override requires a human actor")
        try:
            wi = desk.override(body.work_item_id, direction=body.direction,
                               entry=body.entry, stop=body.stop,
                               target=body.target, note=body.note)
        except ValidationError as e:
            # incoherent levels (a stop that is not a floor) — the guard doing
            # its job. Checked BEFORE ValueError: pydantic's error subclasses
            # it, so the order here is what makes this a 400 and not a 409.
            raise HTTPException(400, str(e)[:300]) from e
        except ValueError as e:                     # wrong state, already done
            raise HTTPException(409, str(e)) from e
        return {"ok": True, "work_item_id": wi}

    @app.post("/api/run")
    def start_run(body: RunBody):
        from ..graph.state import Instrument
        inst = Instrument(
            id=body.instrument_id or f"{body.exchange}:{body.ticker}",
            ticker=body.ticker, exchange=body.exchange,
            currency=body.currency, lot_size=body.lot_size)
        wi = desk.start_run(inst, body.kind, trigger=body.trigger)
        return {"work_item_id": wi}

    @app.get("/status")
    def status():
        counts = {r["state"]: r["n"] for r in conn.execute(
            "SELECT state, COUNT(*) AS n FROM work_items GROUP BY state")}
        return {"ok": True, "work_items": counts, "ts": int(clock())}

    @app.get("/feed")
    def feed(since: int = 0):
        rows = [dict(r) for r in conn.execute(
            "SELECT id, item_id, ts, actor, from_state, to_state, payload"
            " FROM events WHERE id > ? ORDER BY id LIMIT 500", (since,))]
        return {"events": rows, "last": rows[-1]["id"] if rows else since}

    @app.post("/ask")
    async def ask(request: Request):
        if ask_handler is None:
            raise HTTPException(503, "chat agent not configured")
        body = await request.json()
        return ask_handler(body)

    @app.post("/agent/ask")
    async def agent_ask(request: Request):
        if agent_ask_handler is None:
            raise HTTPException(503, "agent ask not configured")
        body = await request.json()
        return agent_ask_handler(body)

    return app
