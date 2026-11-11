"""DB-backed job scheduling (schedules table). The systemd timers used to own
cadence; now the UI does — the claire-api scheduler thread and the dashboards
watcher loop both read this table live, so edits apply without restarts.

Two spec shapes:
  {"type": "interval", "minutes": 5}
  {"type": "daily", "time": "10:00", "tz": "Asia/Singapore",
   "days": [1..7 ISO weekdays], "region": "asia", "require_open": true}

Daily firing rule (stateless — derived from the spec, `last_run_at` and now):
a daily job fires when its most recent scheduled slot is at most
DAILY_GRACE_SECONDS in the past and it has not run since that slot. So
  * a job that has never run (`last_run_at` NULL) fires at its next slot;
  * a slot missed because the process was down (or the job was disabled)
    is SKIPPED once the grace window has passed — the job waits for its next
    slot instead of firing hours late (a stale "pre-open" market scan running
    mid-session would be worse than skipping it);
  * a short outage (a restart, a slow tick) inside the grace window still
    fires the slot, slightly late.

"tz": "local" means the host's real IANA zone (TZ, then /etc/localtime), so
DST transitions are honoured; it is resolved on every call, not cached.
"""
import json
import os
import threading
import time
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import logging
from .logsetup import swallowed

log = logging.getLogger(__name__)

# how late a daily slot may still fire (restart / tick jitter); beyond this a
# missed slot is skipped. The scheduler polls every 20 s, so 10 min is ample.
DAILY_GRACE_SECONDS = 10 * 60

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
    ("event_scan", "Intraday unusual-volume sweep on open markets "
     "(no LLM; wakes the pipeline only on a real move)", "claire-api",
     {"type": "interval", "minutes": 60, "require_open": True,
      "threshold": 3.0, "max_runs": 1}),
    # price triggers (DESIGN.md §15a): one-shot "buy/sell when price ≤/≥ X"
    # rules from the Watchlist page. Deterministic; evaluated only while
    # each rule's market is open, so an overnight quote never fires one.
    ("price_triggers", "Price triggers from the Watchlist (one-shot buy/sell"
     " rules; card, or pre-approved paper order)", "claire-api",
     {"type": "interval", "minutes": 2, "require_open": True}),
    # the maintainer (DESIGN.md §23): checks, log rotation, backups,
    # retention, then a short AI status note. Quiet hour, after US close.
    ("maintenance", "Health checks, log rotation, backups, retention; "
     "AI status note (Maintenance page)", "claire-api",
     {"type": "daily", "time": "06:30", "tz": "local",
      "days": [1, 2, 3, 4, 5, 6, 7]}),
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
    "event_scan": {"agent": None, "then": "full pipeline on a volume spike"},
    "maintenance": {"agent": "maintainer",
                    "then": "status note only — plain code does the cleanup"},
    "price_triggers": {"agent": None,
                       "then": "approval card (or pre-approved paper order)"},
}


def seed(conn):
    for job, desc, runs_in, spec in DEFAULTS:
        conn.execute(
            "INSERT OR IGNORE INTO schedules (job, description, runs_in, spec)"
            " VALUES (?,?,?,?)", (job, desc, runs_in, json.dumps(spec)))


def _local_zone():
    """The host's IANA zone, DST-aware. Order: $TZ, the /etc/localtime
    symlink target, /etc/timezone, the raw /etc/localtime TZif file, and
    only as a last resort the current fixed UTC offset."""
    tzenv = os.environ.get("TZ", "").lstrip(":")
    if tzenv:
        try:
            return ZoneInfo(tzenv)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    try:
        target = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in target:
            return ZoneInfo(target.split("zoneinfo/", 1)[1])
    except (OSError, ZoneInfoNotFoundError, ValueError):
        pass
    try:
        with open("/etc/timezone") as f:
            return ZoneInfo(f.read().strip())
    except (OSError, ZoneInfoNotFoundError, ValueError):
        pass
    try:
        with open("/etc/localtime", "rb") as f:
            return ZoneInfo.from_file(f, key="localtime")
    except (OSError, ValueError):
        pass
    return datetime.now().astimezone().tzinfo


