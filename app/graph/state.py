"""Typed pipeline state (DESIGN.md §6, revision 2).

Every number an agent emits passes through these schemas at the moment of
production. Prose lives in `conditions` and narrative files — never in a
numeric field. Thesis prices are Decimal-friendly floats (views, not ledger
rows); the ledger's integer micro-units live in app/accounting.
"""
import operator
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

SUMMARY_MAX = 300


def _clip(v):
    """A too-long summary is a formatting slip, not a reason to throw away a
    completed analysis — clip it and keep the run alive. The full prose lives
    in the narrative file anyway."""
    if isinstance(v, str) and len(v) > SUMMARY_MAX:
        return v[:SUMMARY_MAX - 1].rstrip() + "…"
    return v


def _as_list(v):
    """Accept prose where a list was asked for. A model returning its key
    points as one string instead of a list is a formatting slip; rejecting it
    threw away a paid-for bull case and left the arbiter judging a one-sided
    debate. Split on newlines/bullets when they are there, else wrap."""
    if v is None:
        return []
    if isinstance(v, str):
        parts = [p.strip(" -•*\t") for p in v.splitlines()]
        parts = [p for p in parts if p]
        return parts if len(parts) > 1 else ([v.strip()] if v.strip() else [])
    if isinstance(v, (list, tuple)):
        return [x if isinstance(x, str) else str(x) for x in v]
    return [str(v)]


class Instrument(BaseModel):
    id: str                       # "SGX:C07", "NASDAQ:NVDA"
    ticker: str
    exchange: str
    currency: str
    name: str | None = None
    lot_size: int = 1             # SGX board lots etc. — sizing floors to this


class AnalystReport(BaseModel):
    ticker: str
    agent: Literal["fundamental", "technical", "news"]
    signal: Literal["bullish", "neutral", "bearish"]
    conviction: float = Field(ge=0.0, le=1.0)   # strength only — the signal
                                                # carries direction; 0 = abstain
    summary: str = Field(max_length=SUMMARY_MAX)
    # concrete facts WITH numbers — the summary alone was too thin a hand-off
    # for the debaters to reason over (the arbiter kept asking for figures
    # the analyst had actually found but had no room to pass on)
    key_findings: list[str] = []
    narrative_path: str = ""
    data_asof: datetime
    sources: list[str] = []

    _clip_summary = field_validator("summary", mode="before")(_clip)
    _lists = field_validator("key_findings", "sources",
                             mode="before")(_as_list)


class DebateCase(BaseModel):
    side: Literal["bull", "bear"]
    key_points: list[str]
    rebuttals: list[str] = []                   # bear: vs bull, point by point
    conviction: float = Field(ge=0.0, le=1.0)   # strength; `side` carries direction
    narrative_path: str = ""

    _lists = field_validator("key_points", "rebuttals",
                             mode="before")(_as_list)


class Thesis(BaseModel):
    ticker: str
    direction: Literal["buy", "sell", "pass"]
    conviction: float = Field(ge=0.0, le=1.0)
    entry_low: float | None = Field(default=None, gt=0)   # a range is two numbers,
    entry_high: float | None = Field(default=None, gt=0)  # never prose
    stop_loss: float | None = Field(default=None, gt=0)
    take_profit: float | None = None
    currency: str
    conditions: list[str] = []                  # caveats live HERE, as text
    narrative_path: str = ""

    _lists = field_validator("conditions", mode="before")(_as_list)

    @model_validator(mode="after")
    def _guards(self):
        if self.direction == "buy":
            assert self.entry_low and self.stop_loss, "buy thesis needs entry+stop"
            assert self.stop_loss < self.entry_low, "stop must be below entry"
        if self.direction == "sell" and self.entry_high and self.stop_loss:
            assert self.stop_loss > self.entry_high, \
                "sell/short stop must be ABOVE entry"
        return self


class Approval(BaseModel):
    status: Literal["approved", "rejected", "expired"]
    size_base: float | None = Field(default=None, gt=0)  # buys: spend, in the
                                                         # account's base currency
    qty: float | None = Field(default=None, gt=0)        # sells: shares to close
    trail_pct: float | None = Field(default=None, gt=0, le=90)
    """Trailing floor: how far below entry the watcher's stop-alert sits."""
    broker: Literal["alpaca", "saxo", "moomoo"] | None = None
    actor: str                                           # "human" | "reaper"
    token: str
    at: datetime


class PipelineState(BaseModel):
    work_item_id: str
    kind: Literal["pipeline", "sell_review", "override"] = "pipeline"
    ticker: str
    instrument: Instrument
    # why this run exists — the screener's rationale or the watcher's breach.
    # Analysts used to start blind; an exit review in particular had no idea
    # it was ordered because a stop was hit.
    trigger: str | None = None
    reports: Annotated[list[AnalystReport], operator.add] = []
    bull: DebateCase | None = None
    bear: DebateCase | None = None
    thesis: Thesis | None = None
    approval: Approval | None = None
    order_ids: Annotated[list[str], operator.add] = []
    errors: Annotated[list[str], operator.add] = []
