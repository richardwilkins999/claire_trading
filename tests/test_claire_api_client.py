"""The dashboards' one client for claire-api (REVIEW-2026-11-11 §3.1)."""
from types import SimpleNamespace

import httpx
import pytest

from app.dash import claire_api
from app.dash.common import HTTPError

CTX = SimpleNamespace(api_base="http://api", secret="s3cret")


def _answer(monkeypatch, status, body=None, text=None):
    seen = {}

    def post(url, json=None, headers=None, timeout=None):
        seen.update(url=url, json=json, headers=headers, timeout=timeout)
        req = httpx.Request("POST", url)
        if text is not None:
            return httpx.Response(status, request=req, text=text)
        return httpx.Response(status, request=req, json=body)
    monkeypatch.setattr(httpx, "post", post)
    return seen


def test_success_returns_the_body_and_sends_the_secret(monkeypatch):
    seen = _answer(monkeypatch, 200, {"work_item_id": "wi_1"})
    out = claire_api.post(CTX, "/internal/manual", {"ticker": "X"},
                          timeout=12)
    assert out == {"work_item_id": "wi_1"}
    assert seen == {"url": "http://api/internal/manual",
                    "json": {"ticker": "X"},
                    "headers": {"x-claire-secret": "s3cret"}, "timeout": 12}


def test_unreachable_is_a_502_that_says_so(monkeypatch):
    def down(*a, **k):
        raise httpx.ConnectError("connection refused")
    monkeypatch.setattr(httpx, "post", down)
    with pytest.raises(HTTPError) as e:
        claire_api.post(CTX, "/api/run", {})
    assert e.value.code == 502
    assert "claire-api unreachable" in e.value.message


@pytest.mark.parametrize("status,body,expected", [
    (409, {"detail": "already in flight"}, "already in flight"),
    (422, {"detail": [{"msg": "entry must be positive"},
                      {"msg": "stop must be below entry"}]},
     "entry must be positive; stop must be below entry"),
    (400, {"error": "bad symbol"}, "bad symbol"),
    (500, {}, "override failed (HTTP 500)"),
])
def test_a_refusal_keeps_its_status_and_its_reason(monkeypatch, status, body,
                                                   expected):
    _answer(monkeypatch, status, body)
    with pytest.raises(HTTPError) as e:
        claire_api.post(CTX, "/internal/override", {}, what="override")
    assert (e.value.code, e.value.message) == (status, expected)


def test_a_non_json_error_page_still_gives_a_reason(monkeypatch):
    _answer(monkeypatch, 503, text="<html>Service Unavailable</html>")
    with pytest.raises(HTTPError) as e:
        claire_api.post(CTX, "/internal/order", {}, what="order change")
    assert e.value.code == 503
    assert e.value.message == "order change failed (HTTP 503)"


def test_no_view_or_action_posts_to_claire_api_by_hand():
    from pathlib import Path
    dash = Path(claire_api.__file__).parent
    for name in ("handler.py", "actions.py", "views_runs.py",
                 "views_orders.py", "views_system.py"):
        src = (dash / name).read_text()
        assert "httpx.post(" not in src, name
