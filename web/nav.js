/* The menu on every page. Five everyday pages inline; the rest in two
   groups, so it fits one line at laptop width (thirteen links used to wrap
   onto two). On the right: whether real money is switched on, always. */
const NAV = [
  ['/', 'Home'],
  ['/approvals', 'Approvals'], ['/orders', 'Orders'],
  ['/portfolio', 'Portfolio'], ['/watchlist', 'Watchlist'],
  ['/markets', 'Markets'],
  {label: 'Review', items: [['/trades', 'Trades'], ['/trading', 'Runs'],
                            ['/usage', 'Usage']]},
  {label: 'System', items: [['/agents', 'Agents & Schedule'],
                            ['/providers', 'Providers & MCP'],
                            ['/maintenance', 'Maintenance'],
                            ['/saxo', 'Saxo accounts']]},
  ['/claire', 'Ask Claire'],
];
function nav(active) {
  const link = ([href, label]) =>
    `<a href="${href}"${href === active ? ' class="active" aria-current="page"' : ''}>${label}` +
    (href === '/approvals' ? '<span class="badge" id="nav-pending" hidden></span>' : '') + '</a>';
  const html = NAV.map(it => Array.isArray(it) ? link(it)
    : `<details class="navgrp${it.items.some(([h]) => h === active) ? ' active' : ''}">
        <summary>${it.label}</summary>
        <div class="navmenu">${it.items.map(link).join('')}</div></details>`).join('');
  document.write('<nav aria-label="Claire">' + html +
    '<span style="flex:1"></span>' +
    '<span id="nav-fresh" class="fresh" hidden></span>' +
    '<span id="nav-mode" class="mode" title="checking…">…</span>' +
    '<a href="#" onclick="pair();return false" aria-label="dashboard key" ' +
    'title="paste the dashboard key from etc/claire.env">🔑</a>' +
    '</nav><div id="live-strip" class="livestrip" hidden></div><div id="msg"></div>');
  // one open group at a time; a click elsewhere closes it
  document.addEventListener('click', e => {
    document.querySelectorAll('details.navgrp[open]').forEach(d => {
      if (!d.contains(e.target)) d.open = false;
    });
  });
  /* a row or card that acts on a click (marked data-kbd, with a tabindex)
     acts on Enter and Space too — only when the row itself has the focus,
     never when a button or field inside it does */
  document.addEventListener('keydown', e => {
    const t = e.target;
    if ((e.key === 'Enter' || e.key === ' ') && t && t.matches &&
        t.matches('[data-kbd]')) { e.preventDefault(); t.click(); }
  });
  /* a refresh that waited because you were in a field runs once you leave
     it (not at once: the click that took the focus away must land first) */
  document.addEventListener('focusout', () => {
    clearTimeout(resumeTimer);
    resumeTimer = setTimeout(() => {
      for (const run of Object.values(PAGE_RESUME)) run();
    }, 1500);
  });
  navState();
  poll(navState, 30000);
}
/* PAPER or LIVE, and how many decisions are waiting — on every page. Paper
   versus live used to show only on Orders, and on Approvals once armed. */
async function navState() {
  const mode = document.getElementById('nav-mode');
  if (!mode) return;
  let s;
  try { s = await api('/api/nav-state'); }
  catch {
    mode.className = 'mode unknown'; mode.textContent = '? cannot reach Claire';
    mode.title = 'the dashboard server did not answer';
    NAV_DOWN = true; drawPageStatus();   // one "cannot reach", not two
    return;
  }
  NAV_DOWN = false;
  const n = document.getElementById('nav-pending');
  if (n) { n.hidden = !s.pending; n.textContent = s.pending || ''; }
  const strip = document.getElementById('live-strip');
  if (s.live_armed) {
    mode.className = 'mode live'; mode.textContent = '● LIVE ARMED';
    mode.title = 'Real-money trading is ON: approving a trade on the live account places a real order';
    strip.hidden = false;
    strip.innerHTML = 'LIVE TRADING IS ARMED — approving a trade on the live account places a real-money order. <a href="/orders">Turn it off on Orders</a>';
  } else {
    mode.className = 'mode paper'; mode.textContent = 'PAPER';
    mode.title = s.live_accounts ? 'Real-money trading is OFF. Paper and simulated accounts only.'
      : 'Paper and simulated accounts only — no live account is set up.';
    strip.hidden = true;
  }
  drawPageStatus();
}
/* A data poll: every `ms` while the tab is visible, not at all while it is
   hidden, and once straight away when it is looked at again. */
