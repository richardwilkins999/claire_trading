"""The states an order, a run and an approval token can be in — one place.

These were string literals at fifty-odd sites, and the "which orders are
still open" tuple was re-typed in SQL twenty-two times in four variants
(REVIEW-2026-11-11 §3.1); `cancelled` and `resuming` were in use but in no
comment anywhere. The SQL still spells the names out — rewriting forty
queries to interpolate constants buys little — but this module is the
definition, the code imports the groupings from here, and
tests/test_states.py fails when any query names a state that is not below.
"""

# ── orders.status (CHECK constraint in schema.sql) ──────────────────────
ORDER_PENDING = "pending_session"       # held by the desk until the market opens
ORDER_AT_VENUE = ("placed", "partially_filled")         # working at the broker
ORDER_OPEN = (ORDER_PENDING,) + ORDER_AT_VENUE          # can still fill
ORDER_TERMINAL = ("filled", "cancelled", "expired", "rejected")
ORDER_STATES = ORDER_OPEN + ORDER_TERMINAL

# ── work_items.state ────────────────────────────────────────────────────
RUN_ACTIVE = ("running", "awaiting_approval", "approved", "executing")
RUN_TERMINAL = ("done", "rejected", "expired", "failed", "cancelled")
RUN_STATES = RUN_ACTIVE + RUN_TERMINAL

# ── work_items.token_state: the single-use approval token ───────────────
# minted         shown on the card, not yet used
# pending_resume authorised; the graph has not yet acknowledged the resume
# resuming       the resume is in flight right now (claimed by one caller)
# burned         used, or expired — never valid again
TOKEN_STATES = ("minted", "pending_resume", "resuming", "burned")


def sql_list(states) -> str:
    """('a','b') for an IN clause. The values are this module's constants,
    never user input."""
    return "(" + ",".join(f"'{s}'" for s in states) + ")"
