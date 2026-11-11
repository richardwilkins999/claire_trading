"""The arbiter's notes in plain English (Approvals card → "Arbiter's notes").

The arbiter's `conditions` are dense trader shorthand ("SMA20", "0.28x
volume", "CET1/ROTE blackout"). For the card, one text-only LLM call (the
`recorder` agent's model — it already writes the desk's plain-language run
records; metered like every call) rewrites them as one plain explanation per
note plus a one-line summary. Text in, text out: no tools, no numbers that
were not in the notes, and nothing here can touch an order.

Generated once per (work item, exact notes) and kept in `arbiter_notes`, so
reloading the page never pays twice. A failed or malformed answer is not
stored — the card falls back to the original notes and asks again next time.
"""
import hashlib
import json
import re
import threading
import time
import logging
from .logsetup import swallowed
from .accounting import db

log = logging.getLogger(__name__)

TABLE_SQL = db.table_ddl("arbiter_notes")      # schema.sql is the one copy

AGENT = "recorder"
MAX_NOTES = 20
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()

SYSTEM = """You explain a stock-trading AI's notes to the account owner, who \
is not a professional trader. You get the arbiter's notes on one proposed \
trade. Rewrite EACH note in plain English, in order, one entry per note.

Rules:
- Keep every number and price that matters (and its currency), but explain \
what it means. Spell out jargon briefly the first time: SMA (average price \
over N days), RSI, MACD, support / resistance, volume confirmation, ATR, \
CET1, ROTE, P/B, beta, catalyst, and similar.
- Say what it means for this trade: why the price was chosen, what could go \
wrong, what was not checked.
- 1-3 short sentences per note. Plain words, no hype, no advice beyond what \
the notes say. Never invent facts or numbers that are not in the notes.
- Also write a one-line summary (max 20 words) of the overall picture, e.g. \
"Low conviction: trend only, no financials or news, weak volume."

Answer with JSON only, no prose around it:
{"summary": "...", "notes": ["plain explanation of note 1", "..."]}"""


def ensure_table(conn):
    conn.execute(TABLE_SQL)


def notes_of(thesis_json):
    try:
        th = json.loads(thesis_json or "{}")
    except (TypeError, ValueError):
        return []
    return [c for c in (th.get("conditions") or [])
            if isinstance(c, str) and c.strip()][:MAX_NOTES]


def notes_hash(notes):
    return hashlib.sha256(json.dumps(notes).encode()).hexdigest()


def cached(conn, work_item_id, notes):
    ensure_table(conn)
    r = conn.execute("SELECT summary, explained_json FROM arbiter_notes"
                     " WHERE work_item_id=? AND notes_hash=?",
                     (work_item_id, notes_hash(notes))).fetchone()
    if r is None:
        return None
    return _shape(notes, r["summary"], json.loads(r["explained_json"]))


def _shape(notes, summary, explained):
    return {"summary": summary,
            "notes": [{"original": o, "plain": p}
                      for o, p in zip(notes, explained)]}


def parse(text, n):
    """(summary, [plain × n]) from the model's answer, or raise ValueError.
    Tolerates a ```json fence or prose around the object."""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise ValueError("no JSON object in the answer")
    data = json.loads(m.group(0))
    summary = data.get("summary")
    plain = data.get("notes")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("no summary")
    if not isinstance(plain, list) or len(plain) != n or not all(
            isinstance(p, str) and p.strip() for p in plain):
        raise ValueError(f"expected {n} explanations, got "
                         f"{len(plain) if isinstance(plain, list) else '?'}")
    return summary.strip()[:300], [p.strip()[:1200] for p in plain]


def _lock_for(wi):
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(wi, threading.Lock())


def explain(conn, work_item_id, *, llm_factory=None, clock=time.time):
    """{summary, notes:[{original, plain}]} for a work item's arbiter notes —
    from the cache, else one LLM call (stored on success). {"notes": []}
    when the item has none. Raises on an LLM failure (not cached)."""
    row = conn.execute("SELECT ticker, thesis_json FROM work_items WHERE id=?",
                       (work_item_id,)).fetchone()
    if row is None:
        raise KeyError("unknown work item")
    notes = notes_of(row["thesis_json"])
    if not notes:
        return {"summary": None, "notes": []}
    hit = cached(conn, work_item_id, notes)
    if hit:
        return hit
    with _lock_for(work_item_id):            # one call even if asked twice
        hit = cached(conn, work_item_id, notes)
        if hit:
            return hit
        if llm_factory is None:
            from .providers import registry

            def llm_factory():
                return registry.model_for(conn, AGENT,
                                          work_item_id=work_item_id)
        llm = llm_factory()
        listing = "\n".join(f"{i + 1}. {n}" for i, n in enumerate(notes))
        out = llm.invoke([("system", SYSTEM),
                          ("user", f"Trade: {row['ticker']}\n"
                                   f"Arbiter's notes ({len(notes)}):\n"
                                   f"{listing}")])
        text = out.content if isinstance(out.content, str) else \
            "".join(p.get("text", "") for p in out.content
                    if isinstance(p, dict))
        summary, plain = parse(text, len(notes))
        conn.execute("INSERT OR REPLACE INTO arbiter_notes VALUES"
                     " (?,?,?,?,?)", (work_item_id, notes_hash(notes),
                                      summary, json.dumps(plain),
                                      int(clock())))
        return _shape(notes, summary, plain)


# ── written ahead: as soon as a card reaches the gate ─────────────────────
RETRY_AFTER_S = 600


def pending(conn, *, limit=5):
    """Work items awaiting approval whose notes are not explained yet."""
    ensure_table(conn)
    out = []
    for r in conn.execute("SELECT id, thesis_json FROM work_items WHERE"
                          " state='awaiting_approval' ORDER BY created_at"):
        notes = notes_of(r["thesis_json"])
        if notes and cached(conn, r["id"], notes) is None:
            out.append(r["id"])
            if len(out) >= limit:
                break
    return out


def write_ahead_once(conn, *, failed, llm_factory=None, clock=time.time):
    """Explain every waiting card's notes, one LLM call at a time. A failure
    is retried after RETRY_AFTER_S, never in a tight loop."""
    done = []
    for wi in pending(conn):
        if clock() - failed.get(wi, 0) < RETRY_AFTER_S:
            continue
        try:
            explain(conn, wi, llm_factory=llm_factory, clock=clock)
            failed.pop(wi, None)
            done.append(wi)
        except Exception as e:                  # noqa: BLE001 — next pass
            failed[wi] = clock()
            log.warning("arbiter notes for %s not written yet: %s", wi,
                        str(e)[:200])
    return done


def start_writer_thread(conn, *, interval=30):
    failed = {}

    def loop():
        while True:
            try:
                write_ahead_once(conn, failed=failed)
            except Exception as e:              # noqa: BLE001 — keep going
                swallowed(log, "arbiter-notes writer pass failed", e,
                          trace=True)
            time.sleep(interval)
    t = threading.Thread(target=loop, daemon=True, name="arbiter-notes")
    t.start()
    return t