def _tz(name):
    if not name or name == "local":
        return _local_zone()
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


def _slot(d: date, h: int, mi: int, tz) -> int:
    # wall-clock HH:MM on that date in tz; DST gaps/folds resolve via fold=0
    return int(datetime.combine(d, dtime(h, mi), tzinfo=tz).timestamp())


def _daily_slots(spec):
    tz = _tz(spec.get("tz", "local"))
    h, mi = (int(x) for x in spec["time"].split(":"))
    days = spec.get("days") or list(range(1, 8))
    return tz, h, mi, days


def prev_slot_ts(spec: dict, now: int) -> int | None:
    """Latest scheduled daily slot at or before `now` (within 8 days)."""
    tz, h, mi, days = _daily_slots(spec)
    today = datetime.fromtimestamp(now, tz).date()
    for back in range(9):
        d = today - timedelta(days=back)
        if d.isoweekday() in days:
            ts = _slot(d, h, mi, tz)
            if ts <= now:
                return ts
    return None


def _next_slot_after(spec: dict, ref: int) -> int:
    tz, h, mi, days = _daily_slots(spec)
    start = datetime.fromtimestamp(ref, tz).date()
    for d in range(9):
        day = start + timedelta(days=d)
        if day.isoweekday() in days:
            ts = _slot(day, h, mi, tz)
            if ts > ref:
                return ts
    raise ValueError("no valid day within 9 days — days list broken?")


def next_run_ts(spec: dict, last_run: int | None, now: int) -> int:
    """When the job should (have) run; a job is due when now >= this.

    Daily: if the latest slot is inside the grace window and has not been run
    (last_run NULL or older than the slot), that slot is returned (<= now, so
    it fires). Otherwise the next slot after now — a missed slot older than
    DAILY_GRACE_SECONDS is skipped, never back-fired."""
    if spec["type"] == "interval":
        return (last_run + spec["minutes"] * 60) if last_run else now
    prev = prev_slot_ts(spec, now)
    if (prev is not None and now - prev <= DAILY_GRACE_SECONDS
            and (last_run is None or last_run < prev)):
        return prev
    return _next_slot_after(spec, now)


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


SLOW_JOB_S = 60


class Scheduler:
    """Runs inside claire-api. `runners`: job -> fn(spec) -> result dict.
    Jobs whose `runs_in` isn't this process (watcher) are displayed by the UI
    but executed elsewhere."""

    def __init__(self, conn, runners: dict, *, clock=time.time, poll=20):
        self.conn, self.runners, self.clock, self.poll = conn, runners, clock, poll

    def tick(self):
        now = int(self.clock())
        fired = []
        # fetchall FIRST: iterating the cursor kept a read snapshot open on
        # this connection while jobs ran. A job that makes ANOTHER connection
        # write (the custodian's resume retry → claire-api's request thread)
        # left that snapshot stale, SQLite refused to upgrade it to a write
        # ("database is locked", immediately — busy_timeout does not apply),
        # last_run_at was never saved, and the job re-fired on every 20 s tick.
        rows = self.conn.execute(
            "SELECT * FROM schedules WHERE enabled=1").fetchall()
        for r in rows:
            job = r["job"]
            if job not in self.runners:
                continue
            spec = json.loads(r["spec"])
            try:
                if now < next_run_ts(spec, r["last_run_at"], now):
                    continue
            except Exception as e:              # noqa: BLE001 — bad spec: skip
                swallowed(log, f"schedule {job!r} has a spec that cannot be "
                               "read; it is not running", e, every=3600)
                continue
            started = time.monotonic()
            try:
                result = self.runners[job](spec)
            except Exception as e:              # noqa: BLE001 — job errors are
                result = {"error": str(e)[:300]}  # data, never crashes
                log.exception("scheduled job %r failed", job)
            took = time.monotonic() - started
            if took > SLOW_JOB_S:
                # jobs run inline: a slow one holds up every other schedule
                log.warning("scheduled job %r took %.0f s; the other "
                            "schedules waited for it", job, took)
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
                except Exception as e:          # noqa: BLE001
                    swallowed(log, "scheduler tick failed", e, trace=True)
        threading.Thread(target=loop, daemon=True, name="scheduler").start()