function poll(fn, ms) {
  setInterval(() => { if (!document.hidden) fn(); }, ms);
  document.addEventListener('visibilitychange',
    () => { if (!document.hidden) fn(); });
}
/* ── is what this page shows current? ─────────────────────────────────
   One pill in the menu bar, the same on every page (REVIEW-2026-11-11
   §6.3.15): "updated 12:03", "paused while you are editing", or
   "✖ cannot reach Claire" with the error as its tooltip. Six pages used to
   stop refreshing in silence whenever a field had the focus, and a failed
   fetch left stale numbers (or a blank page) with nothing to say so.

     const refresh = tracked(loadData, {paused: () => typing()});
     refresh(); poll(refresh, 30000);

   tracked(fn) runs fn and reports how it went; fn THROWS when its data
   could not be loaded. opts.paused(...args) is the page's own "not under
   your cursor" rule — when it answers true the refresh is skipped, the pill
   says so, and the refresh runs once you leave the field. opts.key names
   the source when a page has more than one (the pill shows the worst of
   them). The wrapper answers true (loaded) or false (failed or paused) and
   never rejects, so poll() needs no catch. */
const PAGE_STATE = {}, PAGE_RESUME = {};
let NAV_DOWN = false, resumeTimer = null;
function pageStatus(state, detail, key = 'page') {
  const was = PAGE_STATE[key] || {};
  const down = state === 'error' && !!(detail && detail.unreachable);
  PAGE_STATE[key] = {state, down,
    detail: detail == null ? '' : String(detail.message || detail),
    okAt: state === 'ok' ? Date.now() : was.okAt};
  drawPageStatus();
  /* the PAPER / LIVE pill is the other thing that says "cannot reach
     Claire": have it look again now, so the two never disagree for long */
  if ((down && !NAV_DOWN) || (state === 'ok' && NAV_DOWN)) navState();
}
function drawPageStatus() {
  const el = document.getElementById('nav-fresh');
  if (!el) return;
  const all = Object.values(PAGE_STATE);
  const errs = all.filter(s => s.state === 'error');
  const paused = all.find(s => s.state === 'paused');
  const oks = all.map(s => s.okAt).filter(Boolean);
  const asOf = oks.length ? fmtClock(Math.min(...oks)) : '';
  const shown = asOf ? ' — showing what was loaded at ' + asOf
    : ' — nothing has loaded yet';
  let cls = 'ok', text = 'updated ' + (oks.length
    ? fmtClock(Math.min(...oks), null, false) : ''), tip = 'Everything on this page ' +
    'was loaded at ' + asOf + ' or later. It refreshes on its own.';
  if (errs.length) {
    const down = errs.some(s => s.down);
    cls = 'bad';
    text = down ? '✖ cannot reach Claire' : '✖ could not update';
    tip = errs.map(s => s.detail).filter(Boolean).join(' · ') + shown;
    /* the PAPER / LIVE pill already says "cannot reach Claire": say it once */
    if (down && NAV_DOWN) {
      const mode = document.getElementById('nav-mode');
      if (mode) mode.title = tip;
      el.hidden = true;
      return;
    }
  } else if (paused) {
    cls = 'warn';
    text = 'paused while you are editing';
    tip = (paused.detail || 'This page does not redraw under your cursor. ' +
      'It refreshes when you leave the field.') + shown;
  }
  el.hidden = !all.length || (cls === 'ok' && !asOf);
  el.className = 'fresh ' + cls;
  if (el.textContent !== text) el.textContent = text;
  el.title = tip;
  /* read out a pause or a failure, not every minute's "updated" */
  el.setAttribute('aria-live', cls === 'ok' ? 'off' : 'polite');
}
function tracked(fn, opts = {}) {
  const key = opts.key || fn.name || 'page';
  const run = async (...args) => {
    if (opts.paused && opts.paused(...args)) {
      PAGE_RESUME[key] = run;
      pageStatus('paused', opts.why, key);
      return false;
    }
    delete PAGE_RESUME[key];
    try { await fn(...args); }
    catch (e) { pageStatus('error', e, key); return false; }
    pageStatus('ok', '', key);
    return true;
  };
  return run;
}
/* ── redraw without losing what you were doing ────────────────────────
   swapHtml(el, html) is el.innerHTML = html, except that it
     · does nothing when the HTML has not changed;
     · carries over the value of every field (with an id) you changed and
       have not saved, and marks it "unsaved";
     · keeps <details data-keep="name"> open or closed as you left them;
     · gives the focus back to the field (or data-kbd row) that had it.
   Agents & Schedule rebuilt every 15 s and threw away an unsaved model
   change; Orders closed its expanded sections every 30 s (§6.3.16). */
