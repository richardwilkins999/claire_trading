"""Logging (REVIEW-2026-11-11 §3.5): the desk used to have none. These pin
the setup, the throttle, and that the failures which used to vanish now
leave a line.
"""
import io
import logging
import re
import sys
import threading
import time
from decimal import Decimal

import pytest

from app import logsetup
from app.accounting.money import to_micro
from app.custodian import Custodian
from app.scheduler import Scheduler
from app.tools.market import Market, MarketError
from tests.test_watcher_trailing import T0, _desk


# ── setup ───────────────────────────────────────────────────────────────────
@pytest.fixture
def fresh_setup(monkeypatch):
    """configure() as if in a new process, with everything it touches put
    back afterwards."""
    root = logging.getLogger()
    monkeypatch.setattr(logsetup, "_configured", None)
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    before, level = list(root.handlers), root.level
    quiet = {n: logging.getLogger(n).level for n in logsetup.QUIET}
    yield
    for h in list(root.handlers):
        if h not in before:
            root.removeHandler(h)
    root.setLevel(level)
    for n, lv in quiet.items():
        logging.getLogger(n).setLevel(lv)
    logging.captureWarnings(False)


def test_every_line_has_a_timestamp_level_and_logger(fresh_setup):
    out = io.StringIO()
    logsetup.configure("claire-test", stream=out)
    logging.getLogger("app.custodian").warning("order %s left open", "o1")
    lines = out.getvalue().splitlines()
    assert re.match(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d INFO +claire: "
                    r"claire-test starting \(pid \d+", lines[0])
    assert re.match(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d WARNING app\.custodian: "
                    r"order o1 left open$", lines[1])


def test_configure_twice_adds_one_handler(fresh_setup):
    out = io.StringIO()
    logsetup.configure("a", stream=out)
    logsetup.configure("a", stream=out)
    logging.getLogger("x").warning("once")
    assert out.getvalue().count("once") == 1


def test_http_libraries_cannot_log_request_urls(fresh_setup):
    """httpx logs every request URL at INFO — and the desk's URLs carry the
    Twelve Data key and the Telegram bot token."""
    out = io.StringIO()
    logsetup.configure("a", stream=out, level="DEBUG")
    logging.getLogger("httpx").info(
        "HTTP Request: GET https://api.example/quote?apikey=SECRET")
    logging.getLogger("httpx").warning("pool exhausted")
    assert "SECRET" not in out.getvalue()
    assert "pool exhausted" in out.getvalue()


def test_level_comes_from_the_environment(fresh_setup, monkeypatch):
    out = io.StringIO()
    monkeypatch.setenv("CLAIRE_LOG_LEVEL", "warning")
    logsetup.configure("a", stream=out)
    logging.getLogger("x").info("chatter")
    logging.getLogger("x").warning("matters")
    assert "chatter" not in out.getvalue() and "matters" in out.getvalue()
    monkeypatch.setenv("CLAIRE_LOG_LEVEL", "nonsense")      # falls back
    logsetup.configure("a", stream=out)
    assert logging.getLogger().level == logging.INFO


def test_a_thread_that_dies_is_logged_with_its_traceback(fresh_setup):
    out = io.StringIO()
    logsetup.configure("a", stream=out)

    def boom():
        raise RuntimeError("watcher blew up")
    t = threading.Thread(target=boom, name="watcher")
    t.start()
    t.join()
    text = out.getvalue()
    assert "CRITICAL claire: uncaught exception in thread watcher" in text
    assert "RuntimeError: watcher blew up" in text and "Traceback" in text


# ── swallowed(): visible, but not a flood ───────────────────────────────────
def test_swallowed_logs_once_then_counts_the_repeats(caplog):
    log, now = logging.getLogger("app.test"), {"t": 0.0}
    clock = lambda: now["t"]                                # noqa: E731
    assert logsetup.swallowed(log, "feed failed", ValueError("429"),
                              clock=clock) is True
    for _ in range(40):                     # a poll every few seconds
        now["t"] += 5
        assert logsetup.swallowed(log, "feed failed", ValueError("429"),
                                  clock=clock) is False
    assert len(caplog.records) == 1
    assert caplog.records[0].getMessage() == "feed failed: ValueError: 429"
    now["t"] += 300
    logsetup.swallowed(log, "feed failed", ValueError("still"), clock=clock)
    assert caplog.records[1].getMessage() == (
        "feed failed: ValueError: still (40 more like this since the last "
        "line)")
    # a different failure is never held back by this one
    assert logsetup.swallowed(log, "other thing", clock=clock) is True


def test_swallowed_trace_adds_the_traceback(caplog):
    log = logging.getLogger("app.test")
    try:
        raise KeyError("lot1")
    except KeyError as e:
        logsetup.swallowed(log, "bookkeeping failed", e, trace=True)
        logsetup.swallowed(log, "network hiccup", e)
    assert caplog.records[0].exc_info and not caplog.records[1].exc_info


# ── the failures that used to vanish ────────────────────────────────────────
def _schedules(conn, *jobs):
    conn.execute("DELETE FROM schedules")
    for job in jobs:
        conn.execute(
            "INSERT INTO schedules (job, description, spec, enabled, runs_in)"
            " VALUES (?,?,?,1,'claire-api')",
            (job, job, '{"type": "interval", "minutes": 1}'))


