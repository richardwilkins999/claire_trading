"""UI polish from REVIEW-2026-11-11 §6 (items 11 and 13–19): times with
their zone and a countdown, one freshness pill, redraws that keep unsaved
edits, an accessibility baseline, plain words on the approval card, the Runs
page's search and filter, one vocabulary, the chat page, and which Saxo
account opens first.

The pages have no build step and the desk has no node, so most of this is
static. Where a JavaScript engine happens to be installed (gjs — GNOME's,
SpiderMonkey) the shared helpers are also RUN, and every page's scripts are
compiled together; without one those tests skip."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "web"
PAGES = sorted(WEB.glob("*.html"))
SCRIPTS = sorted(WEB.glob("*.js"))
NAV = (WEB / "nav.js").read_text()
CSS = (WEB / "style.css").read_text()
GJS = shutil.which("gjs")
needs_js = pytest.mark.skipif(not GJS, reason="no JavaScript engine (gjs)")


def page(name):
    return (WEB / name).read_text()


def inline(src):
    return "\n".join(re.findall(r"<script>(.*?)</script>", src, re.S))


# ── a JavaScript engine, when there is one ─────────────────────────────────
HARNESS = r"""
const GLib = imports.gi.GLib;
const read = p => new TextDecoder().decode(GLib.file_get_contents(p)[1]);
const els = {};
let active = null;
const mk = id => ({id, hidden: true, className: '', textContent: '', title: '',
  attrs: {}, setAttribute(k, v) { this.attrs[k] = v; }});
globalThis.document = {getElementById: id => els[id] || (els[id] = mk(id)),
  addEventListener() {}, write() {}, get activeElement() { return active; }};
globalThis.localStorage = {getItem: () => ''};
globalThis.fetch = () => new Promise(() => {});
const nav = new Function(read(ARGV[0]) + `
  return {fmtWhen, fmtUntil, fmtClock, fmtSpan, zoneAbbr, linkify, ts,
          convictionText, kindName, pageStatus, swapHtml, unrealisedOf};`)();

/* a DOM just big enough for swapHtml: controls, <details>, focusable rows */
function parse(h) {
  const kids = [];
  const re = /<(input|select|textarea|details|tr)\b([^>]*)>/g;
  let m;
  while ((m = re.exec(h)) !== null) {
    const a = m[2], g = k => (new RegExp(k + '="([^"]*)"').exec(a) || [])[1];
    const k = {tag: m[1], id: g('id') || '', type: g('type') || '',
      value: g('value') || '', checked: /\bchecked\b/.test(a),
      open: /\bopen\b/.test(a), dataset: {}, cls: new Set(),
      focus() { active = this; }, setSelectionRange() {}};
    k.classList = {toggle: (c, on) => on ? k.cls.add(c) : k.cls.delete(c)};
    if (g('data-keep') != null) k.dataset.keep = g('data-keep');
    if (g('data-kbd') != null) k.dataset.kbd = g('data-kbd');
    kids.push(k);
  }
  return kids;
}
function fakeEl() {
  return {kids: [], firstChild: null, sets: 0,
    set innerHTML(h) { this.kids = parse(h); this.firstChild = {}; this.sets++; active = null; },
    querySelectorAll(sel) {
      if (sel.startsWith('input[id]')) return this.kids.filter(k =>
        ['input', 'select', 'textarea'].includes(k.tag) && k.id);
      if (sel === 'details[data-keep]') return this.kids.filter(k =>
        k.tag === 'details' && k.dataset.keep != null);
      if (sel === '[data-kbd]') return this.kids.filter(k => k.dataset.kbd != null);
      throw new Error('selector ' + sel);
    },
    contains(x) { return this.kids.includes(x); }, addEventListener() {}};
}
const out = {};
const T = 1794384000;                      // 2026-11-11 08:00:00 UTC
out.when = {
  sg: nav.fmtWhen(T, {tz: 'Asia/Singapore'}),
  ny: nav.fmtWhen(T, {tz: 'America/New_York'}),
  hk: nav.fmtWhen(T * 1000, {tz: 'Asia/Hong_Kong', weekday: false, year: true}),
  london_summer: nav.fmtWhen('2025-07-01T09:30:00+01:00', {tz: 'Europe/London'}),
  iso: nav.fmtWhen('2026-11-11T09:30:00-05:00', {tz: 'Asia/Singapore'}),
  seconds: nav.fmtWhen(T, {tz: 'UTC', weekday: false, seconds: true}),
  none: [nav.fmtWhen(null), nav.fmtWhen(''), nav.fmtWhen(0), nav.fmtWhen('rubbish')],
  local: nav.fmtWhen(T), ts: nav.ts(T)};
