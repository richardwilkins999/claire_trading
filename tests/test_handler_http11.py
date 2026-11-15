"""The dashboards speak HTTP/1.1 and keep the connection between requests
(REVIEW-2026-11-11 §4.2). Real HTTP against the stdlib server, offline.

What has to hold for that to be safe: every answer that is not a stream
says how long it is; a stream, and any answer to a request whose body was
not read, ends the connection; and an idle connection gives its thread
back quietly."""
import http.client
import json
import logging
import socket
import socketserver
import threading
import time

import pytest

from app.accounting import db
from app.accounting.repo import Repo
from app.dashboards import create_server

DKEY = "dash-key-123"
JSON = {"Content-Type": "application/json"}


class Market:
    def spark(self, syms):
        return {}

    def quote(self, s):
        return {}


class CountingConn:
    """The desk.db handle, counting how often the server gives it back."""

    def __init__(self, real):
        self._real, self.released = real, 0

    def release(self):
        self.released += 1

    def __getattr__(self, name):
        return getattr(self._real, name)


@pytest.fixture
def server():
    started = []

    def start(**kw):
        raw = db.connect(":memory:")
        db.init(raw)
        conn = CountingConn(raw)
        srv = create_server(conn, Repo(raw), Market(), secret="s",
                            dash_key=DKEY, api_base="http://127.0.0.1:9",
                            port=0, sse_interval=None, **kw)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        started.append(srv)
        return srv, conn
    yield start
    for srv in started:
        srv.shutdown()
        srv.server_close()


def client(srv):
    return http.client.HTTPConnection("127.0.0.1", srv.server_address[1],
                                      timeout=5)


def ask(c, method, path, body=None, headers=None):
    """One exchange on connection `c`: (response, body, the socket used)."""
    c.request(method, path, body=body, headers=headers or {})
    sock = c.sock                   # http.client drops it once told "close"
    r = c.getresponse()
    return r, r.read(), sock


def kept(r):
    return r.version == 11 and \
        (r.getheader("Connection") or "").lower() != "close"


def raw_exchange(srv, request: bytes, wait=5.0):
    """Everything the server sends until IT closes the connection."""
    with socket.create_connection(("127.0.0.1", srv.server_address[1]),
                                  timeout=wait) as s:
        s.sendall(request)
        data = b""
        while chunk := s.recv(65536):
            data += chunk
    return data


def read_one(s):
    """Exactly one answer from raw socket `s`, by its Content-Length."""
    data = b""
    while b"\r\n\r\n" not in data:
        data += s.recv(65536)
    head, _, body = data.partition(b"\r\n\r\n")
    length = int(head.lower().split(b"content-length:")[1].split()[0])
    while len(body) < length:
        body += s.recv(65536)
    return head, body


def until(what, wait=3.0):
    """The server finishes a request just AFTER the answer has gone."""
    deadline = time.monotonic() + wait
    while not what() and time.monotonic() < deadline:
        time.sleep(0.005)
    return what()


# ── one connection, many requests ─────────────────────────────────────────
def test_two_requests_use_one_connection(server):
    srv, _ = server()
    c = client(srv)
    r1, body1, sock1 = ask(c, "GET", "/api/watchlist")
    assert r1.status == 200 and kept(r1) and json.loads(body1) == []
    assert int(r1.getheader("Content-Length")) == len(body1)
    port1 = sock1.getsockname()[1]
    r2, body2, sock2 = ask(c, "GET", "/api/overview")
    assert r2.status == 200 and kept(r2) and "flags" in json.loads(body2)
    # the same socket: http.client would have opened a new one (another
    # local port) had the server closed the first
    assert sock2 is sock1 and c.sock is sock1
    assert sock2.getsockname()[1] == port1


