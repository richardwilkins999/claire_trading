"""Descriptor-leak regressions (2026-11-06 outage): the dashboards process hit
its 1024-file limit after ~4 hours because every request thread left its
desk.db connection open, and every TradingView call left a TLS socket open."""
import os
import threading
import time

import httpx

from app.accounting import db
from app.accounting.repo import Repo
from app.dashboards import create_server
from app.tools import tradingview

from .test_dashboards import DKEY, FakeMarket


DEFERRED_CLOSE_SLACK = 4


def _db_fds(path):
    """Descriptors this process holds on desk.db / desk.db-wal."""
    n = 0
    for fd in os.listdir("/proc/self/fd"):
        try:
            target = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            continue
        if target.startswith(str(path)):
            n += 1
    return n


def _settled(path, target, wait=2.0):
    """The count once request threads have finished: a handler thread
    releases its connection just AFTER the response is sent, so the count
    is read with a short wait rather than the instant a response arrives."""
    deadline = time.monotonic() + wait
    n = _db_fds(path)
    while n > target and time.monotonic() < deadline:
        time.sleep(0.01)
        n = _db_fds(path)
    return n


def test_release_closes_this_threads_connection_and_reopens(tmp_path):
    """SQLite parks a closed connection's descriptor for reuse while another
    connection in the process holds a lock on the file, so the count is
    bounded by peak concurrency, not exact. Unreleased, it grows by one
    connection per thread forever — that is what this guards."""
    path = tmp_path / "desk.db"
    conn = db.connect(path)
    db.init(conn)

    def work():
        conn.execute("SELECT 1").fetchone()
        conn.release()

    def round_of(n):
        for _ in range(n):
            t = threading.Thread(target=work)
            t.start()
            t.join()
        return _db_fds(path)

    settled = round_of(10)
    assert round_of(50) == settled       # 50 more threads, no growth
    conn.release()                       # releasing twice is harmless
    conn.release()
    assert conn.execute("SELECT 1").fetchone()[0] == 1   # reopens on demand


def test_dashboard_requests_do_not_leak_db_connections(tmp_path):
    path = tmp_path / "desk.db"
    conn = db.connect(path)
    db.init(conn)
    srv = create_server(conn, Repo(conn), FakeMarket(), secret="s",
                        dash_key=DKEY, api_base="http://127.0.0.1:9",
                        port=0, sse_interval=None)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        base_url = f"http://127.0.0.1:{srv.server_address[1]}"
        with httpx.Client(base_url=base_url, timeout=5,
                          headers={"X-Dash-Key": DKEY}) as c:
            c.get("/api/overview")        # warm up: lazy opens happen once
            before = _settled(path, 0, wait=0.2)
            for _ in range(40):
                c.get("/api/overview")
        after = _settled(path, before)
    finally:
        srv.shutdown()
    # A leak is one connection (2-3 descriptors) per request: 80+ here. A
    # handful is SQLite itself: closing a connection while another one in
    # the process holds a lock on the same file POSTPONES closing that
    # descriptor until the lock is gone, so how many are parked at any
    # instant depends on how the request threads overlapped. Asserting
    # "not one more" made this test fail on scheduling alone (2026-11-11);
    # the live dashboards process sat at the same count across 260 requests.
    assert after - before <= DEFERRED_CLOSE_SLACK, \
        f"{after - before} desk.db descriptors leaked over 40 requests"


def test_tradingview_reuses_one_client():
    assert tradingview._shared_client() is tradingview._shared_client()
