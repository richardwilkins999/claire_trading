"""Typed pipeline state (DESIGN.md §6, revision 2).

Every number an agent emits passes through these schemas at the moment of
production. Prose lives in `conditions` and narrative files — never in a
numeric field. Thesis prices are Decimal-friendly floats (views, not ledger
rows); the ledger's integer micro-units live in app/accounting.
"""
import operator
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator


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
    summary: str = Field(max_length=300)
    narrative_path: str = ""
    data_asof: datetime
    sources: list[str] = []


class DebateCase(BaseModel):
    side: Literal["bull", "bear"]
    key_points: list[str]
    rebuttals: list[str] = []                   # bear: vs bull, point by point
    conviction: float = Field(ge=0.0, le=1.0)   # strength; `side` carries direction
    narrative_path: str = ""


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
    broker: Literal["alpaca", "saxo", "moomoo"] | None = None
    actor: str                                           # "human" | "reaper"
    token: str
    at: datetime


class PipelineState(BaseModel):
    work_item_id: str
    kind: Literal["pipeline", "sell_review"] = "pipeline"
    ticker: str
    instrument: Instrument
    reports: Annotated[list[AnalystReport], operator.add] = []
    bull: DebateCase | None = None
    bear: DebateCase | None = None
    thesis: Thesis | None = None
    approval: Approval | None = None
    order_ids: Annotated[list[str], operator.add] = []
    errors: Annotated[list[str], operator.add] = []
