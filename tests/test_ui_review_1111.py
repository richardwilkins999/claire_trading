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
    for needle in ('id="view-desk" hidden', 'id="view-system"',
                   "What you hold", "Orders waiting to fill",
                   "function protectionOf(p)", "plan only", "no stop",
                   "perCcy(t, 'equity'"):
        assert needle in src, needle


def test_mission_control_opens_on_the_system_map():
    """System map is the first tab and the one the page opens on; Today is
    the second, reachable (and linkable) as /#today."""
    src = (WEB / "index.html").read_text()
    assert src.index('id="tab-system"') < src.index('id="tab-desk"')
    assert '<section id="view-system">' in src
    assert "showTab(location.hash === '#today' ? 'desk' : 'system');" in src
    assert "addEventListener('hashchange'" in src
    assert "homeTab" not in src and "'#system'" not in src


def test_today_and_portfolio_read_a_position_the_same_way():
    """Which stop-loss and take-profit act, who holds them and what they mean
    in money come from orderform.js for both pages, so the Today row and the
    Portfolio card cannot disagree. Portfolio draws the full price line;
    Today its own compact bar."""
    shared = (WEB / "orderform.js").read_text()
    for fn in ("function protectionFacts(p)", "function protectionHolder(l)",
               "function protectionMoney(p, f)",
               "function protectionLine(f, opt = {})"):
        assert fn in shared, fn
    home = (WEB / "index.html").read_text()
    pf = (WEB / "portfolio.html").read_text()
    for name, src in (("index.html", home), ("portfolio.html", pf)):
        assert '<script src="/orderform.js">' in src, name
        assert "protectionFacts(p)" in src and "protectionMoney(p, f)" in src, name
        # neither page picks the acting order or works out the money itself
        assert "l.leg === kind" not in src and "plTotal" not in src, name
    assert "protectionLine(f)" in pf and "ladder(" not in pf


def test_today_row_is_one_compact_line_led_by_the_money():
    """A holding's protection is one line: what the stop-loss would cost and
    what the take-profit would make (the price under each), with a bar
    between. Who acts is the tooltip; only what needs the owner is written
    out, inside the same line."""
    home = (WEB / "index.html").read_text()
    for gone in ("prot-who", "deskprot", "protectionGaps", "protectionLine(",
                 'class="ladder'):
        assert gone not in home, gone
    for needle in ('<div class="pcell" title="${esc(tip)}">', 'class="pbar"',
                   "side('l', f.sl, f.stop, m.stop, stopWord,",
                   "side('r', f.tp, f.tgt, m.target, 'take-profit'",
                   "no stop-loss", "plan only", 'class="plegend"'):
        assert needle in home, needle
    # the money comes before the price in each end of the line
    side = home[home.index("const side = ("):home.index("/* the bar:")]
    assert side.index("amt(money$)") < side.index("' ' + px(price)")


def test_portfolio_protection_tiles_show_the_figures_only():
    """A tile is its label, the price and the money outcome. How far from the
    price, the separate orders and who acts are its tooltip; a level with no
    order behind it and a missing stop-loss are still written out."""
    pf = (WEB / "portfolio.html").read_text()
    assert '<div class="ptile ${kind}" title="${esc(tip)}">' in pf
    for gone in ("pt-dist", "pt-note", "pt-foot", "pt-orders", "pt-dot"):
        assert gone not in pf, gone
    for kept in ("pt-price", "pt-out", "⚠ plan only", "warnNone: 'no stop-loss'",
                 "callout: alertFired(lv, ccy)", "floorOpen("):
        assert kept in pf, kept
    assert "pt-dot" not in (WEB / "orderform.css").read_text()
    # the money is the large first figure; the price it happens at is under it
    tile = pf[pf.index("function ptile(o)"):pf.index("function posCard(p)")]
    big = tile.index('<div class="pt-price ${out >= 0')
    assert "money(Math.abs(out), '')" in tile[big:tile.index("</div>", big)]
    assert big < tile.index("<b>${money(price, ccy)}</b>")


def test_ticker_on_today_pops_up_its_chart():
    """Resting on a ticker shows the chart of its Portfolio card — one
    drawing routine in charts.js for both."""
    charts = (WEB / "charts.js").read_text()
    assert "function drawPositionChart(el, d, opts)" in charts
    home = (WEB / "index.html").read_text()
    pf = (WEB / "portfolio.html").read_text()
    assert '<script src="/charts.js">' in home
    assert 'data-chart="${esc(p.instrument_id)}"' in home
    assert "drawPositionChart(" in home and "drawPositionChart(" in pf
    assert "label: 'your avg cost'" not in pf          # not drawn twice
    for needle in ("chartPopWire();", "'/api/position-chart?instrument='",
                   "pointer-events:none", "e.key === 'Escape'"):
        assert needle in home, needle


def test_nav_state_endpoint(web):                         # noqa: F811
    base, conn = web
    with paired(base) as c:
        s = c.get("/api/nav-state").json()
    assert s == {"pending": 1, "live_armed": False, "live_enabled": False,
                 "live_accounts": 0}
    # reading it must not write (it is polled by every open tab)
    assert conn.execute("SELECT COUNT(*) FROM live_trading").fetchone()[0] == 0


def test_chat_history_is_served_to_paired_browsers_only(web):  # noqa: F811
    import json

    import httpx
    base, conn = web
    conn.execute(
        "INSERT INTO chat_threads (thread, messages, updated_at) VALUES"
        " ('dashboard', ?, 5)", (json.dumps(
            [{"role": "user", "content": "what do I hold?"},
             {"role": "assistant", "content": "Six positions."},
             {"role": "tool", "content": "{raw}"},
             "not a message"]),))
    assert httpx.get(base + "/api/chat-history").status_code == 403
    with paired(base) as c:
        d = c.get("/api/chat-history?thread=dashboard").json()
        assert d["messages"] == [
            {"role": "user", "content": "what do I hold?"},
            {"role": "assistant", "content": "Six positions."}]
        assert d["updated_at"] == 5
        assert c.get("/api/chat-history?thread=nope").json()["messages"] == []