function ctlValue(c) {
  return c.type === 'checkbox' || c.type === 'radio' ? String(c.checked) : c.value;
}
function formDirty(root) {
  return !!root && [...root.querySelectorAll('[data-orig]')]
    .some(c => ctlValue(c) !== c.dataset.orig);
}
function swapHtml(el, html) {
  if (!el || el._swapHtml === html) return false;
  const FIELDS = 'input[id], select[id], textarea[id]';
  const edits = {}, open = {};
  let focus = null;
  if (el._swapHtml != null) {
    el.querySelectorAll(FIELDS).forEach(c => {
      if (c.dataset.orig != null && ctlValue(c) !== c.dataset.orig) edits[c.id] = c;
    });
    el.querySelectorAll('details[data-keep]').forEach(d => {
      open[d.dataset.keep] = d.open; });
    const a = document.activeElement;
    if (a && el.contains(a) && (a.id || a.dataset.kbd)) focus = {id: a.id,
      kbd: a.dataset.kbd, at: typeof a.selectionStart === 'number'
        ? [a.selectionStart, a.selectionEnd] : null};
  }
  el.innerHTML = html;
  el._swapHtml = html;
  el.querySelectorAll(FIELDS).forEach(c => {
    c.dataset.orig = ctlValue(c);
    const old = edits[c.id];
    if (!old) return;
    if (c.type === 'checkbox' || c.type === 'radio') c.checked = old.checked;
    else c.value = old.value;
    c.classList.toggle('unsaved', ctlValue(c) !== c.dataset.orig);
  });
  el.querySelectorAll('details[data-keep]').forEach(d => {
    if (d.dataset.keep in open) d.open = open[d.dataset.keep]; });
  if (focus) {
    const c = focus.id ? document.getElementById(focus.id)
      : [...el.querySelectorAll('[data-kbd]')].find(x => x.dataset.kbd === focus.kbd);
    if (c && el.contains(c)) {
      c.focus({preventScroll: true});
      if (focus.at) { try { c.setSelectionRange(focus.at[0], focus.at[1]); } catch {} }
    }
  }
  if (!el._swapWatch) {                 // mark a field the moment it is edited
    el._swapWatch = true;
    const mark = e => {
      const c = e.target;
      if (c && c.dataset && c.dataset.orig != null)
        c.classList.toggle('unsaved', ctlValue(c) !== c.dataset.orig);
    };
    el.addEventListener('input', mark);
    el.addEventListener('change', mark);
  }
  return true;
}

/* ── times ────────────────────────────────────────────────────────────
   Every time is shown in this browser's own zone AND says which zone that
   is — "Wed 11 Nov 16:00 SGT" — and anything still to come says how long
   until it: "in 2 h 10 m (16:00 SGT)" (REVIEW-2026-11-11 §6.3.14). Pages
   used to print bare local times, raw ISO strings ("2026-11-11T09:30:00
   -05:00", the exchange's own clock) and expiries with no countdown.
   `t` may be epoch seconds, epoch milliseconds, an ISO string or a Date.
   These change only how a time is SHOWN, never which instant it is. */
const ZONE_ABBR = {'Asia/Singapore': 'SGT', 'Asia/Hong_Kong': 'HKT',
  'Asia/Tokyo': 'JST', 'Asia/Kolkata': 'IST', 'Asia/Calcutta': 'IST',
  'Asia/Kuala_Lumpur': 'MYT', 'Asia/Shanghai': 'CST', 'Asia/Seoul': 'KST',
  'Asia/Bangkok': 'ICT', 'Asia/Jakarta': 'WIB', 'Asia/Manila': 'PHT',
  UTC: 'UTC'};
