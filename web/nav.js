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
    return;
  }
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
}
/* A data poll: every `ms` while the tab is visible, not at all while it is
   hidden, and once straight away when it is looked at again. */
function poll(fn, ms) {
  setInterval(() => { if (!document.hidden) fn(); }, ms);
  document.addEventListener('visibilitychange',
    () => { if (!document.hidden) fn(); });
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
  const r = await fetch(path, init);
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
function fmtBig(n) {
  if (n == null) return '—';
  const a = Math.abs(n);
  if (a >= 1e12) return fmt(n / 1e12) + 'T';
  if (a >= 1e9) return fmt(n / 1e9) + 'B';
  if (a >= 1e6) return fmt(n / 1e6) + 'M';
  if (a >= 1e3) return fmt(n / 1e3) + 'K';
  return fmt(n);
}
function ts(t) { return t ? new Date(t * 1000).toLocaleString() : '—'; }
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
/* the header pill: green as soon as the page's data has loaded; "live" once
   updates arrive the moment they happen */
function connPill(state) {
  const dot = document.getElementById('conn');
  if (!dot) return;
  if (state === 'live') { dot.textContent = '● live'; dot.className = 'pill ok'; dot.dataset.live = '1'; }
  else if (state === 'loaded') {
    if (dot.dataset.live) return;
    dot.textContent = '● loaded ' + new Date().toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
    dot.className = 'pill ok';
    dot.title = 'data loaded — instant updates are connecting; it also refreshes on its own';
  } else if (state === 'polling') {
    delete dot.dataset.live;
    dot.textContent = '○ auto-refresh'; dot.className = 'pill warn';
    dot.title = 'instant updates are not connected; the page still refreshes on its own';
  } else if (state === 'error') { delete dot.dataset.live; dot.textContent = '✖ cannot reach Claire'; dot.className = 'pill bad'; }
}
