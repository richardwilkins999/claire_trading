"""The bin/claire control script's contract with the schema.

The script is bash, but the two things that make it dangerous are SQL: which
tables a reset wipes, and which rows count as "a trade in flight". Both are
parsed out of the script here so a schema rename cannot quietly turn a reset
into a config wipe, or make a graceful stop blind to a live order.
"""
import re
from pathlib import Path

from app.accounting import db

SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "claire"


def _tables():
    conn = db.connect(":memory:")
    db.init(conn)
    return {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def _reset_list():
    m = re.search(r"TABLES = \[(.*?)\]", SCRIPT.read_text(), re.S)
    return re.findall(r'"([a-z_]+)"', m.group(1))


def test_reset_only_touches_tables_that_exist():
    missing = set(_reset_list()) - _tables()
    assert not missing, f"reset would silently skip: {missing}"


def test_reset_keeps_configuration():
    """Wiping trades must not wipe the desk's identity — its providers,
    agents and prompts, schedules, venues or instruments."""
    keep = {"providers", "agents", "agent_versions", "schedules",
            "mcp_servers", "instruments", "exchange_sessions", "watchlist",
            "provider_health", "provider_models"}
    assert not (set(_reset_list()) & keep)


def test_reset_clears_everything_that_records_a_trade():
    """The inverse guard: a table that holds money or run history must be in
    the list, or a 'reset' leaves a half-book behind."""
    must_clear = {"orders", "executions", "lots", "lot_closures",
                  "cash_transactions", "work_items", "events", "agent_runs",
                  "agent_reports", "broker_accounts"}
    assert must_clear <= set(_reset_list())


def test_inflight_query_matches_the_order_lifecycle():
    """A graceful stop waits on orders that are not terminal. If someone adds
    a new terminal status, this catches the script still waiting forever."""
    conn = db.connect(":memory:")
    db.init(conn)
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='orders'"
                       ).fetchone()[0]
    statuses = set(re.findall(r"'(\w+)'",
                              sql[sql.find("status TEXT"):sql.find("),", sql.find("status TEXT"))]))
    excluded = set(re.search(
        r"status NOT IN\"\s*\n\s*\" \((.*?)\)", SCRIPT.read_text(), re.S
    ).group(1).replace("'", "").split(","))
    excluded = {s.strip() for s in excluded}
    live = statuses - excluded
    # these three mean the venue still owes us an answer
    assert live == {"pending_session", "placed", "partially_filled"}, live


def test_inflight_covers_authorised_but_unacked_approvals():
    """token_state='pending_resume' means the human approved and the resume
    was not acked — the one state where a trade can still appear after a
    stop, so a drain must wait for it."""
    assert "pending_resume" in SCRIPT.read_text()


def test_graceful_stop_pauses_and_restores_schedules():
    text = SCRIPT.read_text()
    assert "UPDATE schedules SET enabled=0" in text     # pause during drain
    assert "UPDATE schedules SET enabled=1" in text     # and put them back
    # restored on every exit path, or a Ctrl-C leaves the desk mute
    assert "trap restore_schedules EXIT INT TERM" in text


def test_reset_refuses_to_run_against_a_live_desk():
    text = SCRIPT.read_text()
    assert "stop the desk first" in text
    assert 'backup="$ROOT/var/backup-$stamp"' in text   # and backs up first
