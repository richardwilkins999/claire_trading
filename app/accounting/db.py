"""desk.db connection — WAL + busy_timeout in every process (DESIGN.md §7).

File databases get ONE CONNECTION PER THREAD (thread-local proxy): parallel
graph nodes and API worker threads each own an isolated connection, WAL +
busy_timeout arbitrate between them, and a repo transaction can never be
interleaved by another thread's statements. `:memory:` (tests) stays a single
shared connection.
"""
import contextlib
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

    def release(self):
        """Close the CALLING thread's connection, if it opened one. Threads
        that live for one unit of work (a dashboard request) must call this
        when done: each connection holds desk.db + desk.db-wal open, and
        leaving them to thread teardown leaked ~800 descriptors in four hours
        until the dashboards process hit its 1024-file limit (2026-11-06).
        The next use on this thread simply opens a fresh connection."""
        c = getattr(self._local, "conn", None)
        if c is not None:
            self._local.conn = None
            c.close()

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
    # M4: the order row exists before the venue call, carrying the id the
    # venue is given; submitted_at is the claim that stops a second placement
    "ALTER TABLE orders ADD COLUMN client_order_id TEXT",
    "ALTER TABLE orders ADD COLUMN submitted_at INTEGER",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_client_id"
    " ON orders(client_order_id)",
    # decision time: queued (pending_session) orders count toward the daily
    # trade cap too — they used to be invisible to it until placed
    "ALTER TABLE orders ADD COLUMN created_at INTEGER",
    "UPDATE orders SET created_at=COALESCE(placed_at, updated_at)"
    " WHERE created_at IS NULL",
    # cash committed to open buys; without it two approvals could both pass
    # the cash check and the second fill would be refused at the venue's
    # expense (the sim fills instantly, the book debits at poll time)
    "ALTER TABLE orders ADD COLUMN reserved_base INTEGER",
    # the live-trading limits (DESIGN.md §22) measure orders in base currency
    "ALTER TABLE orders ADD COLUMN fx_rate INTEGER",
    # M7: trailing rules belong to a POSITION (instrument, account, side), and
    # there is exactly one. The old ON CONFLICT DO NOTHING had no constraint to
    # hit, so every approval appended another row. Dedupe (keep the newest —
    # it carries the latest threshold) BEFORE the unique index can be built.
    "ALTER TABLE price_alerts ADD COLUMN account_id TEXT",
    "ALTER TABLE price_alerts ADD COLUMN side INTEGER",
    "DELETE FROM price_alerts WHERE rule='trail_pct' AND id NOT IN"
    " (SELECT MAX(id) FROM price_alerts WHERE rule='trail_pct'"
    "  GROUP BY instrument_id, IFNULL(account_id,''), IFNULL(side,0))",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_alerts_trail_unique"
    " ON price_alerts(instrument_id, IFNULL(account_id,''), IFNULL(side,0))"
    " WHERE rule='trail_pct'",
    # Exits & protective legs (DESIGN.md §11a). A bracket's stop-loss and
    # take-profit rest AT THE VENUE as orders of their own; they used to live
    # only in the adapter's memory, so a stop that filled sold shares the
    # book never heard of, and a desk sell collided with them at Saxo
    # (SellOrdersAlreadyExistForOwnedContracts). Each leg is now an orders
    # row pointing at the entry it protects.
    "ALTER TABLE orders ADD COLUMN parent_order_id TEXT",
    "ALTER TABLE orders ADD COLUMN leg TEXT",
    # the desk order whose placement cancelled this leg — so the shares it
    # protected can be re-protected at the same levels if that order leaves
    # some of the position behind
    "ALTER TABLE orders ADD COLUMN cancelled_for TEXT",
    # on a desk order that cancelled legs: pending → done | not_needed |
    # failed. An unprotected position must never be silent.
    "ALTER TABLE orders ADD COLUMN protect_state TEXT",
    "ALTER TABLE orders ADD COLUMN protect_note TEXT",
    "ALTER TABLE orders ADD COLUMN protect_alerted_at INTEGER",
    # the trailing floor % approved with an entry: a venue that holds stops
    # (Saxo) gets the bracket's stop as a TRAILING stop at that distance.
    # On the row, not looked up at send time, so an order queued while the
    # market is shut carries it to the custodian that places it.
    "ALTER TABLE orders ADD COLUMN trail_pct REAL",
    "CREATE INDEX IF NOT EXISTS idx_orders_parent ON orders(parent_order_id)",
    # what the approval card should prefill for a manual trade (qty, size,
    # account, trailing floor) — the Sell button's "all 9 shares on Saxo SIM"
    "ALTER TABLE work_items ADD COLUMN suggested_json TEXT",
    # price triggers (DESIGN.md §15a). schema.sql creates the table on every
    # init; this copy keeps the migration list a complete record of what a
    # pre-existing desk.db gains, and is a no-op where the table exists.
    # Keep the two definitions identical.
    "CREATE TABLE IF NOT EXISTS price_triggers ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " ticker TEXT NOT NULL, exchange TEXT NOT NULL,"
    " direction TEXT NOT NULL CHECK(direction IN ('buy','sell')),"
    " kind TEXT NOT NULL CHECK(kind IN ('buy','exit','short')),"
    " condition TEXT NOT NULL CHECK(condition IN ('lte','gte')),"
    " trigger_price INTEGER NOT NULL CHECK(trigger_price > 0),"
    " limit_price INTEGER NOT NULL CHECK(limit_price > 0),"
    " qty INTEGER CHECK(qty IS NULL OR qty > 0),"
    " amount_base INTEGER CHECK(amount_base IS NULL OR amount_base > 0),"
    " stop_loss INTEGER, take_profit INTEGER, trail_pct REAL,"
    " broker TEXT NOT NULL, account_id TEXT NOT NULL,"
    " mode TEXT NOT NULL DEFAULT 'ask' CHECK(mode IN ('ask','auto')),"
    " note TEXT,"
    " status TEXT NOT NULL DEFAULT 'active'"
    "  CHECK(status IN ('active','fired','cancelled','expired','failed')),"
    " status_reason TEXT,"
    " created_at INTEGER NOT NULL, created_by TEXT NOT NULL,"
    " expires_at INTEGER NOT NULL,"
    " fired_at INTEGER, fired_price INTEGER, work_item_id TEXT,"
    " outcome TEXT,"
    " last_checked_at INTEGER, last_price INTEGER, last_error TEXT,"
    " updated_at INTEGER NOT NULL,"
    " CHECK((qty IS NULL) <> (amount_base IS NULL)),"
    " CHECK(NOT (mode = 'auto' AND broker = 'saxo_live')))",
    "CREATE INDEX IF NOT EXISTS idx_price_triggers_status"
    " ON price_triggers(status, ticker, exchange)",
    # Mission Control warnings you have cleared (key → when). A cleared
    # "failed runs" warning comes back only for NEW failures; any other
    # comes back after a day if it is still true, or at once if it changes.
    "CREATE TABLE IF NOT EXISTS dashboard_acks ("
    " key TEXT PRIMARY KEY, acked_at INTEGER NOT NULL)",
    # B8: a chat thread is a resumed SDK session; remembering its id here
    # means a restart does not cost each thread a recap turn
    "ALTER TABLE chat_threads ADD COLUMN sdk_session TEXT",
]


