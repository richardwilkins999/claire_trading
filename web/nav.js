function nav(active) {
  const items = [["/", "Mission Control"], ["/approvals", "Approvals"],
    ["/orders", "Orders"], ["/trades", "Trades"],
    ["/markets", "Markets"], ["/watchlist", "Watchlist"],
    ["/claire", "Claire"], ["/trading", "Trading"],
    ["/portfolio", "Portfolio"], ["/saxo", "Saxo accounts"], ["/agents", "Agents & Schedule"],
    ["/providers", "Providers & MCP"], ["/usage", "Usage"],
    ["/maintenance", "Maintenance"]];
  document.write('<nav>' + items.map(([href, label]) =>
    `<a href="${href}" class="${href === active ? 'active' : ''}">${label}</a>`
  ).join('') +
  '<span style="flex:1"></span><a href="#" onclick="pair();return false" ' +
  'title="paste the dashboard key from etc/claire.env">🔑</a>' +
  '</nav><div id="msg"></div>');
}
function dashKey() { return localStorage.getItem('dashKey') || ''; }
function pair() {
  const k = prompt('Dashboard key (CLAIRE_DASHBOARD_KEY in etc/claire.env):',
    dashKey());
  if (k !== null) { localStorage.setItem('dashKey', k.trim()); toast('key saved'); }
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
const DESK_SUFFIX = {SGX: '.SI', HKEX: '.HK', TSE: '.T', ASX: '.AX',
  XETRA: '.DE', PARIS: '.PA', NSE: '.NS', LSE: '.L'};
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
