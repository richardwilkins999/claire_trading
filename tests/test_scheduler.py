"""Scheduler tests — spec validation, next-run math across timezones and
weekends, due-job firing, and the screener's open-market guard."""
import json

import pytest

from app import scheduler
from app.accounting import db

T0 = 1_786_456_800   # Tue 2026-08-11 14:00 UTC


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init(c)
    scheduler.seed(c)
    return c


def test_seed_is_idempotent(conn):
    scheduler.seed(conn)
    (n,) = conn.execute("SELECT COUNT(*) FROM schedules").fetchone()
    assert n == len(scheduler.DEFAULTS)


def test_interval_next_run():
    spec = {"type": "interval", "minutes": 5}
    assert scheduler.next_run_ts(spec, None, T0) == T0          # fire now
    assert scheduler.next_run_ts(spec, T0, T0 + 10) == T0 + 300


def test_daily_next_run_respects_tz_and_weekend():
    spec = {"type": "daily", "time": "10:00", "tz": "Asia/Singapore",
            "days": [1, 2, 3, 4, 5]}
    # last ran Tue 10:00 SGT (02:00 UTC); next is Wed 10:00 SGT
    tue_10_sgt = T0 - 12 * 3600
    nxt = scheduler.next_run_ts(spec, tue_10_sgt, T0)
    assert nxt == tue_10_sgt + 86400
    # Fri run → next is MONDAY (weekend skipped)
    fri_10_sgt = tue_10_sgt + 3 * 86400
    assert scheduler.next_run_ts(spec, fri_10_sgt, fri_10_sgt + 60) == \
        fri_10_sgt + 3 * 86400


def test_daily_never_backfires_on_boot():
    spec = {"type": "daily", "time": "03:00", "tz": "UTC",
            "days": [1, 2, 3, 4, 5, 6, 7]}
    nxt = scheduler.next_run_ts(spec, None, T0)     # 14:00 UTC, 03:00 passed
    assert nxt > T0                                  # tomorrow, not immediately


def test_validate_spec():
    scheduler.validate_spec({"type": "interval", "minutes": 5})
    with pytest.raises(ValueError):
        scheduler.validate_spec({"type": "interval", "minutes": 0})
    with pytest.raises(ValueError):
        scheduler.validate_spec({"type": "daily", "time": "25:00",
                                 "tz": "UTC", "days": [1]})
    with pytest.raises(Exception):
        scheduler.validate_spec({"type": "daily", "time": "10:00",
                                 "tz": "Mars/Olympus", "days": [1]})


def test_update_merges_and_validates(conn):
    scheduler.update(conn, "custodian", spec_patch={"minutes": 15})
    row = conn.execute("SELECT spec FROM schedules WHERE job='custodian'"
                       ).fetchone()
    assert json.loads(row["spec"])["minutes"] == 15
    with pytest.raises(ValueError):
        scheduler.update(conn, "custodian", spec_patch={"minutes": 9999})
    with pytest.raises(KeyError):
        scheduler.update(conn, "nope", enabled=False)


def test_tick_fires_due_jobs_once_and_records_result(conn):
    fired = []
    sched = scheduler.Scheduler(
        conn, {"custodian": lambda spec: (fired.append("c") or
                                          {"fills": 2, "flags": []})},
        clock=lambda: T0)
    assert sched.tick() == ["custodian"]            # interval job: due at boot
    assert sched.tick() == []                       # not due again for 5 min
    row = conn.execute("SELECT * FROM schedules WHERE job='custodian'"
                       ).fetchone()
    assert row["last_run_at"] == T0
    assert json.loads(row["last_result"])["fills"] == 2
    # disabled jobs never fire
    scheduler.update(conn, "custodian", enabled=False)
    sched2 = scheduler.Scheduler(
        conn, {"custodian": lambda spec: fired.append("x")},
        clock=lambda: T0 + 9999)
    assert sched2.tick() == []


def test_tick_captures_job_errors(conn):
    def boom(spec):
        raise RuntimeError("venue exploded")
    sched = scheduler.Scheduler(conn, {"custodian": boom}, clock=lambda: T0)
    assert sched.tick() == ["custodian"]
    row = conn.execute("SELECT last_result FROM schedules WHERE"
                       " job='custodian'").fetchone()
    assert "venue exploded" in row["last_result"]


def test_analysis_requires_open_markets_at_fire_time():
    """The wiring in api/server.run_analysis filters exchanges via
    sessions.is_open; verify the guard itself: at Tue 14:00 UTC only the US
    exchanges in scope are open."""
    from datetime import datetime, timezone

    from app import sessions
    from app.autonomous import REGIONS
    now = datetime.fromtimestamp(T0, tz=timezone.utc)
    assert [ex for ex in REGIONS["asia"]
            if sessions.is_open(ex, now)] == []          # Asia asleep
    assert [ex for ex in REGIONS["us"]
            if sessions.is_open(ex, now)] == ["NASDAQ", "NYSE"]