@contextlib.contextmanager
def tx(conn):
    """One atomic unit of writes: everything inside happens, or nothing does.

    The connection is in autocommit mode (isolation_level=None), so a
    sequence such as "mark the token used, write the audit event, save the
    edited levels" was three separate commits — a crash between them left a
    half-applied transition (REVIEW-2026-11-11 §3.3). Re-entrant: inside an
    open transaction this adds nothing and the outer one decides. Do NOT
    hold one across a network call: it holds SQLite's write lock."""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def table_ddl(name: str) -> str:
    """The CREATE TABLE statement for `name`, from schema.sql — for a module
    that must be able to create its own table on a bare connection."""
    import re
    m = re.search(rf"CREATE TABLE IF NOT EXISTS {re.escape(name)} \(.*?\n\)",
                  SCHEMA.read_text(), re.S)
    if m is None:
        raise KeyError(f"no table {name!r} in schema.sql")
    return m.group(0)


def schema_version(conn) -> int:
    """How many of MIGRATIONS this database has had applied."""
    return conn.execute("PRAGMA user_version").fetchone()[0]


def init(conn) -> None:
    """Bring a database up to date: schema.sql (every statement is IF NOT
    EXISTS), then the MIGRATIONS it has not had yet.

    The database records how far it has got in PRAGMA user_version. Before
    2026-11-11 there was no version: all the migrations ran on every start
    and "already applied" was detected by the words "duplicate column" in
    the error. A database from then reads as version 0, so it gets the whole
    list once more (harmlessly, the same way) and is then stamped.

    MIGRATIONS is APPEND-ONLY: a statement's position is its version
    number. Never reorder, edit or delete one; add at the end."""
    conn.executescript(SCHEMA.read_text())
    have = schema_version(conn)
    for n, stmt in enumerate(MIGRATIONS, start=1):
        if n <= have:
            continue
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as e:
            # a column schema.sql already has on a fresh database, or one a
            # pre-version database already gained
            if "duplicate column" not in str(e):
                raise
    _widen_provider_kinds(conn)
    _widen_account_environments(conn)
    if have < len(MIGRATIONS):
        conn.execute(f"PRAGMA user_version = {len(MIGRATIONS)}")