def test_a_failing_scheduled_job_is_logged_with_its_traceback(caplog):
    conn, _, _ = _desk(Decimal("100"))
    _schedules(conn, "reconcile")

    def reconcile(spec):
        raise RuntimeError("backup disk full")
    s = Scheduler(conn, {"reconcile": reconcile}, clock=lambda: T0)
    assert s.tick() == ["reconcile"]
    rec = [r for r in caplog.records if r.name == "app.scheduler"][0]
    assert rec.levelname == "ERROR" and rec.exc_info
    assert "scheduled job 'reconcile' failed" in rec.getMessage()
    # and it is still data on the Schedule page, as before
    assert "backup disk full" in conn.execute(
        "SELECT last_result FROM schedules").fetchone()[0]


def test_a_slow_job_says_it_held_the_others_up(caplog, monkeypatch):
    from app import scheduler
    conn, _, _ = _desk(Decimal("100"))
    _schedules(conn, "screen")
    monkeypatch.setattr(scheduler, "SLOW_JOB_S", 0.01)
    Scheduler(conn, {"screen": lambda spec: time.sleep(0.03) or {}},
              clock=lambda: T0).tick()
    assert any("took" in r.getMessage() and "waited" in r.getMessage()
               for r in caplog.records)


def test_a_custodian_duty_that_raises_leaves_a_traceback(caplog):
    conn, w, _ = _desk(Decimal("100"))
    c = Custodian(conn, w.repo, {}, resume_post=lambda b: (200, {}),
                  account_broker=lambda a: "alpaca",
                  fx_rate_for=lambda i, a: to_micro("1"), clock=lambda: T0)

    def broken(report):
        raise ZeroDivisionError("bad lot maths")
    c._archive = broken
    flags = c.run_once()["flags"]
    assert any("archive error" in f for f in flags)         # as before
    recs = [r for r in caplog.records if r.name == "app.custodian"]
    assert any(r.exc_info and "duty archive failed" in r.getMessage()
               for r in recs)
    # every flag is in the log too, not only in the health row's JSON
    assert any("custodian flag: custodian archive error" in r.getMessage()
               for r in recs)
    caplog.clear()
    c.run_once()                            # five minutes later: same flag
    assert not [r for r in caplog.records if "custodian flag" in
                r.getMessage()]             # held back, not repeated


def test_a_breach_with_no_review_is_an_error_line(caplog):
    conn, w, _ = _desk(Decimal("100"))
    w.start_sell_review = lambda inst, trigger=None: None
    w.tick()
    w.market.price = Decimal("91")
    w.tick()
    rec = [r for r in caplog.records if r.name == "app.watcher"][0]
    assert rec.levelname == "ERROR"
    assert "NASDAQ:X" in rec.getMessage() and "did not start" in \
        rec.getMessage()


def test_serving_stale_market_data_is_logged(caplog):
    state = {"fail": False}

    def get(url, params=None):
        if state["fail"]:
            raise MarketError("throttled")
        return {"NVDA": {"close": [10.0], "previousClose": 9.0,
                         "currency": "USD"}}
    t = {"now": 1000}
    m = Market(_get=get, clock=lambda: t["now"], tv_quotes=lambda s: {})
    assert m.quote("NVDA")["stale"] is False
    assert not caplog.records
    state["fail"], t["now"] = True, 2000
    assert m.quote("NVDA")["stale"] is True                 # as before
    assert any("serving the last good value" in r.getMessage()
               and "throttled" in r.getMessage() for r in caplog.records)


def test_fx_fallbacks_are_logged(caplog):
    def down(url, params=None):
        raise MarketError("throttled")
    m = Market(_get=down, fx_fallback=lambda a, b: "0.78", clock=lambda: 1000)
    assert m.fx("SGD", "USD") == Decimal("0.78")
    assert any("FX source 'fx' failed" in r.getMessage()
               for r in caplog.records)


def test_a_dashboard_500_is_logged_with_path_and_traceback(caplog):
    import httpx

    from app.dashboards import create_server
    from tests.test_dashboards import DKEY

    conn, w, _ = _desk(Decimal("100"))

    class BrokenMarket:
        def __getattr__(self, name):
            raise RuntimeError("market exploded")
    srv = create_server(conn, w.repo, BrokenMarket(), secret="s",
                        dash_key=DKEY, api_base="http://127.0.0.1:9", port=0,
                        sse_interval=None)
    threading.Thread(target=srv.serve_forever, daemon=True,
                     kwargs={"poll_interval": 0.01}).start()
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        with httpx.Client(base_url=base, timeout=5,
                          headers={"X-Dash-Key": DKEY}) as c:
            codes = {p: c.get(p).status_code for p in (
                "/api/quote?symbol=NVDA", "/api/markets?exchange=NASDAQ",
                "/api/portfolio")}
    finally:
        srv.shutdown()
    assert 500 in codes.values(), codes
    recs = [r for r in caplog.records if r.name == "app.dashboards"
            and r.getMessage().startswith("HTTP 500 on GET /api/")]
    assert recs and recs[0].exc_info
    assert "?" not in recs[0].getMessage()      # no query string in the log