const ZONE_FMT = {};
function zoneFmt(locale, o) {
  const k = locale + JSON.stringify(o);
  return ZONE_FMT[k] || (ZONE_FMT[k] = new Intl.DateTimeFormat(locale, o));
}
function asDate(t) {
  if (t == null || t === '' || t === 0) return null;
  if (typeof t === 'string' && /^\d+(\.\d+)?$/.test(t)) t = Number(t);
  const d = t instanceof Date ? t
    : new Date(typeof t === 'number' && t < 1e11 ? t * 1000 : t);
  return isNaN(d) ? null : d;
}
/* "SGT", "EST", "CEST". Browsers know a zone's short name only in the
   locale that uses it (New York is "EST" in en-US and "GMT-5" in en-GB),
   and no locale has one for Hong Kong or Tokyo — so: the table, then each
   English locale in turn, then whatever the browser says ("GMT+8"). */
function zoneAbbr(t, tz) {
  const d = asDate(t) || new Date();
  let zone = tz, name = '';
  try { zone = zone || Intl.DateTimeFormat().resolvedOptions().timeZone; } catch {}
  if (ZONE_ABBR[zone]) return ZONE_ABBR[zone];
  for (const loc of ['en-GB', 'en-US', 'en-AU', 'en-SG', 'en-IN', 'en-CA', 'en-NZ']) {
    try {
      const o = {timeZoneName: 'short'};
      if (zone) o.timeZone = zone;
      const p = zoneFmt(loc, o).formatToParts(d).find(x => x.type === 'timeZoneName');
      if (!p) continue;
      if (!/^(GMT|UTC)[+\-−]/.test(p.value)) return p.value;
      name = name || p.value;
    } catch {}
  }
  return name;
}
/* "16:00 SGT" — or just "16:00" with zone = false */
function fmtClock(t, tz, zone = true) {
  const d = asDate(t);
  if (!d) return '—';
  const o = {hour: '2-digit', minute: '2-digit', hourCycle: 'h23'};
  if (tz) o.timeZone = tz;
  const s = zoneFmt('en-GB', o).format(d);
  return zone ? (s + ' ' + zoneAbbr(d, tz)).trim() : s;
}
/* "Wed 11 Nov 16:00 SGT" — the year too when it is not this year.
   opts: tz (an IANA zone instead of the browser's), weekday: false,
   year: true, seconds: true, zone: false (no abbreviation) */
function fmtWhen(t, opts = {}) {
  const d = asDate(t);
  if (!d) return '—';
  const o = {day: 'numeric', month: 'short', hour: '2-digit',
    minute: '2-digit', hourCycle: 'h23'};
  if (opts.weekday !== false) o.weekday = 'short';
  if (opts.seconds) o.second = '2-digit';
  if (opts.year || d.getFullYear() !== new Date().getFullYear()) o.year = 'numeric';
  if (opts.tz) o.timeZone = opts.tz;
  const s = zoneFmt('en-GB', o).format(d).replace(/,/g, '');
  return opts.zone === false ? s : (s + ' ' + zoneAbbr(d, opts.tz)).trim();
}
/* a length of time: "under a minute", "45 m", "2 h 10 m", "3 d 4 h" */
function fmtSpan(seconds) {
  const m = Math.round(Math.abs(seconds) / 60);
  if (m < 1) return 'under a minute';
  if (m < 60) return m + ' m';
  const h = Math.floor(m / 60), mm = m % 60;
  if (h < 24) return h + ' h' + (mm ? ' ' + mm + ' m' : '');
  const days = Math.floor(h / 24), hh = h % 24;
  return days + ' d' + (hh && days < 14 ? ' ' + hh + ' h' : '');
}
/* how long until (or since) a time, and the time itself:
   "in 2 h 10 m (16:00 SGT)" · "3 h ago (Wed 11 Nov 13:00 SGT)" · "now (…)".
   The date is left out when it is today. */