def test_every_kind_of_answer_keeps_the_connection(server):
    """A page, a script, compressed JSON, a paired POST with a body and a
    DELETE, one after another on one socket — each has to say exactly how
    long it is, or the next answer would be read from the wrong place."""
    srv, _ = server()
    c = client(srv)
    first = None
    for method, path, headers, body in (
            ("GET", "/portfolio", {}, None),
            ("GET", "/nav.js", {}, None),
            ("GET", "/api/agent-graph", {"Accept-Encoding": "gzip"}, None),
            ("POST", "/api/watchlist", {**JSON, "X-Dash-Key": DKEY},
             b'{"ticker": "NVDA", "exchange": "NASDAQ"}'),
            ("GET", "/api/watchlist", {}, None),
            ("DELETE", "/api/price-triggers?id=1", {"X-Dash-Key": DKEY},
             None),
            ("GET", "/api/live-status", {}, None)):
        r, data, sock = ask(c, method, path, body, headers)
        first = first or sock
        assert sock is first, f"{method} {path} came on a new connection"
        assert kept(r), f"{method} {path} closed the connection"
        assert int(r.getheader("Content-Length")) == len(data), path
        if path == "/api/agent-graph":
            assert r.getheader("Content-Encoding") == "gzip"
        if (method, path) == ("GET", "/api/watchlist"):
            assert [w["ticker"] for w in json.loads(data)] == ["NVDA"]
        if method == "DELETE":
            assert r.status == 404          # no such trigger: still kept
        else:
            assert r.status == 200, path


def test_a_304_keeps_the_connection_and_has_no_body(server):
    srv, _ = server()
    c = client(srv)
    r1, page, sock = ask(c, "GET", "/nav.js")
    etag = r1.getheader("ETag")
    assert r1.status == 200 and etag and page
    r2, nothing, sock2 = ask(c, "GET", "/nav.js", headers={
        "If-None-Match": etag})
    assert r2.status == 304 and nothing == b"" and kept(r2)
    assert r2.getheader("ETag") == etag
    # a 304 never has a body, so it must not be given a length of its own
    assert r2.getheader("Content-Length") is None
    # and the answer after it is read from the right place
    r3, again, sock3 = ask(c, "GET", "/nav.js")
    assert r3.status == 200 and again == page
    assert sock is sock2 is sock3


def test_a_redirect_keeps_the_connection(server, monkeypatch):
    """The Saxo sign-in comes back to the desk and is sent on to the Saxo
    page: a 303 with nothing in it, which has to say so (length 0)."""
    from app.tools import saxo_live
    monkeypatch.setattr(saxo_live, "complete_login", lambda *a, **k: "sim")
    srv, _ = server()
    c = client(srv)
    r, data, sock = ask(c, "GET", "/claire-saxo?state=s&code=c")
    assert r.status == 303 and data == b"" and kept(r)
    assert r.getheader("Location") == "/saxo?env=sim&signin=ok"
    assert r.getheader("Content-Length") == "0"
    r2, _, sock2 = ask(c, "GET", "/api/watchlist")
    assert r2.status == 200 and sock2 is sock


def test_an_error_answer_keeps_the_connection(server, caplog):
    srv, _ = server()
    c = client(srv)
    paired = {**JSON, "X-Dash-Key": DKEY}
    first = None
    with caplog.at_level(logging.CRITICAL):         # the 500 logs a traceback
        for method, path, headers, body, want in (
                ("GET", "/api/nope", {}, None, 404),
                ("GET", "/api/feed?since=abc", {}, None, 400),
                ("GET", "/api/chat-history", {}, None, 403),
                ("GET", "/api/trades?days=abc", {}, None, 500),
                # the body was read, so these are safe to keep too
                ("POST", "/api/watchlist", paired, b"{not json", 400),
                ("POST", "/api/nope", paired, b"{}", 404),
                ("GET", "/api/watchlist", {}, None, 200)):
            r, data, sock = ask(c, method, path, body, headers)
            first = first or sock
            assert r.status == want, path
            assert sock is first and kept(r), f"{path} lost the connection"
            assert int(r.getheader("Content-Length")) == len(data)
            if want != 200:
                assert "error" in json.loads(data)


# ── what cannot be kept is closed, and says so ────────────────────────────
def test_a_refused_post_never_leaves_its_body_to_be_read_as_a_request(
        server):
    """The key and Content-Type are checked BEFORE the body is read. On a
    kept connection the unread body would be the next thing the server
    parses — so a body shaped like a request would be answered as one."""
    srv, conn = server()
    host = f"127.0.0.1:{srv.server_address[1]}"
    smuggled = (f"POST /api/watchlist HTTP/1.1\r\nHost: {host}\r\n"
                f"X-Dash-Key: {DKEY}\r\nContent-Type: application/json\r\n"
                "Content-Length: 40\r\n\r\n"
                '{"ticker": "EVIL", "exchange": "NASDAQ"}').encode()
    for head, want in (
            ("Content-Type: application/json\r\n", b" 403 "),   # no key
            (f"X-Dash-Key: {DKEY}\r\nContent-Type: text/plain\r\n",
             b" 415 "),
            (f"X-Dash-Key: {DKEY}\r\nContent-Type: application/json\r\n"
             "Origin: http://evil.example\r\n", b" 403 ")):
        out = raw_exchange(srv, (
            f"POST /api/watchlist HTTP/1.1\r\nHost: {host}\r\n{head}"
            f"Content-Length: {len(smuggled)}\r\n\r\n").encode() + smuggled)
        assert want in out.split(b"\r\n")[0]
        assert out.count(b"HTTP/1.1 ") == 1, "the body was answered too"
        assert b"\r\nConnection: close\r\n" in out
    assert conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] == 0


