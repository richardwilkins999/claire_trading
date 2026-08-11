"""DB-backed job scheduling (schedules table). The systemd timers used to own
cadence; now the UI does — the claire-api scheduler thread and the dashboards
watcher loop both read this table live, so edits apply without restarts.

Two spec shapes:
  {"type": "interval", "minutes": 5}
  {"type": "daily", "time": "10:00", "tz": "Asia/Singapore",
   "days": [1..7 ISO weekdays], "region": "asia", "require_open": true}

Daily jobs never back-fire: a job that was due while the process was down runs
at its next occurrence, not immediately on boot (starting a stale market scan
hours late would be worse than skipping it).
"""
import json
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

DEFAULTS = [
    ("analysis_asia", "Screen + pipeline: SGX·HKEX·TSE·ASX", "claire-api",
     {"type": "daily", "time": "10:00", "tz": "Asia/Singapore",
      "days": [1, 2, 3, 4, 5], "region": "asia", "require_open": True}),
    ("analysis_eu", "Screen + pipeline: LSE·XETRA·PARIS", "claire-api",
     {"type": "daily", "time": "09:00", "tz": "Europe/London",
      "days": [1, 2, 3, 4, 5], "region": "eu", "require_open": True}),
    ("analysis_us", "Screen + pipeline: NASDAQ·NYSE", "claire-api",
     {"type": "daily", "time": "10:00", "tz": "America/New_York",
      "days": [1, 2, 3, 4, 5], "region": "us", "require_open": True}),
    ("custodian", "Fills, order TTLs, approval expiry, orphan checks",
     "claire-api", {"type": "interval", "minutes": 5}),
    ("reconcile", "Broker snapshots, drift check, desk.db backup",
     "claire-api", {"type": "daily", "time": "09:00", "tz": "local",
                    "days": [1, 2, 3, 4, 5, 6, 7]}),
    ("watcher", "Price alerts on open positions", "dashboards",
     {"type": "interval", "minutes": 10}),
]


# which agent a job invokes (shown on the schedule card); deterministic
# jobs run no LLM — that is a design property, not an omission
JOB_INVOKES = {
    "analysis_asia": {"agent": "screener",
                      "then": "full pipeline per pick"},
    "analysis_eu": {"agent": "screener", "then": "full pipeline per pick"},
    "analysis_us": {"agent": "screener", "then": "full pipeline per pick"},
    "custodian": {"agent": None, "then": None},
    "reconcile": {"agent": None, "then": None},
    "watcher": {"agent": None, "then": "sell_review pipeline on breach"},
}


def seed(conn):
    for job, desc, runs_in, spec in DEFAULTS:
        conn.execute(
            "INSERT OR IGNORE INTO schedules (job, description, runs_in, spec)"
            " VALUES (?,?,?,?)", (job, desc, runs_in, json.dumps(spec)))


def _tz(name):
    if not name or name == "local":
        return datetime.now().astimezone().tzinfo
    return ZoneInfo(name)


def validate_spec(spec: dict):
    if spec.get("type") == "interval":
        m = spec.get("minutes")
        if not isinstance(m, int) or not 1 <= m <= 1440:
            raise ValueError("minutes must be an integer 1..1440")
        return
    if spec.get("type") == "daily":
        h, m = str(spec.get("time", "")).split(":")
        if not (0 <= int(h) <= 23 and 0 <= int(m) <= 59):
            raise ValueError("time must be HH:MM")
        _tz(spec.get("tz", "local"))            # raises on unknown zone
        days = spec.get("days", [])
        if not days or not all(d in range(1, 8) for d in days):
            raise ValueError("days must be ISO weekdays 1..7")
        return
    raise ValueError("spec.type must be interval|daily")


def next_run_ts(spec: dict, last_run: int | None, now: int) -> int:
    if spec["type"] == "interval":
        return (last_run + spec["minutes"] * 60) if last_run else now
    tz = _tz(spec.get("tz", "local"))
    ref = datetime.fromtimestamp(last_run or now, tz)
    h, mi = (int(x) for x in spec["time"].split(":"))
    for d in range(9):
        cand = (ref + timedelta(days=d)).replace(hour=h, minute=mi, second=0,
                                                 microsecond=0)
        if cand <= ref:
            continue
        if cand.isoweekday() in spec.get("days", list(range(1, 8))):
            return int(cand.timestamp())
    raise ValueError("no valid day within 9 days — days list broken?")


def rows_with_next(conn, clock=time.time):
    now = int(clock())
    out = []
    for r in conn.execute("SELECT * FROM schedules ORDER BY job"):
        spec = json.loads(r["spec"])
        try:
            nxt = next_run_ts(spec, r["last_run_at"], now) if r["enabled"] \
                else None
        except Exception:                       # noqa: BLE001
            nxt = None
        invokes = JOB_INVOKES.get(r["job"], {"agent": None, "then": None})
        out.append({"job": r["job"], "description": r["description"],
                    "runs_in": r["runs_in"], "spec": spec,
                    "enabled": bool(r["enabled"]),
                    "agent": invokes["agent"], "then": invokes["then"],
                    "last_run_at": r["last_run_at"],
                    "last_result": json.loads(r["last_result"] or "null"),
                    "next_run_at": nxt})
    return out


def update(conn, job: str, *, enabled=None, spec_patch=None):
    row = conn.execute("SELECT * FROM schedules WHERE job=?", (job,)).fetchone()
    if row is None:
        raise KeyError(f"unknown job {job!r}")
    spec = json.loads(row["spec"])
    if spec_patch:
        spec.update({k: v for k, v in spec_patch.items() if v is not None})
        validate_spec(spec)
    if enabled is None:
        enabled = row["enabled"]
    conn.execute("UPDATE schedules SET spec=?, enabled=? WHERE job=?",
                 (json.dumps(spec), 1 if enabled else 0, job))


class Scheduler:
    """Runs inside claire-api. `runners`: job -> fn(spec) -> result dict.
    Jobs whose `runs_in` isn't this process (watcher) are displayed by the UI
    but executed elsewhere."""

    def __init__(self, conn, runners: dict, *, clock=time.time, poll=20):
        self.conn, self.runners, self.clock, self.poll = conn, runners, clock, poll

    def tick(self):
        now = int(self.clock())
        fired = []
        for r in self.conn.execute("SELECT * FROM schedules WHERE enabled=1"):
            job = r["job"]
            if job not in self.runners:
                continue
            spec = json.loads(r["spec"])
            try:
                if now < next_run_ts(spec, r["last_run_at"], now):
                    continue
            except Exception:                   # noqa: BLE001 — bad spec: skip
                continue
            try:
                result = self.runners[job](spec)
            except Exception as e:              # noqa: BLE001 — job errors are
                result = {"error": str(e)[:300]}  # data, never crashes
            self.conn.execute(
                "UPDATE schedules SET last_run_at=?, last_result=? WHERE job=?",
                (now, json.dumps(result, default=str), job))
            fired.append(job)
        return fired

    def start(self):
        def loop():
            while True:
                time.sleep(self.poll)
                try:
                    self.tick()
                except Exception:               # noqa: BLE001
                    pass
        threading.Thread(target=loop, daemon=True, name="scheduler").start()
