"""Server-side efficiency (REVIEW-2026-11-11 §3.5, §4.2): compressed
answers, pages the browser can revalidate, and slow scheduled jobs that no
longer hold up the money-path ones."""
import gzip
import json
import threading
import time

import httpx

from app.scheduler import Scheduler
from tests.test_dashboards import paired, web  # noqa: F401 — fixture
from tests.test_watcher_trailing import T0, _desk
from decimal import Decimal


# ── gzip and ETag ───────────────────────────────────────────────────────────
def test_large_answers_are_gzipped_when_the_client_accepts_it(web):  # noqa: F811
    base, _ = web
    raw = httpx.get(base + "/nav.js", headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in raw.headers
    with httpx.Client(headers={"Accept-Encoding": "gzip"}) as c:
        with c.stream("GET", base + "/nav.js") as r:
            packed = b"".join(r.iter_raw())
            assert r.headers["content-encoding"] == "gzip"
            assert r.headers["vary"] == "Accept-Encoding"
            assert int(r.headers["content-length"]) == len(packed)
    assert gzip.decompress(packed) == raw.content       # same bytes
    assert len(packed) < len(raw.content) / 2


def test_small_answers_are_left_alone(web):                  # noqa: F811
    base, _ = web
    r = httpx.get(base + "/api/nav-state")
    assert "content-encoding" not in r.headers and r.json()["pending"] == 1


def test_api_json_is_gzipped_too_and_still_parses(web):      # noqa: F811
    base, _ = web
    with paired(base) as c:
        r = c.get("/api/agent-graph")                   # a large payload
    assert r.headers.get("content-encoding") == "gzip" and "agents" in r.json()


def test_pages_and_scripts_revalidate_with_an_etag(web):     # noqa: F811
    base, _ = web
    for path in ("/", "/nav.js", "/style.css"):
        first = httpx.get(base + path)
        etag = first.headers["etag"]
        assert first.headers["cache-control"] == "no-cache"
        again = httpx.get(base + path, headers={"If-None-Match": etag})
        assert again.status_code == 304 and again.content == b""
        changed = httpx.get(base + path, headers={"If-None-Match": '"old"'})
        assert changed.status_code == 200 and changed.content == first.content


# ── scheduler ───────────────────────────────────────────────────────────────
def _sched(conn, runners, **kw):
    conn.execute("DELETE FROM schedules")
    for job in runners:
        conn.execute(
            "INSERT INTO schedules (job, description, spec, enabled, runs_in)"
            " VALUES (?,?,?,1,'claire-api')",
            (job, job, '{"type": "interval", "minutes": 1}'))
    return Scheduler(conn, runners, clock=lambda: T0, **kw)


def _result(conn, job):
    return json.loads(conn.execute(
        "SELECT last_result FROM schedules WHERE job=?", (job,)).fetchone()[0])


def test_a_slow_job_no_longer_holds_up_the_custodian():
    conn, _, _ = _desk(Decimal("100"))
    release, order = threading.Event(), []

    def screen(spec):
        release.wait(5)                     # a model call that takes a while
        order.append("analysis_us")
        return {"started": 1}

    def custodian(spec):
        order.append("custodian")
        return {"fills": 0}
    s = _sched(conn, {"analysis_us": screen, "custodian": custodian})
    t0 = time.monotonic()
    fired = s.tick()
    assert time.monotonic() - t0 < 2        # the tick did not wait for it
    assert set(fired) == {"analysis_us", "custodian"}
    assert order == ["custodian"]           # ran while the slow job was busy
    assert _result(conn, "analysis_us")["running"] is True
    release.set()
    s.wait_idle()
    assert order == ["custodian", "analysis_us"]
    assert _result(conn, "analysis_us") == {"started": 1}
    assert _result(conn, "custodian") == {"fills": 0}


def test_a_background_job_is_not_started_twice():
    conn, _, _ = _desk(Decimal("100"))
    release, runs = threading.Event(), []

    def scan(spec):
        runs.append(1)
        release.wait(5)
        return {}
    now = {"t": T0}
    s = _sched(conn, {"event_scan": scan})
    s.clock = lambda: now["t"]
    assert s.tick() == ["event_scan"]
    now["t"] += 600                         # due again, still running
    assert s.tick() == []
    release.set()
    s.wait_idle()
    assert len(runs) == 1
    now["t"] += 600
    assert s.tick() == ["event_scan"]       # free again
    s.wait_idle()


def test_a_failing_background_job_is_recorded_and_the_worker_survives():
    conn, _, _ = _desk(Decimal("100"))

    def boom(spec):
        raise RuntimeError("model unavailable")
    s = _sched(conn, {"maintenance": boom, "analysis_eu": lambda spec: {"ok": 1}})
    s.tick()
    s.wait_idle()
    assert "model unavailable" in _result(conn, "maintenance")["error"]
    assert _result(conn, "analysis_eu") == {"ok": 1}


def test_money_path_jobs_stay_inline_and_in_order():
    conn, _, _ = _desk(Decimal("100"))
    seen = []
    s = _sched(conn, {
        "custodian": lambda spec: seen.append(threading.current_thread().name) or {},
        "price_triggers": lambda spec: seen.append(threading.current_thread().name) or {},
        "reconcile": lambda spec: seen.append(threading.current_thread().name) or {}})
    s.tick()
    here = threading.current_thread().name
    assert seen == [here, here, here]       # this thread, one after another
    assert not any(s._is_background(j)
                   for j in ("custodian", "price_triggers", "reconcile"))
    assert all(s._is_background(j) for j in
               ("analysis_asia", "analysis_us", "event_scan", "maintenance"))