function fmtUntil(t, opts = {}) {
  const d = asDate(t);
  if (!d) return '—';
  const sec = (d.getTime() - Date.now()) / 1000;
  const today = !opts.tz && d.toDateString() === new Date().toDateString();
  const at = today ? fmtClock(d) : fmtWhen(d, opts);
  if (Math.abs(sec) < 30) return `now (${at})`;
  return (sec > 0 ? 'in ' + fmtSpan(sec) : fmtSpan(sec) + ' ago') + ` (${at})`;
}
/* An in-page confirmation, in place of the browser's own confirm() and
   prompt() — those were used for every money action, including "type LIVE"
   to place a real-money order (REVIEW-2026-11-11 §6.1.5).

     if (!await ask('Cancel BUY 10 NVDA?', {ok: 'Cancel the order'})) return;
     const typed = await ask('…', {type: 'LIVE', live: true});   // string | null

   message: plain text, line breaks kept. opts.ok / opts.cancel: button
   words — say what the button DOES. opts.danger: the action cannot be
   undone (red button, Cancel focused). opts.live: real money (red frame and
   banner). opts.type: a word that must be typed exactly before OK works.
   opts.input: a free-text field, opts.value its starting text.
   Resolves true/false, or the typed text / null when type or input is set. */
function ask(message, opts = {}) {
  return new Promise(resolve => {
    const d = document.createElement('dialog');
    d.className = 'ask' + (opts.live ? ' live' : '') + (opts.danger ? ' danger' : '');
    const wantsText = opts.type != null || opts.input;
    d.innerHTML =
      (opts.live ? '<div class="ask-live">⚠ REAL MONEY</div>' : '') +
      (opts.title ? `<h3>${esc(opts.title)}</h3>` : '') +
      '<p class="ask-msg"></p>' +
      (wantsText ? `<label class="ask-type">${opts.type != null
          ? 'Type <b>' + esc(opts.type) + '</b> to confirm' : esc(opts.label || '')}
          <input autocomplete="off" spellcheck="false"></label>` : '') +
      `<div class="ask-btns"><button type="button" class="ghost" value="no">${esc(opts.cancel || 'Go back')}</button>
        <button type="button" class="${opts.danger || opts.live ? 'reject' : ''}" value="yes">${esc(opts.ok || 'Confirm')}</button></div>`;
    d.querySelector('.ask-msg').textContent = message;
    const [no, yes] = d.querySelectorAll('.ask-btns button');
    const box = d.querySelector('input');
    if (box) {
      box.value = opts.value || '';
      if (opts.type != null) {
        yes.disabled = true;
        box.addEventListener('input', () => { yes.disabled = box.value !== opts.type; });
      }
      box.addEventListener('keydown', e => {
        if (e.key === 'Enter' && !yes.disabled) yes.click();
      });
    }
    const done = v => { d.close(); d.remove(); resolve(v); };
    no.onclick = () => done(wantsText ? null : false);
    yes.onclick = () => done(wantsText ? box.value : true);
    d.addEventListener('cancel', e => { e.preventDefault(); no.click(); });  // Esc
    document.body.appendChild(d);
    d.showModal();
    (box || (opts.danger || opts.live ? no : yes)).focus();
  });
}
/* a problem with what was typed: said in the page, not in a browser alert() */
function tellBad(text) { toast(text, false); }
function dashKey() { return localStorage.getItem('dashKey') || ''; }
async function pair() {
  const k = await ask('Paste the dashboard key. It is CLAIRE_DASHBOARD_KEY in etc/claire.env on the desk\'s own PC, and is kept only in this browser.',
    {title: 'Dashboard key', input: true, label: 'Key', value: dashKey(), ok: 'Save'});
  if (k !== null) { localStorage.setItem('dashKey', k.trim()); toast('key saved'); navState(); }
}
async function api(path, opts) {
  const headers = {'X-Dash-Key': dashKey()};
  let init;
  if (opts) {
    headers['Content-Type'] = 'application/json';
    init = {method: 'POST', headers, body: JSON.stringify(opts)};
  } else init = {headers};
  let r;
  try { r = await fetch(path, init); }
  catch (e) {
    /* no answer at all (server down, network gone) — not the same thing as
       an answer that says no; the header pill words them differently */
    const err = new Error('cannot reach Claire — ' + (e.message || e));
    err.unreachable = true;
    throw err;
  }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || data.detail || r.status);
  return data;
}
function toast(text, ok = true) {
  const el = document.getElementById('msg');
  el.textContent = text; el.style.display = 'block';
  el.style.borderColor = ok ? 'var(--good)' : 'var(--bad)';
  setTimeout(() => el.style.display = 'none', 4000);
}
function fmt(n, dp = 2) {
  return n == null ? '—' : Number(n).toLocaleString(undefined,
    {maximumFractionDigits: dp});
}
/* Money in more than one currency is shown as one figure PER currency,
   never as a sum: 56,801 SGD and 98,989 USD are not 155,790 of anything.
   `totals` is [{ccy, <key>: number}]; opts.signed prints + / − and colours. */