out.clock = [nav.fmtClock(T, 'Asia/Singapore'), nav.fmtClock(T, 'Europe/Berlin'),
             nav.fmtClock(T, 'Australia/Sydney'), nav.fmtClock(T, 'Asia/Tokyo')];
out.span = [10, 600, 7800, 10800, 93600, 273600, 20 * 86400 + 7200].map(nav.fmtSpan);
const now = Date.now() / 1000;
out.until = {soon: nav.fmtUntil(now + 7800), past: nav.fmtUntil(now - 10800),
  now: nav.fmtUntil(now + 5), days: nav.fmtUntil(now + 3 * 86400 + 4 * 3600),
  none: nav.fmtUntil(null), iso: nav.fmtUntil(new Date((now + 600) * 1000).toISOString())};
out.link = {
  plain: nav.linkify('Q3 results — https://example.com/q3?a=1&b=2.'),
  paren: nav.linkify('(see https://en.wikipedia.org/wiki/Nvidia_(company))'),
  hostile: nav.linkify('<img src=x onerror=alert(1)> javascript:alert(1) https://a.b/"onmouseover="x'),
  none: nav.linkify('Reuters, 3 Nov')};
out.words = [nav.convictionText(0.62), nav.convictionText(0.2), nav.convictionText(0.7),
             nav.convictionText(null), nav.kindName('sell_review'), nav.kindName('pipeline'),
             nav.kindName('something_new')];
const P = (pnl_base, price_missing = false) => ({pnl_base, price_missing});
out.upl = [nav.unrealisedOf([P(120.5), P(-20.5)], 999),
           nav.unrealisedOf([P(120.5), P(null, true)], 999),
           nav.unrealisedOf([P(null, true)], 999),
           nav.unrealisedOf([], 42), nav.unrealisedOf([P(null)], 42)];

const pill = els['nav-fresh'] = mk('nav-fresh');
const shot = () => ({text: pill.textContent, cls: pill.className, hidden: pill.hidden,
  title: pill.title, live: pill.attrs['aria-live']});
out.pill = {};
nav.pageStatus('ok', '', 'a'); out.pill.ok = shot();
nav.pageStatus('paused', '', 'b'); out.pill.paused = shot();
nav.pageStatus('ok', '', 'b');
nav.pageStatus('error', new Error('dashboard key required'), 'a'); out.pill.error = shot();
nav.pageStatus('ok', '', 'a'); out.pill.again = shot();

const el = fakeEl(), by = id => el.kids.find(k => k.id === id);
const H = m => `<input id="model-a" value="${m}"><input id="en-a" type="checkbox" checked>` +
  `<details data-keep="done"><tr data-kbd="row1"><tr data-kbd="row2">`;
const s = out.swap = {};
s.first = nav.swapHtml(el, H('opus'));
s.same = nav.swapHtml(el, H('opus')); s.sets = el.sets;
by('model-a').value = 'sonnet'; by('en-a').checked = false;
el.kids.find(k => k.tag === 'details').open = true;
el.kids[4].focus();
s.changed = nav.swapHtml(el, H('opus') + '<p>3m ago</p>');
s.kept = [by('model-a').value, by('model-a').cls.has('unsaved'),
          by('en-a').checked, by('en-a').cls.has('unsaved'),
          el.kids.find(k => k.tag === 'details').open, active && active.dataset.kbd];