def test_a_get_with_a_body_and_an_old_client_are_closed(server):
    srv, _ = server()
    host = f"127.0.0.1:{srv.server_address[1]}"
    # nothing reads a GET's body: close rather than parse it next
    out = raw_exchange(srv, (
        f"GET /api/watchlist HTTP/1.1\r\nHost: {host}\r\n"
        "Content-Length: 5\r\n\r\nhello").encode())
    assert out.startswith(b"HTTP/1.1 200 ") and out.count(b"HTTP/1.1 ") == 1
    assert b"\r\nConnection: close\r\n" in out
    # an HTTP/1.0 client expects the connection to end with the answer
    out = raw_exchange(srv, (
        f"GET /api/watchlist HTTP/1.0\r\nHost: {host}\r\n\r\n").encode())
    assert out.startswith(b"HTTP/1.1 200 ") and out.endswith(b"\r\n\r\n[]")
    assert b"\r\nConnection: close\r\n" in out
    # and a browser that asks to close is told it was heard
    c = client(srv)
    r, _, _ = ask(c, "GET", "/api/watchlist", headers={"Connection": "close"})
    assert r.status == 200 and r.getheader("Connection") == "close"


def test_a_client_that_waits_for_100_continue_is_kept(server):
    """curl sends `Expect: 100-continue` with a large body and waits to be
    told to go on. That interim line is not the answer: it must not carry
    `Connection: close`, and the real answer follows on the same socket."""
    srv, conn = server()
    host = f"127.0.0.1:{srv.server_address[1]}"
    body = b'{"ticker": "NVDA", "exchange": "NASDAQ"}'
    with socket.create_connection(("127.0.0.1", srv.server_address[1]),
                                  timeout=5) as s:
        s.sendall((f"POST /api/watchlist HTTP/1.1\r\nHost: {host}\r\n"
                   f"X-Dash-Key: {DKEY}\r\nContent-Type: application/json\r\n"
                   f"Content-Length: {len(body)}\r\n"
                   "Expect: 100-continue\r\n\r\n").encode())
        assert s.recv(65536) == b"HTTP/1.1 100 Continue\r\n\r\n"
        s.sendall(body)
        head, answer = read_one(s)
        assert head.startswith(b"HTTP/1.1 200 ") and answer == b'{"ok": true}'
        assert b"connection: close" not in head.lower()
        s.sendall(f"GET /api/watchlist HTTP/1.1\r\nHost: {host}\r\n\r\n"
                  .encode())
        head, answer = read_one(s)
        assert [w["ticker"] for w in json.loads(answer)] == ["NVDA"]


# ── the streams ───────────────────────────────────────────────────────────
def test_the_live_event_stream_still_delivers_and_ends(server):
    srv, _ = server(sse_max_age=1.5)
    ctx = srv.RequestHandlerClass.ctx
    c = client(srv)
    c.request("GET", "/api/events")
    r = c.getresponse()
    assert r.status == 200
    assert r.getheader("Content-Type") == "text/event-stream"
    # no length — it ends when the connection does, and says so
    assert r.getheader("Content-Length") is None
    assert r.getheader("Connection") == "close"
    assert r.readline() == b"retry: 3000\n"         # the first bytes, at once
    assert r.readline() == b": connected\n" and r.readline() == b"\n"
    # the stream holds its thread; the server still answers everyone else
    other = client(srv)
    assert ask(other, "GET", "/api/watchlist")[0].status == 200
    with ctx.sse_lock:
        (queue,) = ctx.sse_clients
    queue.put({"kind": "agents", "n": 1})
    assert json.loads(r.readline().removeprefix(b"data: ")) == {
        "kind": "agents", "n": 1}
    assert r.readline() == b"\n"
    # at sse_max_age the server ends it (EventSource then reconnects)
    t = time.monotonic()
    assert r.read() == b""
    assert time.monotonic() - t < 5
    with ctx.sse_lock:
        assert ctx.sse_clients == []