function perCcy(totals, key, opts = {}) {
  const rows = (totals || []).filter(t => t[key] != null);
  if (!rows.length) return '—';
  return '<span class="cvs">' + rows.map(t => {
    const v = t[key];
    const cls = opts.signed ? (v > 0 ? ' ok' : v < 0 ? ' bad' : '') : '';
    return `<span class="cv${cls}">${opts.signed && v > 0 ? '+' : ''}` +
      `${fmt(v, opts.dp == null ? 2 : opts.dp)}</span>` +
      `<span class="cc">${esc(t.ccy || '?')}</span>`;
  }).join('<br>') + '</span>';
}
/* [{ccy, <key>}...] summed per currency, for rows that arrive per account */
function sumByCcy(rows, key, ccyKey = 'ccy') {
  const by = {};
  for (const r of rows || []) {
    const c = r[ccyKey] || '?';
    by[c] = (by[c] || 0) + (r[key] || 0);
  }
  return Object.keys(by).sort().map(c => ({ccy: c, [key]: by[c]}));
}

/* The desk's ten exchanges, and how search results name them (Yahoo: "NMS",
   "SES"; Twelve Data: "NASDAQ", "SGX", "XETR"). deskExchange(hit) answers
   [exchange, ticker] — or [null, symbol] when the venue is not one the desk
   trades. It never guesses: Markets used to fall back to whichever exchange
   chip was selected, and sent runs and buys out with the wrong exchange and
   currency (REVIEW-2026-11-11 B12). */
/* The desk's ten exchanges: currency, Yahoo suffix, board lot, time zone.
   The pages' ONE copy — the same table as app/exchanges.py, and
   tests/test_exchanges.py fails if the two differ. Each page used to carry
   its own SUFFIX / CCY / TZ tables and "SGX ? 100 : 1". */
const DESK = {
  NASDAQ: {ccy: 'USD', suffix: '', lot: 1, tz: 'America/New_York'},
  NYSE: {ccy: 'USD', suffix: '', lot: 1, tz: 'America/New_York'},
  LSE: {ccy: 'GBP', suffix: '.L', lot: 1, tz: 'Europe/London'},
  XETRA: {ccy: 'EUR', suffix: '.DE', lot: 1, tz: 'Europe/Berlin'},
  PARIS: {ccy: 'EUR', suffix: '.PA', lot: 1, tz: 'Europe/Paris'},
  SGX: {ccy: 'SGD', suffix: '.SI', lot: 100, tz: 'Asia/Singapore'},
  HKEX: {ccy: 'HKD', suffix: '.HK', lot: 1, tz: 'Asia/Hong_Kong'},
  TSE: {ccy: 'JPY', suffix: '.T', lot: 1, tz: 'Asia/Tokyo'},
  ASX: {ccy: 'AUD', suffix: '.AX', lot: 1, tz: 'Australia/Sydney'},
  NSE: {ccy: 'INR', suffix: '.NS', lot: 1, tz: 'Asia/Kolkata'}
};
const DESK_EX = Object.keys(DESK);
const deskMap = k => Object.fromEntries(
  DESK_EX.filter(x => DESK[x][k]).map(x => [x, DESK[x][k]]));
/* NASDAQ and NYSE have no suffix and are absent, so a loop over the
   suffixes never matches the empty string */
const DESK_SUFFIX = deskMap('suffix');
const DESK_CCY = deskMap('ccy');
const DESK_TZ = deskMap('tz');
const deskLot = ex => (DESK[ex] || {}).lot || 1;
const DESK_EX_ALIAS = {NASDAQ: 'NASDAQ', NMS: 'NASDAQ', NGM: 'NASDAQ',
  NCM: 'NASDAQ', NASDAQGS: 'NASDAQ', NASDAQGM: 'NASDAQ', NASDAQCM: 'NASDAQ',
  NYSE: 'NYSE', NYQ: 'NYSE', SGX: 'SGX', SES: 'SGX', SINGAPORE: 'SGX',
  HKEX: 'HKEX', HKG: 'HKEX', 'HONG KONG': 'HKEX', JPX: 'TSE', TSE: 'TSE',
  TYO: 'TSE', TOKYO: 'TSE', ASX: 'ASX', SYDNEY: 'ASX', XETR: 'XETRA',
  XETRA: 'XETRA', GER: 'XETRA', PAR: 'PARIS', PARIS: 'PARIS', NSE: 'NSE',
  NSI: 'NSE', LSE: 'LSE', LON: 'LSE', LONDON: 'LSE'};
