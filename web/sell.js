/* The Sell button (Watchlist, Portfolio and Saxo accounts pages) —
   DESIGN.md §11a.

   Nothing here places an order. "Send to approval" creates a MANUAL sell
   (POST /api/manual-trade) that then waits on the Approvals page like every
   other trade, where you confirm the shares and the account with the
   approval token. On a Saxo account the desk first cancels the position's
   resting stop-loss / take-profit (Saxo refuses a sell while they rest),
   then sells; any shares left are re-protected at the same levels.

   A page registers each sellable position with sellRegister(key, pos) while
   rendering, puts <div id="sellbox-KEY"></div> where the form should open,
   and calls sellRestore() after every re-render so an open form survives
   the page's auto-refresh. pos = {ticker, exchange, broker, account_id,
   label, held, price, currency, whole, avg?, symbol?, source}. Needs
   orderform.js (the order summary, shared with the buy forms). Every server
   string is escaped. */
const SELLPOS = {}, SELLS = {}, SELL_R2Y = {};

function sellRegister(key, pos) { SELLPOS[key] = pos; }

function sellButton(key) {
  return `<button class="ghost" onclick="sellOpen(${jsArg(key)})"
    title="sell shares the desk holds — goes to Approvals first">Sell…</button>`;
}

const sellR2 = v => v == null || v === '' ? '' : r2(Number(v), Number(v));

function sellOpen(key) {
  const p = SELLPOS[key];
  if (!p) return;
  if (!SELLS[key]) {
    SELLS[key] = {mode: 'market', qty: p.held, px: sellR2(p.price), note: '', msg: ''};
  }
  sellRender(key);
  /* the 2-year high / low, for the price line (best effort, once a symbol) */
  if (p.symbol && !(p.symbol in SELL_R2Y)) {
    SELL_R2Y[p.symbol] = null;
    api('/api/range2y?symbol=' + encodeURIComponent(p.symbol))
      .then(d => { SELL_R2Y[p.symbol] = d.range2y; sellSummaryLine(key); })
      .catch(() => {});
  }
}

function sellClose(key) {
  delete SELLS[key];
  const el = document.getElementById('sellbox-' + key);
  if (el) el.innerHTML = '';
}

function sellRestore() {
  Object.keys(SELLS).forEach(k => { if (SELLPOS[k]) sellRender(k); });
}

function sellSave(key) {
  const s = SELLS[key];
  if (!s) return;
  const g = n => (document.getElementById(n + '-' + key) || {}).value;
  s.mode = g('so') || s.mode; s.qty = g('sq'); s.px = g('sp'); s.note = g('sn');
  sellSummaryLine(key);
}

/* the order type: "current price" locks the sale price to the live price */
function sellMode(key) {
  const s = SELLS[key], p = SELLPOS[key];
  if (!s || !p) return;
  s.mode = document.getElementById('so-' + key).value;
  if (s.mode === 'market') s.px = sellR2(p.price);
  sellRender(key);
}

/* All / Half of what the desk holds on this account */
function sellPart(key, f) {
  const s = SELLS[key], p = SELLPOS[key];
  if (!s || !p) return;
  sellSave(key);
  const q = p.held * f;
  s.qty = p.whole ? Math.max(1, Math.floor(q)) : Math.round(q * 1e4) / 1e4;
  const el = document.getElementById('sq-' + key);
  if (el) el.value = s.qty;
  sellSummaryLine(key);
}

/* what is wrong with the form, in plain words (empty when it can go) */
function sellProblems(key) {
  const p = SELLPOS[key], s = SELLS[key], out = [];
  const q = Number(s.qty), lim = Number(s.px), live = p.price;
  if (!(q > 0)) out.push('Enter the number of shares to sell.');
  else if (q > p.held + 1e-9) out.push(`The desk holds only ${fmt(p.held, 4)} here — it can only sell what it bought.`);
  else if (p.whole && q !== Math.floor(q)) out.push('Saxo sells whole shares only.');
  if (s.mode !== 'market') {
    if (!(lim > 0)) out.push('Enter a sale price.');
    else if (live && Math.abs(lim / live - 1) > 0.25)
      out.push(`The sale price is more than 25% from the current price (${money(live, esc(p.currency || ''))}) — the desk will refuse it.`);
  } else if (!live) out.push('No current price yet — choose “Set a sale price” instead.');
  return out;
}

function sellSummaryLine(key) {
  const el = document.getElementById('sv-' + key);
  const p = SELLPOS[key], s = SELLS[key];
  if (!el || !p || !s) return;
  const mkt = s.mode === 'market', live = p.price ?? null;
  const lim = mkt ? live : (Number(s.px) || null);
  const px = mkt ? live : fillPrice(lim, live, false);
  const probs = sellProblems(key);
  el.innerHTML = '<div class="osum-title">Order summary</div>' +
    sellSummary({shares: Number(s.qty) || 0, px, ccy: esc(p.currency || ''),
                 acct: {label: p.label || p.account_id, broker: p.broker}, mkt, live, lim,
                 ticker: p.ticker, exit: true, held: p.held, avg: p.avg,
                 r2y: p.symbol ? SELL_R2Y[p.symbol] : null}) +
    (probs.length ? `<dl>${probs.map(m => `<dd class="full bad">${m}</dd>`).join('')}</dl>` : '');
}

