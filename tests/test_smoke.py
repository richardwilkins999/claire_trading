"""Live smoke (DESIGN.md §18 layer 6) — auto-skips when services are down.
Start them with systemd (or by hand) and re-run to exercise for real."""
import httpx
import pytest


def _up(url):
    try:
        return httpx.get(url, timeout=3).status_code < 500
    except Exception:                              # noqa: BLE001
        return False


needs_api = pytest.mark.skipif(not _up("http://127.0.0.1:7788/status"),
                               reason="claire-api not running")
needs_dash = pytest.mark.skipif(not _up("http://127.0.0.1:7787/api/overview"),
                                reason="dashboards not running")


@needs_api
def test_api_status():
    body = httpx.get("http://127.0.0.1:7788/status", timeout=5).json()
    assert body["ok"] is True


@needs_api
def test_ask_streams_ndjson():
    with httpx.stream("POST", "http://127.0.0.1:7788/ask",
                      json={"text": "status?"}, timeout=60) as r:
        lines = [line for line in r.iter_lines() if line]
    assert lines, "no NDJSON lines from /ask"
    import json
    kinds = {json.loads(line)["kind"] for line in lines}
    assert "done" in kinds


@needs_dash
def test_every_page_serves():
    for page in ("/", "/portfolio", "/markets", "/trading", "/agents",
                 "/providers"):
        r = httpx.get(f"http://127.0.0.1:7787{page}", timeout=5)
        assert r.status_code == 200, page


@needs_dash
def test_live_quote_plausible():
    r = httpx.get("http://127.0.0.1:7787/api/quote?symbols=NVDA",
                  timeout=30).json()
    if "error" in r and "429" in r["error"]:
        pytest.skip("Yahoo rate-limited right now")
    assert float(r["NVDA"]["price"]) > 1
