"""The one way the dashboards process talks to claire-api.

Eight copies of "POST to claire-api, then turn whatever came back into an
answer for the browser" had grown, each with its own timeout, its own idea
of what an error body looks like, and — in the watcher's — a habit of
swallowing the failure (REVIEW-2026-11-11 §3.1, B1). One function now:
it returns the JSON body on 200 and raises HTTPError otherwise, which the
request handler already knows how to send.
"""
import httpx

from .common import HTTPError


def _detail(body, fallback):
    """FastAPI's `detail`: a string, or pydantic's list of problems (422)."""
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, list):
        detail = "; ".join(str(d.get("msg")) for d in detail
                           if isinstance(d, dict)) or "invalid"
    return str(detail or (body.get("error") if isinstance(body, dict)
                          else None) or fallback)


def post(ctx, path, body=None, *, timeout=30, what="request"):
    """POST `body` to claire-api at `path` with the internal secret.
    → the JSON answer. Raises HTTPError(502) when claire-api cannot be
    reached, HTTPError(<its status>, <its reason>) when it refuses."""
    try:
        r = httpx.post(f"{ctx.api_base}{path}", json=body,
                       headers={"x-claire-secret": ctx.secret},
                       timeout=timeout)
    except Exception as e:                      # noqa: BLE001
        raise HTTPError(502, f"claire-api unreachable ({e})") from e
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.status_code != 200:
        raise HTTPError(r.status_code,
                        _detail(data, f"{what} failed (HTTP {r.status_code})"))
    return data if isinstance(data, dict) else {"result": data}
