"""desk.db connection — WAL + busy_timeout in every process (DESIGN.md §7)."""
import sqlite3
from pathlib import Path

SCHEMA = Path(__file__).with_name("schema.sql")


def connect(path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)  # explicit BEGIN in repo
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA.read_text())