def test_the_chat_stream_is_not_given_a_length_and_ends_the_connection(
        server):
    """/api/ask and /api/agent-ask pass claire-api's newline-delimited JSON
    straight through. claire-api is not running here, so the stream is the
    error line and the closing line — which is all the page needs to see
    where it ends."""
    srv, _ = server()
    for path in ("/api/ask", "/api/agent-ask"):
        c = client(srv)
        r, data, sock = ask(c, "POST", path, b'{"message": "hi"}',
                            {**JSON, "X-Dash-Key": DKEY})
        assert r.status == 200
        assert r.getheader("Content-Type") == "application/x-ndjson"
        assert r.getheader("Content-Length") is None
        assert r.getheader("Connection") == "close"
        lines = [json.loads(x) for x in data.splitlines()]
        assert [x["kind"] for x in lines] == ["error", "done"]
        # the same client object carries on, on a new connection
        r2, _, sock2 = ask(c, "GET", "/api/watchlist")
        assert r2.status == 200 and sock2 is not sock


# ── an idle connection ────────────────────────────────────────────────────
def test_an_idle_connection_is_closed_quietly(server, caplog, capfd):
    srv, _ = server()
    srv.RequestHandlerClass.timeout = 0.3           # this server's handler
    host = f"127.0.0.1:{srv.server_address[1]}"
    with caplog.at_level(logging.DEBUG):
        with socket.create_connection(("127.0.0.1", srv.server_address[1]),
                                      timeout=5) as s:
            s.sendall(f"GET /api/watchlist HTTP/1.1\r\nHost: {host}\r\n\r\n"
                      .encode())
            head, body = read_one(s)
            assert head.startswith(b"HTTP/1.1 200 ") and body == b"[]"
            # kept open for the next request … which never comes
            t = time.monotonic()
            assert s.recv(65536) == b""             # the server hung up
            assert 0.2 < time.monotonic() - t < 4
        # a client that stalls half-way through its headers goes the same way
        with socket.create_connection(("127.0.0.1", srv.server_address[1]),
                                      timeout=5) as s:
            s.sendall(f"GET /api/watchlist HTTP/1.1\r\nHost: {host}\r\n"
                      .encode())
            assert s.recv(65536) == b""
    time.sleep(0.05)                    # let the handler threads finish
    assert caplog.records == []
    assert "Traceback" not in capfd.readouterr().err


def test_the_idle_timeout_is_a_sensible_length():
    from app.dash.handler import Handler
    assert Handler.protocol_version == "HTTP/1.1"
    # long enough to span the pages' polls, short enough to free threads
    assert 15 <= Handler.timeout <= 30


# ── the server around it ──────────────────────────────────────────────────
def test_the_server_is_still_threaded_with_daemon_threads(server):
    srv, _ = server()
    assert isinstance(srv, socketserver.ThreadingMixIn)
    assert srv.daemon_threads is True
    # two connections held open at once are both served
    a, b = client(srv), client(srv)
    assert ask(a, "GET", "/api/watchlist")[0].status == 200
    assert ask(b, "GET", "/api/watchlist")[0].status == 200
    assert ask(a, "GET", "/api/watchlist")[0].status == 200


def test_writes_are_not_held_back_on_a_kept_connection(server):
    """Headers and body are two writes. With Nagle's algorithm on, the
    second waits for the browser to acknowledge the first — 40 ms on every
    request after the first, which made a kept connection SLOWER than a
    new one. The handler turns it off on each socket it accepts."""
    srv, _ = server()
    seen = []

    class Spy(srv.RequestHandlerClass):
        def setup(self):
            super().setup()
            seen.append(self.connection.getsockopt(socket.IPPROTO_TCP,
                                                   socket.TCP_NODELAY))
    srv.RequestHandlerClass = Spy
    assert ask(client(srv), "GET", "/api/watchlist")[0].status == 200
    assert seen and all(seen)


def test_the_database_handle_goes_back_after_every_request(server):
    """The thread now outlives the request. Its desk.db handle must not:
    a transaction left open by one request would be carried into the next
    and hold the write lock for as long as the page keeps polling."""
    srv, conn = server()
    c = client(srv)
    r, _, sock = ask(c, "GET", "/api/watchlist")
    assert r.status == 200 and until(lambda: conn.released == 1)
    r, _, sock2 = ask(c, "GET", "/api/watchlist")
    # the same connection, so the same thread: given back a second time
    assert sock2 is sock and until(lambda: conn.released == 2)
