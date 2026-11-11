"""service_health rows: what each loop last found.

The custodian wrote a full JSON report every five minutes whether or not
anything had changed — 2,038 nearly identical "ok" rows in a week
(REVIEW-2026-11-11 §4.3). record() keeps ONE row while a service keeps
reporting the same thing, moving its checked_at forward as a heartbeat, and
adds a row only when the status or the report changes. The latest row per
service is still "now"; the rows before it are the history of changes.
"""
import json


def record(conn, service, ok, report, ts) -> bool:
    """Store `report` for `service` at `ts`. → True if a new row was written,
    False if the previous identical one was only moved forward."""
    detail = json.dumps(report, default=str)
    last = conn.execute(
        "SELECT rowid, ok, detail FROM service_health WHERE service=?"
        " ORDER BY rowid DESC LIMIT 1", (service,)).fetchone()
    if last is not None and last["ok"] == ok and last["detail"] == detail:
        conn.execute("UPDATE service_health SET checked_at=? WHERE rowid=?",
                     (ts, last["rowid"]))
        return False
    conn.execute(
        "INSERT INTO service_health (service, checked_at, ok, detail)"
        " VALUES (?,?,?,?)", (service, ts, ok, detail))
    return True
