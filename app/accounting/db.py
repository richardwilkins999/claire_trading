"""desk.db connection — WAL + busy_timeout in every process (DESIGN.md §7).

File databases get ONE CONNECTION PER THREAD (thread-local proxy): parallel
graph nodes and API worker threads each own an isolated connection, WAL +
busy_timeout arbitrate between them, and a repo transaction can never be
interleaved by another thread's statements. `:memory:` (tests) stays a single
shared connection.
"""
import sqlite3
import threading
from pathlib import Path

SCHEMA = Path(__file__).with_name("schema.sql")


def _raw_connect(path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None,  # explicit BEGIN in repo
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


class ThreadLocalConnection:
    """One real sqlite3 connection per thread, same file, same interface."""

    def __init__(self, path):
        self._path = str(path)
        self._local = threading.local()

    def _conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = _raw_connect(self._path)
            self._local.conn = c
        return c

    def __getattr__(self, name):
        return getattr(self._conn(), name)


def connect(path):
    if str(path) == ":memory:":
        return _raw_connect(path)
    return ThreadLocalConnection(path)


# additive migrations for databases created before a column existed;
# "duplicate column" just means it's already applied
MIGRATIONS = [
    "ALTER TABLE work_items ADD COLUMN archived_at INTEGER",
    "ALTER TABLE work_items ADD COLUMN trigger TEXT",
    # price is per MODEL, not per provider: billing haiku at sonnet's rate
    # overstated the news agent threefold while billing opus at sonnet's rate
    # understated the arbiter fivefold, and both errors fed the same total
    "ALTER TABLE provider_models ADD COLUMN cost_per_1k_in REAL",
    "ALTER TABLE provider_models ADD COLUMN cost_per_1k_out REAL",
    # cached input is billed at a different rate from fresh input
    "ALTER TABLE agent_runs ADD COLUMN cache_write_tokens INTEGER",
    "ALTER TABLE agent_runs ADD COLUMN cache_read_tokens INTEGER",
    # a human override points back at the PASS verdict it overturned, so the
    # approval card can show the evidence the desk declined on
    "ALTER TABLE work_items ADD COLUMN override_of TEXT",
    # a sell review points back at the run that opened the position, so it can
    # reuse that analysis instead of paying to think about the name again
    "ALTER TABLE work_items ADD COLUMN prior_run TEXT",
    # the best price seen since entry — highest for a long, LOWEST for a
    # short. Without it a "trailing" floor never moved off the entry price.
    "ALTER TABLE price_alerts ADD COLUMN peak_base REAL",
    # the old name described the old behaviour; anything still on it would
    # silently keep measuring from entry
    "UPDATE price_alerts SET rule='trail_pct' WHERE rule='drop_pct_from_entry'",
]


def init(conn) -> None:
    conn.executescript(SCHEMA.read_text())
    for stmt in MIGRATIONS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e):
                raise