function sellRender(key) {
  const el = document.getElementById('sellbox-' + key);
  const p = SELLPOS[key], s = SELLS[key];
  if (!el || !p || !s) return;
  const k = esc(key), a = jsArg(key), mkt = s.mode === 'market';
  const ccy = esc(p.currency || '');
  const saxo = String(p.broker || '').startsWith('saxo');
  el.innerHTML = `<div class="card ord" style="margin:.4rem 0;padding:.6rem .8rem;
      border:1px solid var(--warn)">
    <p style="margin:0 0 .3rem"><b>Sell ${esc(p.ticker)}</b>
      <span class="muted">on ${esc(p.label || p.account_id)} · the desk holds
      <b>${fmt(p.held, 4)}</b> · now <b>${p.price == null ? '—'
      : money(p.price, ccy)}</b></span></p>
    <div class="row" style="flex-wrap:wrap">
      <label>Order type <select id="so-${k}" onchange="sellMode(${a})">
        <option value="market" ${mkt ? 'selected' : ''}>Sell at the current price — sells now</option>
        <option value="limit" ${mkt ? '' : 'selected'}>Set a sale price</option></select></label>
    </div>
    <div class="osum" id="sv-${k}"></div>
    <div class="row" style="flex-wrap:wrap">
      <label title="${mkt ? 'the current price — choose “Set a sale price” to type your own'
        : 'the least you accept per share — the order sells at this price or higher'}">Sale price
        <input id="sp-${k}" type="number" min="0" step="0.01" value="${esc(s.px)}"
          ${mkt ? 'readonly class="locked"' : ''} oninput="sellSave(${a})"> ${ccy}</label>
      <label>Shares to sell <input id="sq-${k}" type="number" min="0"
        step="${p.whole ? 1 : 'any'}" max="${esc(p.held)}"
        value="${esc(s.qty)}" oninput="sellSave(${a})"></label>
      <button type="button" class="ghost" onclick="sellPart(${a}, 1)">All</button>
      <button type="button" class="ghost" onclick="sellPart(${a}, 0.5)">Half</button>
      <input id="sn-${k}" placeholder="note (optional)" maxlength="200"
        style="flex:1;min-width:140px" value="${esc(s.note)}"
        oninput="sellSave(${a})">
    </div>
    <div class="row" style="margin-top:.4rem;gap:.5rem">
      ${saxo ? `<button type="button" onclick="sellCheck(${a})">✓ Check with Saxo</button>` : ''}
      <button type="button" onclick="sellSend(${a})">Send to approval →</button>
      <button type="button" class="ghost" onclick="sellClose(${a})">✖</button>
    </div>
    <p id="sm-${k}" style="font-size:.82rem;margin:.3rem 0">${s.msg || ''}</p>
    <p class="muted" style="font-size:.72rem;margin:0">Nothing is sold yet:
      this puts a SELL on the Approvals page, where you confirm the shares and
      the account.</p></div>`;
  sellSummaryLine(key);
}

function sellValues(key) {
  sellSave(key);
  const s = SELLS[key] || {}, p = SELLPOS[key] || {};
  const mkt = s.mode === 'market';
  return {qty: Number(s.qty), mkt, entry: mkt ? sellR2(p.price) : Number(s.px),
          note: s.note || null, p};
}

function sellMsg(key, html) {
  if (SELLS[key]) SELLS[key].msg = html;
  const el = document.getElementById('sm-' + key);
  if (el) el.innerHTML = html;
}

async function sellCheck(key) {
  const v = sellValues(key), probs = sellProblems(key);
  if (probs.length) return sellMsg(key, `<b class="bad">${probs[0]}</b>`);
  sellMsg(key, '<span class="muted">asking Saxo…</span>');
  try {
    const r = await api('/api/trade-check', {ticker: v.p.ticker,
      exchange: v.p.exchange, direction: 'sell', qty: v.qty, entry: v.entry,
      broker: v.p.broker});
    sellMsg(key, r.ok === null ? `<span class="warn">${esc(r.note)}</span>`
      : r.ok ? `<b class="ok">✔ Saxo would accept:</b> sell ${esc(r.amount)}
          @ ${esc(r.price)}`
      : `<b class="bad">✖ Saxo would refuse:</b> ${esc(r.error)}`);
  } catch (e) { sellMsg(key, `<b class="bad">${esc(e.message)}</b>`); }
}

async function sellSend(key) {
  const v = sellValues(key), probs = sellProblems(key);
  if (probs.length) return sellMsg(key, `<b class="bad">${probs[0]}</b>`);
  const how = v.mkt ? 'at the current price (about ' + v.entry + ')'
    : 'at a sale price of no less than ' + v.entry;
  if (!confirm(`Send to approval: SELL ${v.qty} ${v.p.ticker} ${how} on ` +
               `${v.p.label || v.p.account_id}?\n\nNothing is sold until you ` +
               'approve it on the Approvals page.')) return;
  try {
    const r = await api('/api/manual-trade', {ticker: v.p.ticker,
      exchange: v.p.exchange, direction: 'sell', entry: v.entry,
      note: v.note, source: v.p.source || 'portfolio',
      suggested: {qty: v.qty, broker: v.p.broker,
                  order_type: v.mkt ? 'market' : 'limit'}});
    sellMsg(key, `<b class="ok">✔ sent</b> — approve it on
      <a href="/approvals#c-${esc(r.work_item_id)}">Approvals →</a>
      <span class="muted">(${esc(r.work_item_id)})</span>`);
    toast('sell sent to approval ✓');
  } catch (e) { sellMsg(key, `<b class="bad">${esc(e.message)}</b>`); }
}
