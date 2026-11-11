"""app/states.py is the definition of every order, run and token state
(REVIEW-2026-11-11 §3.1). The queries still spell the names out, so this
checks that none of them names a state that does not exist."""
import re
from pathlib import Path

from app import states
from app.accounting import db
from app.accounting.repo import TERMINAL_ORDER
from app.maintenance import TERMINAL
from app.protection import WORKING

APP = Path(states.__file__).parent


def _flat(path):
    """Source with adjacent string literals joined, as Python sees them."""
    return re.sub(r'"\s*\n\s*f?"', "", path.read_text())


def _used(column_re):
    out = {}
    for p in APP.rglob("*.py"):
        flat = _flat(p)
        for m in re.finditer(column_re + r"\s*(?:NOT\s+)?IN\s*\(([^)]*)\)", flat):
            for v in re.findall(r"'([a-z_]+)'", m.group(1)):
                out.setdefault(v, set()).add(p.name)
        for m in re.finditer(column_re + r"\s*(?:=|!=)\s*'([a-z_]+)'", flat):
            out.setdefault(m.group(1), set()).add(p.name)
    return out


def test_groupings_are_consistent():
    assert set(states.ORDER_OPEN).isdisjoint(states.ORDER_TERMINAL)
    assert set(states.RUN_ACTIVE).isdisjoint(states.RUN_TERMINAL)
    assert TERMINAL_ORDER == set(states.ORDER_TERMINAL)
    assert WORKING == states.ORDER_AT_VENUE
    assert TERMINAL == states.RUN_TERMINAL
    assert states.sql_list(states.ORDER_AT_VENUE) == \
        "('placed','partially_filled')"


def test_order_states_match_the_schemas_check_constraint():
    conn = db.connect(":memory:")
    db.init(conn)
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='orders'"
                       ).fetchone()[0]
    check = re.search(r"status IN \(([^)]*)\)", sql).group(1)
    assert set(re.findall(r"'([a-z_]+)'", check)) == set(states.ORDER_STATES)


def test_no_query_names_a_run_or_token_state_that_does_not_exist():
    runs = _used(r"\b(?:w\.|wi\.)?state")
    unknown = {s: f for s, f in runs.items() if s not in states.RUN_STATES}
    assert not unknown, unknown
    tokens = _used(r"\btoken_state")
    unknown = {s: f for s, f in tokens.items()
               if s not in states.TOKEN_STATES}
    assert not unknown, unknown


def test_no_order_query_names_a_status_that_does_not_exist():
    """`status` is also a column of triggers, notices and health rows, so
    this looks only at queries that mention the orders table."""
    bad = {}
    for p in APP.rglob("*.py"):
        flat = _flat(p)
        for m in re.finditer(r'"([^"]*\borders\b[^"]*)"', flat):
            q = m.group(1)
            for lst in re.findall(r"\b(?:o\.)?status\s*(?:NOT\s+)?IN\s*\(([^)]*)\)", q):
                for v in re.findall(r"'([a-z_]+)'", lst):
                    if v not in states.ORDER_STATES:
                        bad.setdefault(v, set()).add(p.name)
    assert not bad, bad