def _widen_provider_kinds(conn) -> None:
    """Databases created before the Claude Agent SDK provider have a CHECK
    on providers.kind that rejects 'claude_sdk'."""
    _widen_check(conn, "providers", "'anthropic','openai_compatible'",
                 "'anthropic','claude_sdk','openai_compatible'", "claude_sdk")


def _widen_account_environments(conn) -> None:
    """broker_accounts.environment used to reject 'live' outright. It must be
    representable so the live-trading switch (DESIGN.md §22) can refuse it
    by name; creating such an account still needs `bin/claire live setup`."""
    _widen_check(conn, "broker_accounts", "IN ('paper','sim'))",
                 "IN ('paper','sim','live'))",
                 # the marker must be the CHECK itself: the old table's
                 # comment ("-- no 'live'") contains the bare word
                 "'sim','live'")


def _widen_check(conn, table, old, new, marker) -> None:
    """SQLite cannot alter a CHECK, so rebuild the table in place
    (create-copy-drop-rename). Rows and ids are preserved, so every foreign
    key pointing at the table stays valid. legacy_alter_table keeps the
    rename from re-validating views that reference the table mid-swap."""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table'"
                       " AND name=?", (table,)).fetchone()
    if row is None or marker in row[0]:
        return
    new_sql = row[0].replace(old, new)
    if marker not in new_sql:
        raise RuntimeError(f"{table} has an unexpected CHECK; migrate it by "
                           "hand")
    for prefix in (f"CREATE TABLE IF NOT EXISTS {table}",
                   f"CREATE TABLE {table}"):
        if new_sql.startswith(prefix):
            new_sql = f"CREATE TABLE {table}_new" + new_sql[len(prefix):]
            break
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("PRAGMA legacy_alter_table=ON")
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute("SELECT sql FROM sqlite_master WHERE type='table'"
                           " AND name=?", (table,)).fetchone()
        if marker in cur[0]:                    # another process won the race
            conn.execute("COMMIT")
            return
        conn.execute(new_sql)
        conn.execute(f"INSERT INTO {table}_new SELECT * FROM {table}")
        conn.execute(f"DROP TABLE {table}")
        conn.execute(f"ALTER TABLE {table}_new RENAME TO {table}")
        bad = conn.execute("PRAGMA foreign_key_check").fetchall()
        if bad:
            raise RuntimeError(f"foreign key check failed: {list(bad)[:3]}")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA legacy_alter_table=OFF")
        conn.execute("PRAGMA foreign_keys=ON")