nav.swapHtml(el, H('sonnet'));
s.saved = [by('model-a').value, by('model-a').cls.has('unsaved'), by('en-a').cls.has('unsaved')];
el.innerHTML = '<p>loading…</p>';
s.after_direct = nav.swapHtml(el, H('sonnet'));
print(JSON.stringify(out));
"""

COMPILE = r"""
const GLib = imports.gi.GLib;
const dir = ARGV[0];
const read = f => new TextDecoder().decode(GLib.file_get_contents(dir + '/' + f)[1]);
const d = GLib.Dir.open(dir, 0), files = [], bad = [];
let name;
while ((name = d.read_name()) !== null) files.push(name);
for (const f of files.sort()) {
  let code = '';
  if (f.endsWith('.js')) code = read(f);
  else if (f.endsWith('.html')) {
    const re = /<script(?:\s+src="\/([\w.]+\.js)")?\s*>([\s\S]*?)<\/script>/g;
    let m;
    while ((m = re.exec(read(f))) !== null) code += (m[1] ? read(m[1]) : m[2]) + '\n;\n';
  } else continue;
  try { new Function(code); } catch (e) { bad.push(f + ': ' + e); }
}
print(JSON.stringify(bad));
"""


def run_js(tmp_path, source, arg):
    script = tmp_path / "check.js"
    script.write_text(source)
    r = subprocess.run([GJS, str(script), str(arg)], capture_output=True,
                       text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def js(tmp_path_factory):
    return run_js(tmp_path_factory.mktemp("navjs"), HARNESS, WEB / "nav.js")


@needs_js
def test_every_pages_scripts_compile_together(tmp_path):
    """A page's inline script and the shared scripts it loads share one
    scope: a syntax error, an `await` outside an async function, or a
    const / let declared in both, stops the whole page. Compiled as one
    function body, each of those is an error here."""
    assert run_js(tmp_path, COMPILE, WEB) == []


# ── 14: times ──────────────────────────────────────────────────────────────
def test_shared_time_helpers_and_no_bare_local_times():
    for fn in ("fmtWhen(t, opts = {})", "fmtUntil(t, opts = {})",
               "fmtClock(t, tz, zone = true)", "zoneAbbr(t, tz)", "asDate(t)"):
        assert "function " + fn in NAV, fn
    assert "function ts(t) { return fmtWhen(t); }" in NAV
    # no page formats a date itself any more: no zone label, and each page
    # had its own format
    for f in PAGES + [WEB / "live.js", WEB / "orderinfo.js", WEB / "sell.js"]:
        src = f.read_text()
        assert "toLocaleTimeString" not in src, f.name
        assert not re.search(r"new Date\([^()]*(\([^()]*\))?[^()]*\)\s*\.toLocaleString",
                             src), f.name
        assert "toLocaleString([], {" not in src, f.name


def test_expiries_and_next_opens_are_countdowns():
    wl = page("watchlist.html")
    assert "esc(r.next_open" not in wl               # the raw ISO string
    assert "fmtUntil(r.next_open)" in wl
    for name, needles in {
        "watchlist.html": ["fmtUntil(t.expires_at)", "fmtWhen(t.created_at)"],
        "orders.html": ["fmtUntil(o.expires_at)", "fmtUntil(t.expires_at)"],
        "approvals.html": ["fmtUntil(a.expires_at)",
                           "fmtUntil(a.session.next_open)"],
        "index.html": ["fmtUntil(x.expires_at)"],
        "trading.html": ["fmtUntil(r.expires_at)"],
        "agents.html": ["fmtUntil(j.next_run_at)"],
        "maintenance.html": ["fmtUntil(s.next_run_at)"],
        "markets.html": ["fmtUntil(e.next_open)", "fmtClock(new Date(), e.tz)"],
        "orderinfo.js": ["function oiWhen(t) { return t ? fmtUntil(t) : '?'; }"],
    }.items():
        for n in needles:
            assert n in page(name), (name, n)
    # the two page-local "in 5m" helpers are gone (one said "5m", one "5 min")
    for name in ("agents.html", "trading.html"):
        assert "function until(" not in page(name), name


@needs_js
def test_times_carry_their_zone(js):
    w = js["when"]
    assert re.fullmatch(r"Wed 11 Nov( 2026)? 16:00 SGT", w["sg"]), w["sg"]
    assert re.fullmatch(r"Wed 11 Nov( 2026)? 03:00 EST", w["ny"]), w["ny"]
    assert w["hk"] == "11 Nov 2026 16:00 HKT"
    assert w["london_summer"] == "Tue 1 Jul 2025 09:30 BST"
    # an ISO time on the exchange's clock, shown on the reader's
    assert re.fullmatch(r"Wed 11 Nov( 2026)? 22:30 SGT", w["iso"]), w["iso"]
    assert re.fullmatch(r"11 Nov( 2026)? 08:00:00 UTC", w["seconds"]), w["seconds"]
    assert w["none"] == ["—", "—", "—", "—"]
    assert w["local"] == w["ts"] and re.search(r" \d\d:\d\d \S+$", w["local"])
    assert js["clock"] == ["16:00 SGT", "09:00 CET", "19:00 AEDT", "17:00 JST"]
    assert js["span"] == ["under a minute", "10 m", "2 h 10 m", "3 h", "1 d 2 h",
                          "3 d 4 h", "20 d"]


@needs_js
def test_countdowns(js):
    u = js["until"]
    assert re.fullmatch(r"in 2 h 10 m \(.*\d\d:\d\d \S+\)", u["soon"]), u["soon"]
    assert re.fullmatch(r"3 h ago \(.*\d\d:\d\d \S+\)", u["past"]), u["past"]
    assert u["now"].startswith("now (")
    # another day: the date is part of it
    assert re.fullmatch(r"in 3 d 4 h \(\w{3} \d+ \w{3}.* \d\d:\d\d \S+\)", u["days"]), u["days"]
    assert u["none"] == "—"
    assert u["iso"].startswith("in 10 m (")


# ── 15: one freshness pill ─────────────────────────────────────────────────
def test_one_shared_freshness_pill():
    assert 'id="nav-fresh"' in NAV
    for needle in ("function pageStatus(state, detail, key = 'page')",
                   "function tracked(fn, opts = {})", "'updated '",
                   "'paused while you are editing'", "'✖ cannot reach Claire'",
                   "el.title = tip"):
        assert needle in NAV, needle
    assert "nav .fresh" in CSS
    # said once: when the PAPER / LIVE pill already says it, this one hides
    assert "NAV_DOWN = true; drawPageStatus();" in NAV
    assert re.search(r"if \(down && NAV_DOWN\) \{.*?el\.hidden = true;", NAV, re.S)
    # a network failure is told apart from an answer that says no
    assert "err.unreachable = true" in NAV
    # a paused refresh runs when the field is left
    assert "PAGE_RESUME" in NAV and "'focusout'" in NAV


def test_every_polling_page_reports_through_the_pill():
    polling = 0
    for p in PAGES:
        src = inline(p.read_text())
        for m in re.finditer(r"\bpoll\((.*?),\s*\d+\)", src, re.S):
            polling += 1
            arg = m.group(1)
            names = [arg] if re.fullmatch(r"\w+", arg) else re.findall(r"(\w+)\(", arg)
            assert names, (p.name, arg)
            for n in names:
                assert re.search(rf"const {n} = tracked\(", src), \
                    f"{p.name}: {n} is polled but not tracked()"
        # no silent skip: the page's own rule is the wrapper's `paused`
        assert not re.search(r"if \([^)\n]*typing\(\)[^)\n]*\) return;", src), p.name
        if re.search(r"\btyping = \(\) =>", src) and "poll(" in src:
            assert re.search(r"paused: [^}]*typing\(\)", src), p.name
        # one place says when the data was loaded
        assert 'id="stamp"' not in p.read_text(), p.name
    assert polling >= 13
    assert 'id="conn"' not in page("watchlist.html")
    # Approvals used to say "live" while it had stopped refreshing
    ap = page("approvals.html")
    assert "paused: () => typing() || LADDER_DRAG" in ap
    assert "connPill('error')" not in "".join(p.read_text() for p in PAGES)


@needs_js
def test_the_pill_says_updated_paused_or_failed(js):
    p = js["pill"]
    assert re.fullmatch(r"updated \d\d:\d\d", p["ok"]["text"]) and not p["ok"]["hidden"]
    assert p["ok"]["cls"] == "fresh ok" and p["ok"]["live"] == "off"
    assert p["paused"]["text"] == "paused while you are editing"
    assert p["paused"]["cls"] == "fresh warn" and p["paused"]["live"] == "polite"
    assert "showing what was loaded at" in p["paused"]["title"]
    # the worst of the page's sources wins, with the error as the tooltip
    assert p["error"]["text"] == "✖ could not update"
    assert p["error"]["title"].startswith("dashboard key required")
    assert p["error"]["cls"] == "fresh bad"
    assert p["again"]["text"].startswith("updated ")


# ── 16: do not wipe edits ──────────────────────────────────────────────────
def test_redraws_keep_unsaved_edits_and_open_sections():
    assert "function swapHtml(el, html)" in NAV
    for needle in ("details[data-keep]", "classList.toggle('unsaved'",
                   "c.dataset.orig = ctlValue(c)"):
        assert needle in NAV, needle
    assert ".unsaved" in CSS
    ag = page("agents.html")
    assert "swapHtml(document.getElementById('agents')" in ag
    assert "swapHtml(document.getElementById('jobs')" in ag
    assert not re.search(r"getElementById\('(agents|jobs)'\)\.innerHTML", ag)
    od = page("orders.html")
    assert 'data-keep="done-triggers"' in od
    assert "swapHtml(document.getElementById('done-triggers')" in od
    assert not re.search(r"getElementById\('done-triggers'\)\.innerHTML", od)


@needs_js
def test_swap_html_behaviour(js):
    s = js["swap"]
    assert s["first"] is True
    assert s["same"] is False and s["sets"] == 1       # unchanged: untouched
    assert s["changed"] is True
    # the edited model and the unticked box survive, marked; the section
    # stays open; the focused row has the focus again
    assert s["kept"] == ["sonnet", True, False, True, True, "row2"]
    # once the server sends the saved value it is no longer "unsaved"; the
    # other edit still is
    assert s["saved"] == ["sonnet", False, True]
    # a page that wrote "loading…" straight into the element is noticed
    assert s["after_direct"] is True


# ── 18: accessibility baseline ─────────────────────────────────────────────
def _tag_at(src, i):
    quoted = False
    for j in range(i, min(len(src), i + 2500)):
        c = src[j]
        if c == '"':
            quoted = not quoted
        elif c == ">" and not quoted:
            return src[i:j + 1]
    return src[i:i + 300]


def test_every_form_control_has_a_label():
    """Inside a <label> (which then names it with for=), or a <label for>,
    or an aria-label. Placeholders are not labels: they vanish on typing."""
    missing, explicit = [], 0
    for f in PAGES + SCRIPTS:
        src = f.read_text()
        fors = set(re.findall(r'<label[^>]*?\bfor="([^"]+)"', src))
        explicit += len(fors)
        for m in re.finditer(r"<(input|select|textarea)\b", src):
            tag = _tag_at(src, m.start())
            idm = re.search(r'\bid="([^"]+)"', tag)
            before = src[:m.start()]
            if 'aria-label="' in tag or (idm and idm.group(1) in fors) \
                    or before.rfind("<label") > before.rfind("</label>"):
                continue
            missing.append((f.name, src.count("\n", 0, m.start()) + 1, tag[:70]))
    assert not missing, missing
    assert explicit >= 70


def test_clickable_rows_and_cards_work_from_the_keyboard():
    # one handler for all of them, in the shared script
    assert "t.matches('[data-kbd]')" in NAV
    assert "e.key === 'Enter' || e.key === ' '" in NAV
    assert "new MouseEvent('click'" in NAV            # SVG boxes have no .click()
    bare = []
    for f in PAGES + SCRIPTS:
        src = f.read_text()
        for m in re.finditer(r"<(tr|div|span|th|td|g|li|p)\b", src):
            tag = _tag_at(src, m.start())
            click = re.search(r'\bonclick="([^"]*)"', tag)
            if not click or click.group(1).strip() == "event.stopPropagation()":
                continue
            if "data-kbd=" not in tag or 'tabindex="0"' not in tag:
                bare.append((f.name, tag[:90]))
    assert not bare, bare
    # and the ones that are not table rows say what they are
    for name, needle in (("trading.html", 'class="runhead"'),
                         ("markets.html", 'class="mchip'),
                         ("index.html", 'class="runrow"')):
        src = page(name)
        tag = _tag_at(src, src.rindex("<", 0, src.index(needle)))
        assert 'role="button"' in tag, name
    assert 'role="button" tabindex="0" data-kbd="${esc(id)}" aria-label=' in page("index.html")


def test_state_never_relies_on_colour_alone():
    for f in PAGES + SCRIPTS:
        src = f.read_text()
        # a button that is only a symbol says what it does
        for m in re.finditer(r"<button\b", src):
            tag = _tag_at(src, m.start())
            body = src[m.start() + len(tag):m.start() + len(tag) + 40]
            sym = re.match(r"\s*([^<\w$]{1,6})\s*</button>", body)
            if sym:
                assert "aria-label=" in tag, (f.name, tag[:80])
    mk = page("markets.html")
    for state in ('aria-label="open">●', 'aria-label="lunch break">◐',
                  'aria-label="closed">○'):
        assert state in mk, state
    ix = page("index.html")
    assert "NODE_STATE" in ix and 'class="dotx"' in ix and 'class="dotw"' in ix
    assert "role=\"img\" aria-label=\"needs attention\"" in page("maintenance.html")
    # a real-money account is words, not a red option
    assert "'⚠ REAL MONEY · '" in page("watchlist.html")


def _lum(h):
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (1, 3, 5))

    def f(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def _contrast(a, b):
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_muted_text_has_enough_contrast_and_still_reads_as_secondary():
    var = dict(re.findall(r"--([\w-]+):(#[0-9a-fA-F]{6})", CSS))
    for name in ("dim", "muted"):
        for bg in ("bg", "panel", "panel2"):
            assert _contrast(var[name], var[bg]) >= 4.5, (name, bg)
        assert _contrast(var[name], "#101a2c") >= 4.5        # a hovered row
        # … and it is still clearly below the menu's grey and the body text
        assert _contrast(var[name], var["panel"]) < _contrast("#8fa6bd", var["panel"]) - 1
        assert _contrast(var[name], var["panel"]) < _contrast(var["text"], var["panel"]) / 2
    assert _contrast("#5d7590", var["panel"]) < 4.5           # what it was


# ── 17: the approval card, in plain words ──────────────────────────────────
def test_approval_card_explains_its_words():
    ap = page("approvals.html")
    # conviction: a word and the number, with what the number means
    assert "function convictionText(v)" in NAV and "CONVICTION_TIP" in NAV
    assert "convictionText(v)" in ap and "CONVICTION_TIP" in ap
    assert not re.search(r"conviction \$\{fmt\(", ap)        # the bare 0.62
    # the kind pill: "exit review", not sell_review
    assert "esc(kindName(a.kind))" in ap and "esc(a.kind)}</span>" not in ap
    assert "sell_review: 'exit review'" in NAV and "pipeline: 'research run'" in NAV
    for k in ("pipeline:", "sell_review:", "override:", "manual:"):
        assert k in ap[ap.index("const KIND_TIP"):ap.index("const AGENT")], k
    # PASS and the arbiter are explained where they first appear
    assert "<b>PASS</b> means the desk" in ap
    assert "DIR_TIP" in ap and "decided not to trade" in ap
    assert re.search(r'class="term"[^>]*title="The arbiter is the agent', ap)
    assert "arbiter: ['Arbiter'," in ap
    # ATR 14 / SMA 20 / SMA 50: a plain name on screen, the term in the tooltip
    for plain, term in (("'20-day average'", "SMA 20"), ("'50-day average'", "SMA 50"),
                        ("'Typical daily move'", "ATR 14")):
        i = ap.index("${cell(" + plain)
        assert term in ap[i:i + 420], plain
    assert "cell('SMA 20'" not in ap and "' (ATR 14)'" not in ap


def test_evidence_sources_are_links():
    ap = page("approvals.html")
    assert "${list('sources', 'sources', linkify)}" in ap
    assert "function linkify(s)" in NAV
    assert 'target="_blank" rel="noopener noreferrer"' in NAV
    assert r"(https?:\/\/[^\s<>" in NAV                      # http and https only


@needs_js
def test_linkify_escapes_and_links_only_web_addresses(js):
    li = js["link"]
    assert li["plain"] == (
        'Q3 results — <a class="ext" href="https://example.com/q3?a=1&amp;b=2" '
        'target="_blank" rel="noopener noreferrer">https://example.com/q3?a=1&amp;b=2</a>.')
    # a balanced bracket is part of the address; the sentence's own is not
    assert 'href="https://en.wikipedia.org/wiki/Nvidia_(company)"' in li["paren"]
    assert li["paren"].endswith("</a>)")
    h = li["hostile"]
    assert "<img" not in h and "&lt;img" in h
    assert h.count("<a ") == 1 and 'href="https://a.b/"' in h   # stops at the quote
    assert "javascript:alert(1)" in h and 'href="javascript' not in h
    assert "onmouseover=" in h and '"onmouseover' not in h.replace('&quot;', '')
    assert li["none"] == "Reuters, 3 Nov"
    assert js["words"] == ["medium conviction (0.62)", "low conviction (0.2)",
                           "high conviction (0.7)", "conviction not stated",
                           "exit review", "research run", "something new"]


# ── 11: the Runs page ──────────────────────────────────────────────────────
def test_runs_page_heading_filter_and_search():
    tr = page("trading.html")
    assert "<title>Claire — Runs</title>" in tr and "<h1>Runs</h1>" in tr
    assert "['/trading', 'Runs']" in NAV
    # a status filter over the list it already loads
    block = tr[tr.index("const FILTERS = {"):tr.index("let FILTER = 'all'")]
    for key, label in (("all:", "'All'"), ("waiting:", "'Waiting for you'"),
                       ("running:", "'Running'"), ("done:", "'Done'"),
                       ("failed:", "'Failed'")):
        assert key in block and label in block, key
    assert "RUNS.filter(FILTERS[FILTER][1])" in tr
    assert 'aria-pressed="${k === FILTER}"' in tr
    # every state the page knows lands in a filter other than "all"
    states = set(re.findall(r"^  (\w+): \['", tr[tr.index("const STATE = {"):
                                                 tr.index("function badge(")], re.M))
    filtered = set(re.findall(r"'(\w+)'", "".join(
        line for line in block.splitlines() if "r.state" in line)))
    assert states and states <= filtered | {"cancelled"}, states - filtered
    # the search Markets and Watchlist use; the hand-typed form is a fallback
    assert "api('/api/search?q=' + encodeURIComponent(q))" in tr
    assert "deskExchange(r)" in tr
    assert "<option>NASDAQ</option>" not in tr and "DESK_EX.map(" in tr
    i = tr.index("or type the\n      ticker and exchange yourself")
    assert 'id="ticker"' in tr[i:i + 600] and 'onclick="launch()"' in tr[i:i + 600]
    assert "Run pipeline" not in tr


# ── 13: one vocabulary ─────────────────────────────────────────────────────
def test_one_word_for_one_thing_in_page_text():
    def no_comments(src):                 # what the reader sees, not the notes
        src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
        return re.sub(r"(?m)(^|\s)//.*$", "", src)
    text = {p.name: no_comments(p.read_text()) for p in PAGES + SCRIPTS}
    gone = ("Run pipeline", "Send to pipeline", "give up (stop)", "thesis stop",
            "no work item attached", "The pipeline\\'s recommendation",
            "launch a\n        pipeline", "stop / target", "stop/target",
            "its stop and target", "pending theses", "trailing floor (")
    for name, src in text.items():
        for phrase in gone:
            assert phrase not in src, (name, phrase)
    assert "const LEG_NAME = {stop: 'stop-loss', target: 'take-profit'}" in NAV
    # raw run ids and rule codes moved out of prominent text into tooltips
    od = text["orders.html"]
    assert "${esc(o.work_item_id || '')}</a>" not in od
    assert 'title="run ${esc(o.work_item_id)}"' in od
    wl = text["watchlist.html"]
    assert "(${esc(r.work_item_id)})" not in wl
    assert ">${esc(t.work_item_id)}</a>" not in wl
    pf = text["portfolio.html"]
    assert "<td>${esc(a.rule)}</td>" not in pf
    assert 'title="rule code: ${esc(a.rule)}"' in pf and "trail_pct: ['trailing alert'" in pf
    assert "toast('launched ' + r.work_item_id" not in text["markets.html"]
    # the trigger text a run is started with is DATA (the server reads it)
    assert "trigger: 'launched from the Trading page'" in text["trading.html"]
    assert "trigger: 'launched from the Markets page'" in text["markets.html"]


# ── 19: the chat page ──────────────────────────────────────────────────────
def test_chat_page():
    ch = page("claire.html")
    # Enter cannot send a second message while a reply is streaming
    body = ch[ch.index("async function send()"):]
    assert body.index("if (BUSY) return;") < body.index("fetch('/api/ask'")
    assert body.index("BUSY = true;") < body.index("fetch('/api/ask'")
    assert "ready();\n  input.focus();" in body        # ready() ends BUSY
    assert "BUSY = false;" in ch[ch.index("function ready()"):ch.index("const TOOL_SAID")]
    # tool calls: a readable line, the raw call only in its tooltip
    assert "portfolio_summary: () => 'looked up the portfolio'" in ch
    assert "bubble('toolcall', said, raw)" in ch
    assert "bubble('toolcall', `⚙ ${ev.name}(" not in ch
    # approvals are on Approvals
    assert "Mission Control" not in ch and '<a href="/approvals">Approvals</a>' in ch
    # the conversation so far is read back from the server on load, through
    # api() (it needs the dashboard key) — and quietly not at all if it fails
    assert "api('/api/chat-history?thread=dashboard')" in ch
    hist = ch[ch.index("async function loadHistory()"):ch.index("function greet()")]
    assert "m.role === 'user'" in hist and "m.role === 'assistant'" in hist
    assert "bubble('claire', m.content)" in hist and "} catch {}" in hist
    assert "toast(" not in hist and "localStorage" not in ch
    # the input waits for it: BUSY starts true, ready() ends it
    assert "let BUSY = true;" in ch and hist.rstrip().endswith("ready();\n}")
    assert ch.rstrip().endswith("loadHistory();\n</script>")


# ── Saxo opens on SIM; a live account is unmistakable on the Watchlist ─────
def test_saxo_page_does_not_open_on_real_money():
    sx = page("saxo.html")
    assert "|| 'live'" not in sx
    assert "return ENVS.includes(last) ? last : 'sim';" in sx
    start = sx[sx.index("function startEnv()"):sx.index("let ENV = startEnv();")]
    # ?env= first, then the tab chosen last time, then SIM
    assert start.index(".get('env')") < start.index("localStorage.getItem('saxoEnv')")
    assert "localStorage.setItem('saxoEnv', ENV)" in sx
    assert sx.index('data-env="sim"') < sx.index('data-env="live"')


def test_unpriced_saxo_positions_are_never_summed_as_if_complete():
    assert "function unrealisedOf(pos, fallback)" in NAV
    for name in ("saxo.html", "portfolio.html"):
        src = page(name)
        assert "unrealisedOf(pos, b.UnrealizedMarginProfitLoss)" in src, name
        assert "(p.pnl_base || 0)" not in src, name          # the silent sum
        assert "esc(upl.note)" in src, name
        # the reason, when the server gives one, on the "no price" marker
        assert ("title=\"${esc(p.price_note || 'Saxo sent no price for this market')}\""
                ">no price</span>") in src, name


@needs_js
def test_unrealised_total_says_what_it_leaves_out(js):
    whole, part, none, empty, old = js["upl"]
    assert whole == {"value": 100, "unpriced": 0, "note": ""}
    assert part == {"value": 120.5, "unpriced": 1, "note": "1 of 2 positions unpriced"}
    # nothing priced: no figure at all — not the broker's, not zero
    assert none == {"value": None, "unpriced": 1, "note": "1 of 1 position unpriced"}
    # no positions, or a server that sends no per-position P/L: Saxo's figure
    assert empty["value"] == 42 and old["value"] == 42


def test_sending_a_trade_goes_straight_to_its_approval_card():
    assert "function toApprovalCard(id)" in NAV
    assert "location.href = '/approvals#c-' + encodeURIComponent(id);" in NAV
    assert "getElementById(decodeURIComponent(location.hash.slice(1)))" in page("approvals.html")
    for name, call in (("watchlist.html", "toApprovalCard(r.work_item_id)"),
                       ("sell.js", "toApprovalCard(r.work_item_id)"),
                       ("markets.html", "toApprovalCard(out.work_item_id)")):
        src = page(name)
        i = src.index(call)
        # only after the request succeeded, and a failure is still said aloud
        assert "api('/api/manual-trade'" in src[i - 900:i], name
        assert "toast(e.message, false)" in src[i:i + 200], name
    # the buttons open a form; they do not act at once
    wl = re.sub(r"/\*.*?\*/", "", page("watchlist.html"), flags=re.S)
    assert "Buy now" not in wl and "Sell now" not in wl
    assert "'buy')\">Buy…</button>" in wl and "'sell')\">Sell…</button>" in wl
    assert ">Sell…</button>" in page("sell.js")


def test_watchlist_marks_a_live_account():
    wl = page("watchlist.html")
    opts = wl[wl.index("function accountOptions("):wl.index("function buildDetail(")]
    assert "a.environment === 'live' ? '⚠ REAL MONEY · ' : ''" in opts
    assert 'class="livebadge" hidden>⚠ REAL MONEY ACCOUNT</b>' in opts
    assert "markLive('b-acct');" in wl and "markLive('t-acct');" in wl
    assert "select.liveacct" in wl
