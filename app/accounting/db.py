"""desk.db connection — WAL + busy_timeout in every process (DESIGN.md §7)."""
import sqlite3
from pathlib import Path

SCHEMA = Path(__file__).with_name("schema.sql")


def connect(path) -> sqlite3.Connection:
    # explicit BEGIN in repo; cross-thread use is safe — sqlite3 is built
    # serialized and desk writes are single short transactions
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA.read_text())
