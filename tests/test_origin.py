"""What started a run is decided once (REVIEW-2026-11-11 §3.1): the Runs
page story, the Usage attribution and the Telegram card must agree."""
import pytest

from app import origin
from app import telegram_approvals as ta
from app.dash.usage import usage_source

CASES = [
    # kind, trigger, code, usage, telegram starts with
    ("override", "human override of wi_x", origin.OVERRIDE, "override",
     "Your override of a PASS (wi_x)"),
    ("pipeline", "human override of wi_x", origin.OVERRIDE, "override",
     "Your override of a PASS"),
    ("sell_review", None, origin.EXIT_REVIEW, "exit_review",
     "Exit review (price watcher)"),
    ("pipeline", "watcher: trail 8% hit", origin.EXIT_REVIEW, "exit_review",
     "Exit review (price watcher) — trail 8% hit"),
    ("pipeline", "event scan: relative volume 5.2x", origin.VOLUME_SCAN,
     "volume_scan", "Volume scan — relative volume 5.2x"),
    ("pipeline", "screener agent: momentum", origin.DAILY_SCREEN, "daily",
     "Daily screen — momentum"),
    ("screen", None, origin.DAILY_SCREEN, "daily", "Daily screen"),
    ("pipeline", "deterministic fallback (down): top", origin.DAILY_FALLBACK,
     "daily", "Daily screen (most-active fallback)"),
    ("manual", "manual buy from trigger #7: price <= 5", origin.PRICE_TRIGGER,
     "manual", "Your price trigger — price <= 5"),
    ("manual", "manual sell from the Portfolio page", origin.MANUAL_TRADE,
     "manual", "Manual trade you started from the Portfolio page"),
    ("pipeline", "launched from the Trading page", origin.LAUNCH_FORM,
     "manual", "Manual run (the Launch form"),
    ("pipeline", "launched from the Markets page", origin.MARKETS_PAGE,
     "manual", "Manual run (Research on the Markets page)"),
    ("pipeline", "chat: Richard asked for NVDA", origin.CHAT, "manual",
     "Asked for in Claire's chat — Richard asked for NVDA"),
    ("pipeline", None, origin.UNRECORDED, "manual", "Manual run"),
    ("pipeline", "something else", origin.OTHER, "manual",
     "Other — something else"),
]


@pytest.mark.parametrize("kind,trigger,code,usage,telegram", CASES)
def test_one_decision_three_wordings(kind, trigger, code, usage, telegram):
    assert origin.classify(kind, trigger)[0] == code
    assert usage_source("news", kind, trigger) == usage
    assert ta.origin(kind, trigger).startswith(telegram)


def test_region_comes_from_the_exchange_not_the_text():
    assert usage_source("news", "pipeline", "screener agent: x", "SGX") \
        == "daily_asia"
    assert usage_source("news", "pipeline", "deterministic fallback: top",
                        "NASDAQ") == "daily_us"


def test_an_override_is_recognised_by_its_link_too():
    assert origin.classify("pipeline", None, override_of="wi_p")[0] \
        == origin.OVERRIDE


def test_no_second_classifier_is_left():
    from pathlib import Path
    app = Path(origin.__file__).parent
    for name in ("dash/usage.py", "dash/views_runs.py",
                 "telegram_approvals.py"):
        src = (app / name).read_text()
        assert 'startswith("event scan")' not in src, name
        assert 'startswith("watcher:")' not in src, name
