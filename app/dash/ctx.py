"""What a request needs from the server it arrived at.

The request handler used to be a class defined INSIDE create_server, reaching
these through the closure — so no view could be imported or tested without a
running HTTP server. They are fields now; create_server builds one Ctx and
hangs it on its handler class (REVIEW-2026-11-11 §3.2, step 5)."""
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class Ctx:
    conn: Any                   # desk.db (thread-local connection)
    repo: Any                   # accounting.repo.Repo
    market: Any                 # tools.market.Market
    api_base: str               # claire-api, e.g. http://127.0.0.1:7788
    secret: str                 # CLAIRE_INTERNAL_SECRET, for /internal/*
    dash_key: str               # the dashboard key every POST must carry
    narratives: Any             # tools.files.Narratives, or None
    clock: Callable[[], float]
    env: dict
    cal: Any                    # exchange sessions from desk.db, or None
    resume_post: Callable       # approvals.default_resume_post(...)
    sse_clients: list           # one queue per connected browser
    sse_lock: Any
    sse_max_age: float
    host_allowed: Callable[[str], bool]
    api_ok: Callable[[], bool]  # is claire-api answering (cached 10 s)
    cal_or_default: Callable[[], dict]
