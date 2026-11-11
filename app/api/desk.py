"""Run lifecycle around the pipeline graph (DESIGN.md §10).

The Desk starts runs, notices interrupts, mints single-use approval tokens
with session-aware expiry, and applies authorized resumes. It never decides —
authorization happened upstream (dashboards) and validation happens in the
API layer; the Desk just keeps graph and desk.db in step.
"""
import json
import re
import secrets
import threading
import time
from datetime import datetime, timezone

from langgraph.types import Command

from .. import sessions
from ..graph.pipeline import (build_override_pipeline, build_pipeline,
                              build_sell_review_pipeline)
from ..graph.state import Instrument, PipelineState, Thesis
from ..accounting import holdings

# The ticker is baked into the work-item id, and that id is rendered into
# pages, handler attributes and narrative paths. Every run starter (the HTTP
# route, the schedules, Claire's run_pipeline tool, the watcher) funnels
# through start_run, so this is the one place a ticker carrying markup or a
# path can be refused. Real symbols only need letters, digits, '.', '-' (BRK.B,
# RDS-A, 0700, C07) and '&' (NSE's M&M) — none of which can open a tag, break
# out of a quoted attribute or climb a directory ('..' is refused below).
TICKER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.&-]{0,19}")
EXCHANGE_RE = re.compile(r"[A-Z][A-Z0-9]{1,9}")


# What a manual trade may ask its approval card to prefill. Suggestions,
# never decisions: the human still sees, edits and approves every one, and
# thesis_action re-checks whatever is finally sent.
SUGGEST_KEYS = ("qty", "size_base", "broker", "trail_pct", "order_type")
BROKERS = ("alpaca", "saxo", "moomoo", "saxo_live")
SOURCE_RE = re.compile(r"[a-z][a-z0-9_-]{0,23}")


class Conflict(ValueError):
    """The request is well-formed but clashes with the desk's state (e.g. the
    same manual trade is already in flight) — HTTP 409, not 400."""


def clean_suggested(s):
    """Validate a manual trade's prefill: {qty, size_base, broker, trail_pct},
    all optional. Unknown keys are refused rather than stored, so a typo in
    a caller is loud."""
    import math
    if s is None:
        return None
    if not isinstance(s, dict):
        raise ValueError("suggested must be an object")
    bad = set(s) - set(SUGGEST_KEYS)
    if bad:
        raise ValueError(f"unknown suggested field(s) {sorted(bad)}")
    out = {}
    for k in ("qty", "size_base", "trail_pct"):
        v = s.get(k)
        if v is None:
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)) or \
                not math.isfinite(v) or v <= 0 or v > 1e9:
            raise ValueError(f"suggested {k} must be a positive number")
        if k == "trail_pct" and v > 90:
            raise ValueError("suggested trail_pct must be at most 90")
        out[k] = float(v)
    if s.get("broker") is not None:
        if s["broker"] not in BROKERS:
            raise ValueError(f"suggested broker must be one of {BROKERS}")
        out["broker"] = s["broker"]
    if s.get("order_type") is not None:
        if s["order_type"] not in ("limit", "market"):
            raise ValueError("suggested order_type must be limit or market")
        out["order_type"] = s["order_type"]
    return out or None


def validate_symbol(ticker, exchange):
    """Raise ValueError unless ticker/exchange look like real venue codes.
    Importable by the API layer so RunBody can 422 before a run exists."""
    if not isinstance(ticker, str) or not TICKER_RE.fullmatch(ticker) \
            or ".." in ticker:
        raise ValueError(f"invalid ticker {str(ticker)[:40]!r}")
    if not isinstance(exchange, str) or not EXCHANGE_RE.fullmatch(exchange):
        raise ValueError(f"invalid exchange {str(exchange)[:40]!r}")


