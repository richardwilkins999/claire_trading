"""db.tx / repo.tx: multi-step writes are one unit (REVIEW-2026-11-11 §3.3)."""
import pytest

from app.accounting import db
from app.accounting.repo import LedgerError, Repo

T0 = 1_786_456_800


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init(c)
    c.execute("INSERT INTO work_items (id, kind, ticker, state, thread_id,"
              " created_at, updated_at) VALUES ('wi', 'pipeline', 'X',"
              " 'running', 'wi', ?, ?)", (T0, T0))
    return c


def _state(conn):
    return conn.execute("SELECT state FROM work_items WHERE id='wi'"
                        ).fetchone()[0]


def _events(conn):
    return conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]


def test_everything_inside_commits_together(conn):
    with db.tx(conn):
        conn.execute("UPDATE work_items SET state='done' WHERE id='wi'")
        conn.execute("INSERT INTO events (item_id, ts, actor, from_state,"
                     " to_state) VALUES ('wi', ?, 't', 'running', 'done')",
                     (T0,))
        assert conn.in_transaction
    assert not conn.in_transaction
    assert _state(conn) == "done" and _events(conn) == 1


def test_an_error_undoes_every_write_in_the_block(conn):
    with pytest.raises(RuntimeError):
        with db.tx(conn):
            conn.execute("UPDATE work_items SET state='done' WHERE id='wi'")
            raise RuntimeError("crash between the two writes")
    assert _state(conn) == "running" and _events(conn) == 0
    assert not conn.in_transaction


def test_nested_blocks_belong_to_the_outer_one(conn):
    with pytest.raises(RuntimeError):
        with db.tx(conn):
            with db.tx(conn):                       # no second BEGIN
                conn.execute("UPDATE work_items SET state='done'"
                             " WHERE id='wi'")
            assert conn.in_transaction              # inner exit did not commit
            raise RuntimeError("outer fails after the inner block ended")
    assert _state(conn) == "running"


def test_set_state_writes_the_state_and_its_event_or_neither(conn,
                                                             monkeypatch):
    repo = Repo(conn)
    repo.set_state("wi", "awaiting_approval", actor="graph", ts=T0)
    assert _state(conn) == "awaiting_approval" and _events(conn) == 1

    def boom(*a, **k):
        raise RuntimeError("disk full writing the event")
    monkeypatch.setattr(repo, "_event", boom)
    with pytest.raises(RuntimeError):
        repo.set_state("wi", "done", actor="graph", ts=T0)
    # it used to be 'done' with no event to say how it got there
    assert _state(conn) == "awaiting_approval" and _events(conn) == 1
    with pytest.raises(LedgerError):
        repo.set_state("nope", "done", actor="graph", ts=T0)
    assert not conn.in_transaction


def test_set_state_inside_a_larger_unit_joins_it(conn):
    repo = Repo(conn)
    with pytest.raises(RuntimeError):
        with repo.tx():
            repo.set_state("wi", "done", actor="graph", ts=T0)
            raise RuntimeError("the larger unit fails")
    assert _state(conn) == "running" and _events(conn) == 0


def test_the_multi_step_writers_use_it():
    from pathlib import Path
    app = Path(db.__file__).resolve().parents[1]
    assert (app / "approvals.py").read_text().count("with tx(conn):") == 2
    assert "with tx(conn):" in (app / "order_control.py").read_text()


# ── schema version (REVIEW-2026-11-11 §3.4) ─────────────────────────────────
def test_a_database_records_which_migrations_it_has_had():
    c = db.connect(":memory:")
    assert db.schema_version(c) == 0
    db.init(c)
    assert db.schema_version(c) == len(db.MIGRATIONS) > 0


def test_applied_migrations_are_not_run_again(monkeypatch):
    c = db.connect(":memory:")
    db.init(c)
    # a statement that would fail if it ran: proof the list is skipped
    monkeypatch.setattr(db, "MIGRATIONS", list(db.MIGRATIONS))
    db.MIGRATIONS[0] = "THIS IS NOT SQL"
    db.init(c)                                      # no error
    # a NEW migration, appended, runs exactly once
    db.MIGRATIONS.append("ALTER TABLE watchlist ADD COLUMN test_col TEXT")
    db.init(c)
    assert "test_col" in [r[1] for r in c.execute(
        "PRAGMA table_info(watchlist)")]
    assert db.schema_version(c) == len(db.MIGRATIONS)
    db.init(c)                                      # and not a second time


def test_a_pre_version_database_is_brought_up_and_stamped():
    c = db.connect(":memory:")
    db.init(c)
    c.execute("PRAGMA user_version = 0")            # as every DB was before
    db.init(c)                                      # the old idempotent path
    assert db.schema_version(c) == len(db.MIGRATIONS)