function deskExchange(r) {
  const sym = String(r.symbol || '');
  for (const [ex, suf] of Object.entries(DESK_SUFFIX))
    if (sym.toUpperCase().endsWith(suf.toUpperCase()))
      return [ex, sym.slice(0, -suf.length)];
  const raw = String(r.exchange || '').toUpperCase().trim();
  if (raw === 'EURONEXT' && String(r.country || '').toLowerCase() === 'france')
    return ['PARIS', sym];
  const ex = DESK_EX_ALIAS[raw];
  return ex ? [ex, sym] : [null, sym];
}
/* one word for one thing in page text (REVIEW-2026-11-11 §6.3.13): the
   data says leg "stop" / "target", the pages say stop-loss / take-profit */
const LEG_NAME = {stop: 'stop-loss', target: 'take-profit'};
/* what kind of run something is, in words — the data says "pipeline",
   "sell_review"; a raw kind never belongs in a heading or a pill */
const KIND_NAME = {pipeline: 'research run', sell_review: 'exit review',
  override: 'your override', manual: 'your trade'};
function kindName(k) { return KIND_NAME[k] || String(k || 'run').replace(/_/g, ' '); }
function fmtBig(n) {
  if (n == null) return '—';
  const a = Math.abs(n);
  if (a >= 1e12) return fmt(n / 1e12) + 'T';
  if (a >= 1e9) return fmt(n / 1e9) + 'B';
  if (a >= 1e6) return fmt(n / 1e6) + 'M';
  if (a >= 1e3) return fmt(n / 1e3) + 'K';
  return fmt(n);
}
function ts(t) { return fmtWhen(t); }
function ago(t) {
  if (!t) return '—';
  const s = Date.now() / 1000 - t;
  if (s < 90) return Math.round(s) + 's ago';
  if (s < 5400) return Math.round(s / 60) + 'm ago';
  if (s < 172800) return Math.round(s / 3600) + 'h ago';
  return Math.round(s / 86400) + 'd ago';
}
/* HTML-escape for BOTH text and attribute context. The old textContent →
   innerHTML trick left quotes alone, so title="${esc(x)}" could still be
   broken out of. Everything server-sent (provider names, models, tickers,
   work-item ids) goes through this before it touches innerHTML. */