class Desk:
    def __init__(self, conn, deps, checkpointer, repo, *, clock=time.time,
                 session_cal=None):
        self.conn, self.repo = conn, repo
        self.pipeline = build_pipeline(deps, checkpointer)
        self.override_pipeline = build_override_pipeline(deps, checkpointer)
        self.sell_pipeline = build_sell_review_pipeline(deps, checkpointer)
        self.clock = clock
        self.cal = session_cal          # None → sessions.DEFAULTS

    def cfg(self, wi):
        return {"configurable": {"thread_id": wi}}

    # ── starting runs ────────────────────────────────────────────────────
    def start_run(self, instrument, kind="pipeline", *, background=True,
                  trigger=None):
        # before create_work_item: a failed run still leaves its row behind,
        # so a bad ticker must never get as far as an id
        validate_symbol(instrument.ticker, instrument.exchange)
        ts = int(self.clock())
        day = time.strftime("%Y-%m-%d", time.gmtime(ts))
        wi = f"wi_{day}_{instrument.ticker}_{secrets.token_hex(2)}"
        self.repo.create_work_item(wi, kind, instrument.ticker, ts=ts)
        if trigger:
            self.conn.execute("UPDATE work_items SET trigger=? WHERE id=?",
                              (trigger, wi))
        prior, evidence = ("", "")
        if kind == "sell_review":
            prior, evidence = self._prior_analysis(instrument.ticker)
            if prior:
                self.conn.execute(
                    "UPDATE work_items SET prior_run=? WHERE id=?", (prior, wi))
        state = PipelineState(work_item_id=wi, kind=kind, trigger=trigger,
                              ticker=instrument.ticker, instrument=instrument,
                              prior_evidence=evidence)
        if background:
            threading.Thread(target=self._run, args=(wi, state),
                             daemon=True, name=f"run-{wi}").start()
        else:
            self._run(wi, state)
        return wi

    def _prior_analysis(self, ticker):
        """The run that opened this position, rendered for the arbiter. An
        exit review inherits it rather than re-deriving it: the question at a
        stop is 'has anything changed', and you cannot answer that without
        what was believed at entry."""
        row = self.conn.execute(
            "SELECT id, thesis_json FROM work_items WHERE ticker=?"
            " AND kind IN ('pipeline','override') AND thesis_json IS NOT NULL"
            " ORDER BY created_at DESC LIMIT 1", (ticker,)).fetchone()
        if row is None:
            return "", ""
        parts = []
        try:
            t = json.loads(row["thesis_json"])
            parts.append(
                f"### Verdict at entry: {str(t.get('direction')).upper()} "
                f"(conviction {t.get('conviction')})\n"
                f"entry {t.get('entry_low')}–{t.get('entry_high')}, "
                f"stop {t.get('stop_loss')}, target {t.get('take_profit')}\n"
                + "\n".join(f"- {c}" for c in (t.get('conditions') or [])))
        except ValueError:
            pass
        for rep in self.conn.execute(
                "SELECT agent_id, payload FROM agent_reports"
                " WHERE work_item_id=? ORDER BY id", (row["id"],)):
            try:
                d = json.loads(rep["payload"])
            except ValueError:
                continue
            head = d.get("signal") or d.get("side") or d.get("direction") or ""
            body = d.get("summary") or ""
            finds = "\n".join(f"- {f}" for f in (d.get("key_findings")
                                                 or d.get("key_points") or []))
            parts.append(f"### {rep['agent_id']} at entry — {head}\n{body}"
                         + (f"\n{finds}" if finds else ""))
        return row["id"], "\n\n".join(parts)

    def _run(self, wi, state):
        try:
            self._pipeline_for(wi).invoke(state, self.cfg(wi))
        except Exception as e:                      # noqa: BLE001
            self.repo.set_state(wi, "failed", actor="system",
                                ts=int(self.clock()),
                                payload={"error": str(e)[:300]})
            return
        self._sync(wi)

    # ── human override of a PASS verdict (DESIGN.md §10) ─────────────────
    def override(self, origin_wi, *, direction, entry, stop, target=None,
                 note=None, trail_pct=None, qty=None, broker=None,
                 order_type=None, background=True):
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
            " AND state NOT IN ('rejected','expired','failed','cancelled')",
            (origin_wi,)).fetchone()
        if prior:                       # one live override per verdict
            raise ValueError(f"already overridden by {prior['id']} "
                             f"({prior['state']})")

        if direction == "buy" and not stop:
            raise ValueError("a buy needs a stop below the entry")
        if direction == "sell" and not stop:
            # no stop is only coherent for an EXIT: selling shares already
            # held. Selling what is not held is a short, whose loss has no
            # ceiling — that must carry a stop above the entry.
            held = self.desk_held(origin["ticker"])
            if held <= 0:
                raise ValueError("the desk holds no shares of "
                                 f"{origin['ticker']}, so this sell is a "
                                 "SHORT — it needs a stop ABOVE the entry")
        # raises if the levels are incoherent — that guard is the point
        thesis = Thesis(ticker=origin["ticker"], direction=direction,
                        conviction=old.get("conviction") or 0.0,
                        entry_low=entry, entry_high=entry,
                        stop_loss=stop, take_profit=target,
                        currency=old.get("currency") or "USD",
                        conditions=(old.get("conditions") or [])
                        + [f"HUMAN OVERRIDE of a PASS verdict"
                           f"{': ' + note if note else ''}"])
        # what you chose here prefills the approval card: the trailing floor
        # (not on an exit — it closes the position, there is no floor to
        # carry), the shares, the account and the order type
        exit_ = direction == "sell" and not stop
        sug = {"trail_pct": None if exit_ else trail_pct, "qty": qty,
               "broker": broker, "order_type": order_type}
        suggested = clean_suggested({k: v for k, v in sug.items()
                                     if v is not None} or None)
        inst = self._instrument(origin["ticker"], thesis.currency)
        ts = int(self.clock())
        day = time.strftime("%Y-%m-%d", time.gmtime(ts))
        wi = f"wi_{day}_{origin['ticker']}_ovr_{secrets.token_hex(2)}"
        self.repo.create_work_item(wi, "override", origin["ticker"], ts=ts)
        self.conn.execute(
            "UPDATE work_items SET override_of=?, trigger=?, thesis_json=?,"
            " suggested_json=? WHERE id=?",
            (origin_wi, f"human override of {origin_wi}",
             thesis.model_dump_json(),
             json.dumps(suggested) if suggested else None, wi))
        self.repo._event(wi, ts, "human", "created", "override",
                         payload={"origin": origin_wi, "direction": direction,
                                  "entry": entry, "stop": stop,
                                  "target": target, "trail_pct":
                                  (suggested or {}).get("trail_pct"),
                                  "note": note})
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

    # ── a trade YOU start (the Sell button; later, price triggers) ────────
    def manual_trade(self, ticker, exchange, direction, entry, stop=None,
                     target=None, note=None, suggested=None, *,
                     source="manual", background=True):
        """Put a trade you decided on in front of the approval gate — no
        analysis, no PASS to overturn. Same money path as every other trade
        from the gate on: a minted token, the dashboard card, the cash and
        risk checks, the live-trading locks, and execution's leg handling.

        Levels go through the same Thesis validator as an agent's: a buy
        needs a stop below the entry; a sell of shares the desk holds is an
        EXIT and needs none (execution sells at most what is held); a sell of
        shares it does NOT hold is a SHORT and must carry a stop above the
        entry. The instrument is resolved by the graph's own `prepare` step
        (currency, board lot), exactly as for a screened run.

        `suggested` prefills the card (qty, size_base, broker, trail_pct);
        `source` says where the trade came from (portfolio, saxo, watchlist,
        trigger) and is shown on the card. Returns the work-item id.
        Raises ValueError (bad input), pydantic ValidationError (incoherent
        levels) or Conflict (the same trade is already in flight)."""
        from ..tools.market import currency_for, yahoo_symbol
        validate_symbol(ticker, exchange)
        if direction not in ("buy", "sell"):
            raise ValueError("direction must be buy or sell")
        if not isinstance(source, str) or not SOURCE_RE.fullmatch(source):
            raise ValueError("invalid source")
        if note is not None and (not isinstance(note, str) or len(note) > 200):
            raise ValueError("note must be text of at most 200 characters")
        for name, v in (("entry", entry), ("stop", stop), ("target", target)):
            if v is not None and (isinstance(v, bool) or
                                  not isinstance(v, (int, float)) or v <= 0):
                raise ValueError(f"{name} must be a positive price")
        if entry is None:
            raise ValueError("an entry (limit) price is required")
        suggested = clean_suggested(suggested)
        inst_row = self.conn.execute(
            "SELECT * FROM instruments WHERE ticker=? AND exchange=? LIMIT 1",
            (ticker, exchange)).fetchone()
        held = self._held_instrument(inst_row["id"]) if inst_row else 0.0
        if direction == "buy" and not stop:
            raise ValueError("a buy needs a stop below the entry")
        if direction == "sell" and not stop and held <= 0:
            raise ValueError(f"the desk holds no shares of {ticker}, so this "
                             "sell is a SHORT — it needs a stop ABOVE the "
                             "entry")
        what = ("EXIT (sell shares the desk holds)"
                if direction == "sell" and held > 0 else
                "SHORT" if direction == "sell" else "BUY")
        ccy = currency_for(yahoo_symbol(ticker, exchange))
        ccy = "GBP" if ccy == "GBp" else ccy
        thesis = Thesis(ticker=ticker, direction=direction, conviction=0.0,
                        entry_low=entry, entry_high=entry, stop_loss=stop,
                        take_profit=target, currency=ccy,
                        conditions=[f"MANUAL {what} — you started this from "
                                    f"{source}" + (f": {note}" if note
                                                   else "")])
        dup = self.conn.execute(
            "SELECT id, state FROM work_items WHERE kind='manual' AND ticker=?"
            " AND state IN ('running','awaiting_approval','approved',"
            "'executing') AND thesis_json LIKE ? ORDER BY created_at DESC"
            " LIMIT 1", (ticker, f'%"direction":"{direction}"%')).fetchone()
        if dup:                         # a double click is not two trades
            raise Conflict(f"a manual {direction} of {ticker} is already "
                           f"{dup['state']} ({dup['id']})")
        inst = Instrument(id=inst_row["id"] if inst_row else
                          f"{exchange}:{ticker}", ticker=ticker,
                          exchange=exchange, currency=ccy,
                          name=inst_row["name"] if inst_row else None)
        ts = int(self.clock())
        day = time.strftime("%Y-%m-%d", time.gmtime(ts))
        wi = f"wi_{day}_{ticker}_man_{secrets.token_hex(2)}"
        trigger = f"manual {direction} from {source}" + (
            f": {note}" if note else "")
        self.repo.create_work_item(wi, "manual", ticker, ts=ts)
        prior = self._prior_analysis(ticker)[0] if what.startswith("EXIT") \
            else ""
        self.conn.execute(
            "UPDATE work_items SET trigger=?, thesis_json=?, suggested_json=?,"
            " prior_run=? WHERE id=?",
            (trigger, thesis.model_dump_json(),
             json.dumps(suggested) if suggested else None, prior or None, wi))
        self.repo._event(wi, ts, "human", "created", "manual",
                         payload={"source": source, "direction": direction,
                                  "entry": entry, "stop": stop,
                                  "target": target, "note": note,
                                  "suggested": suggested})
        state = PipelineState(work_item_id=wi, kind="manual", trigger=trigger,
                              ticker=ticker, instrument=inst, thesis=thesis)
        if background:
            threading.Thread(target=self._run_override, args=(wi, state),
                             daemon=True, name=f"man-{wi}").start()
        else:
            self._run_override(wi, state)
        return wi

    def _held_instrument(self, instrument_id) -> float:
        return float(holdings.net(self.conn, instrument_id=instrument_id))

    def amend(self, wi, *, entry, stop=None, target=None, actor="human"):
        """Change the levels of an approved trade whose placement was
        REFUSED, before the human retries it (e.g. Saxo's
        TooFarFromEntryOrder: the stop or target sits too far from the
        entry). Only a parked, refused approval can be amended; the new
        levels pass the same Thesis validator as any agent's or override's,
        and the change is written to the graph's own state — execution reads
        the thesis from there, not from desk.db."""
        from .. import approvals
        row = self.work_item(wi)
        if row is None:
            raise ValueError("unknown work item")
        if row["state"] != "approved" or row["token_state"] != \
                "pending_resume" or approvals.last_event(self.conn, wi) != \
                "resume_failed":
            raise ValueError("only a refused, approved trade can be edited")
        graph, cfg = self._pipeline_for(wi), self.cfg(wi)
        snap = graph.get_state(cfg)
        if tuple(snap.next) != ("execute",):
            raise ValueError(f"trade is not waiting to execute ({snap.next})")
        old = snap.values["thesis"]
        data = old.model_dump()
        data.update(entry_low=entry, entry_high=entry, stop_loss=stop,
                    take_profit=target)
        if data["direction"] == "buy" and not stop:
            raise ValueError("a buy needs a stop below the entry")
        new = Thesis.model_validate(data)            # same guards as always
        # as the gate: the graph's next step stays `execute`, which re-runs
        # with these levels when the human retries
        graph.update_state(cfg, {"thesis": new}, as_node="gate")
        ts = int(self.clock())
        self.conn.execute("UPDATE work_items SET thesis_json=?, updated_at=?"
                          " WHERE id=?", (new.model_dump_json(), ts, wi))
        self.repo._event(wi, ts, actor, "approved", "amended",
                         payload={"from": {"entry": old.entry_high,
                                           "stop": old.stop_loss,
                                           "target": old.take_profit},
                                  "to": {"entry": entry, "stop": stop,
                                         "target": target}})
        # still a refused approval, waiting for the human's retry
        self.repo._event(wi, ts, "system", "approved", "resume_failed",
                         payload={"error": "levels amended — retry to send"})
        return new

    def desk_held(self, ticker) -> float:
        """Shares the desk's book holds in `ticker`, net, across accounts."""
        return float(holdings.net(self.conn, ticker=ticker))

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
        """Each run kind has its own graph, and resuming against the wrong one
        would look for nodes its checkpoint never ran."""
        row = self.work_item(wi)
        if row is None:
            return self.pipeline
        if row["kind"] == "sell_review":
            return self.sell_pipeline
        if row["override_of"] or row["kind"] == "manual":
            return self.override_pipeline
        return self.pipeline

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
