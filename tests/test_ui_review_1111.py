"""UI changes from REVIEW-2026-11-11 §6: phone-width pages, the grouped menu
with its PAPER / LIVE indicator, the "Today" home view, and in-page
confirmations in place of the browser's confirm() / prompt() / alert()."""
import re
from pathlib import Path

from tests.test_dashboards import paired, web  # noqa: F401 — fixture

WEB = Path(__file__).resolve().parents[1] / "web"
PAGES = sorted(WEB.glob("*.html"))
NAV = (WEB / "nav.js").read_text()
CSS = (WEB / "style.css").read_text()


def test_every_page_is_standards_mode_and_phone_width():
    assert len(PAGES) == 14
    for page in PAGES:
        head = page.read_text()[:400]
        assert head.lower().startswith("<!doctype html>"), page.name
        assert 'name="viewport" content="width=device-width' in head, page.name
        assert '<meta charset="utf-8">' in head, page.name


def test_no_browser_dialogs_are_left_on_any_page():
    """confirm() and prompt() guarded every money action, including typing
    LIVE to place a real-money order; alert() reported form errors."""
    for f in list(PAGES) + sorted(WEB.glob("*.js")):
        src = re.sub(r"/\*.*?\*/", "", f.read_text(), flags=re.S)   # comments
        for word in ("confirm", "prompt", "alert"):
            assert not re.search(rf"(?<![\w.]){word}\(", src), (f.name, word)


def test_every_confirmation_is_awaited():
    """ask() returns a Promise: `if (!ask(...))` would never stop anything."""
    for f in list(PAGES) + sorted(WEB.glob("*.js")):
        for m in re.finditer(r"(.{0,12})(?<![\w.])ask\(", f.read_text()):
            before = m.group(1)
            if "function " in before or before.rstrip().endswith("*"):
                continue                       # the definition / a comment
            assert "await " in before or "= " in before or "p = " in before \
                or before.strip().startswith(("//", "*")), (f.name, before)


def test_reject_and_real_money_are_guarded():
    src = (WEB / "approvals.html").read_text()
    assert "action === 'reject'" in src and "Reject this trade?" in src
    assert src.count("type: 'LIVE'") == 2           # approve and retry
    assert "live: true" in src
    live = (WEB / "live.js").read_text()
    assert "type: st.arm_phrase" in live and "live: true" in live


def test_the_shared_menu_has_groups_mode_and_badge():
    assert "function ask(message, opts = {})" in NAV
    assert "function poll(fn, ms)" in NAV and "document.hidden" in NAV
    assert "async function navState()" in NAV and "/api/nav-state" in NAV
    for label in ("Review", "System", "Ask Claire", "LIVE ARMED", "PAPER"):
        assert label in NAV, label
    # every page is reachable from the menu
    hrefs = set(re.findall(r"\['(/[a-z]*)', '", NAV))
    assert hrefs == {"/", "/approvals", "/orders", "/portfolio", "/watchlist",
                     "/markets", "/trades", "/trading", "/usage", "/agents",
                     "/providers", "/maintenance", "/saxo", "/claire"}


def test_data_polls_pause_while_the_tab_is_hidden():
    for page in PAGES:
        src = page.read_text()
        for m in re.finditer(r"setInterval\(([^,]+),\s*(\d+)\)", src):
            assert int(m.group(2)) < 5000 or "tick" in m.group(1) \
                or "document.hidden" in m.group(1), (page.name, m.group(0))


def test_base_styles():
    assert "color-scheme:dark" in CSS
    assert re.search(r"^a \{ color:var\(--accent\)", CSS, re.M)   # not blue
    assert ":focus-visible" in CSS and "@media (max-width: 760px)" in CSS
    assert "dialog.ask" in CSS and "margin:auto" in CSS


def test_home_page_answers_the_daily_question():
    src = (WEB / "index.html").read_text()
    for needle in ('id="view-desk"', 'id="view-system" hidden',
                   "What you hold", "Orders waiting to fill",
                   "function protectionOf(p)", "plan only", "no stop",
                   "perCcy(t, 'equity'"):
        assert needle in src, needle


def test_nav_state_endpoint(web):                           # noqa: F811
    base, conn = web
    with paired(base) as c:
        s = c.get("/api/nav-state").json()
    assert s == {"pending": 1, "live_armed": False, "live_enabled": False,
                 "live_accounts": 0}
    # reading it must not write (it is polled by every open tab)
    assert conn.execute("SELECT COUNT(*) FROM live_trading").fetchone()[0] == 0