const esc = s => String(s ?? '').replace(/[&<>"'`]/g, c => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  '`': '&#96;'})[c]);
/* a value as a JS string literal inside an inline handler attribute:
   onclick="f(${jsArg(id)})". JSON.stringify makes it a safe JS literal and
   esc keeps it inside the attribute; the browser decodes the entities back
   before the JS runs. Plain esc is NOT enough there — &#39; decodes to '
   before the handler is parsed. */
const jsArg = s => esc(JSON.stringify(String(s ?? '')));
function sparkline(series, w = 70, h = 22) {
  if (!series || series.length < 2) return '';
  const lo = Math.min(...series), hi = Math.max(...series);
  const up = series[series.length - 1] >= series[0];
  const pts = series.map((v, i) =>
    `${(i / (series.length - 1) * w).toFixed(1)},` +
    `${(h - 2 - (h - 4) * (v - lo) / (hi - lo || 1)).toFixed(1)}`).join(' ');
  return `<svg class="spark" width="${w}" height="${h}"><polyline
    points="${pts}" fill="none" stroke-width="1.4"
    stroke="${up ? 'var(--good)' : 'var(--bad)'}"/></svg>`;
}
/* minimal markdown: headings, bold, italics, code, bullets — no library */
function mdLite(t) {
  return esc(t)
    .replace(/^#{1,3} (.+)$/gm, '<h3>$1</h3>')
    .replace(/\*\*(.+?)\*\*/g, '<b>$1</b>')
    .replace(/(^|\s)_(.+?)_(?=\s|$)/g, '$1<i>$2</i>')
    .replace(/`(.+?)`/g, '<code>$1</code>')
    .replace(/^[-*] (.+)$/gm, '<li>$1</li>')
    .replace(/(<li>[\s\S]*?<\/li>)(?!\s*<li>)/g, '<ul>$1</ul>')
    .split(/\n{2,}/).map(p => p.match(/^\s*<(h3|ul)/) ? p
      : `<p>${p.replace(/\n/g, '<br>')}</p>`).join('');
}
function feeFor(model, notional) {
  if (!model) return 0;
  if (model.type === 'pct')
    return Math.max(notional * Number(model.pct || 0), Number(model.min || 0));
  return Number(model.per_trade || 0);
}

/* ── live updates, ONE stream for every Claire tab ────────────────────
   Each tab used to open its own EventSource. Chrome allows only 6
   connections per site, so a few open tabs (each holding a stream for up to
   15 min) left a new tab stuck on "connecting…" until a slot freed.
   Now one tab (the holder of a Web Lock) keeps the stream and relays each
   event to the others over a BroadcastChannel; when it closes, another tab
   takes over. Where Web Locks are unavailable (http:// from another device
   is not a secure context) each tab falls back to its own stream.
   onState(state): 'live' | 'polling'. */
function liveEvents(onEvent, onState) {
  const st = s => { try { onState && onState(s); } catch {} };
  const own = () => {
    const es = new EventSource('/api/events');
    es.onopen = () => st('live');
    es.onerror = () => st('polling');
    es.onmessage = ev => { try { onEvent(JSON.parse(ev.data)); } catch {} };
    return es;
  };
  if (!('locks' in navigator) || typeof BroadcastChannel === 'undefined') { own(); return; }
  const bc = new BroadcastChannel('claire-events');
  let lastBeat = 0;
  bc.onmessage = m => {
    const d = m.data || {};
    if (d.t === 'beat') { lastBeat = Date.now(); st(d.ok ? 'live' : 'polling'); }
    else if (d.t === 'ev') { lastBeat = Date.now(); try { onEvent(d.ev); } catch {} }
  };
  bc.postMessage({t: 'hello'});                  // ask the holder for a beat now
  navigator.locks.request('claire-events-leader', () => new Promise(() => {
    /* this tab holds the stream until it closes (the lock then passes on) */
    let ok = false;
    const es = new EventSource('/api/events');
    es.onopen = () => { ok = true; st('live'); bc.postMessage({t: 'beat', ok}); };
    es.onerror = () => { ok = false; st('polling'); bc.postMessage({t: 'beat', ok}); };
    es.onmessage = ev => {
      let e; try { e = JSON.parse(ev.data); } catch { return; }
      try { onEvent(e); } catch {}
      bc.postMessage({t: 'ev', ev: e});
    };
    const lead = new BroadcastChannel('claire-events');
    lead.onmessage = m => { if ((m.data || {}).t === 'hello') lead.postMessage({t: 'beat', ok}); };
    setInterval(() => lead.postMessage({t: 'beat', ok}), 10000);
  }));
  /* a follower that hears nothing for 30 s says so */
  setInterval(() => { if (lastBeat && Date.now() - lastBeat > 30000) st('polling'); }, 10000);
}
/* the pill beside a page's heading says HOW changes arrive: "live" (pushed
   the moment they happen) or "auto-refresh" (the page asks again on a
   timer). Whether what is on screen is current — updated, paused, cannot
   reach Claire — is the pill in the menu bar (pageStatus), on every page. */
function connPill(state) {
  const dot = document.getElementById('conn');
  if (!dot) return;
  if (state === 'live') {
    dot.textContent = '● live'; dot.className = 'pill ok'; dot.dataset.live = '1';
    dot.title = 'changes appear here the moment they happen';
  } else if (state === 'loaded') {
    if (dot.dataset.live) return;
    dot.textContent = '○ auto-refresh';
    dot.className = 'pill';
    dot.title = 'instant updates are still connecting; the page refreshes on its own meanwhile';
  } else if (state === 'polling') {
    delete dot.dataset.live;
    dot.textContent = '○ auto-refresh'; dot.className = 'pill warn';
    dot.title = 'instant updates are not connected; the page still refreshes on its own';
  }
}
