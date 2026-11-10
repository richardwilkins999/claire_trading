/* Shared by the Approvals card and the Watchlist forms: the order summary
   (headline, max loss / gain / reward:risk tiles, the price line, today's and
   2-year ranges), the amount ⇄ shares maths and the suggested levels. Needs
   nav.js (fmt, esc). Display only — nothing here sends an order. */

/* shares for an amount (in the account currency) at a price per share already
   converted into that currency: floored to lots, whole shares, or 0.1 */
function sharesFor(amount, pxBase, {lot = 1, whole = false} = {}) {
  if (!amount || !pxBase) return 0;
  const raw = amount / pxBase;
  return lot > 1 ? Math.floor(raw / lot) * lot : whole ? Math.floor(raw) : Math.floor(raw * 10) / 10;
}
/* where a LIMIT order really fills: at the current price when that is
   already better than your price (a buy limit of 40 with the stock at 33
   buys at ~33), else at your price. Stops and sizes are measured from it. */
function fillPrice(lim, live, buy = true) {
  if (lim == null) return live;
  if (live == null) return lim;
  return buy ? Math.min(lim, live) : Math.max(lim, live);
}
function amountFor(shares, pxBase) {
  return shares && pxBase ? Math.round(shares * pxBase * 100) / 100 : null;
}
const r2 = (v, px) => v == null ? null : Math.round(v * (px != null && px < 1 ? 1e4 : 100)) / (px != null && px < 1 ? 1e4 : 100);
/* a stated rule when there is no plan: stop 2 × the typical daily move (ATR
   14; 8% without one), take-profit at twice the risk — both inside Saxo's 25% */
function suggestLevels(px, atr, buy = true) {
  if (!px) return {stop: null, tgt: null};
  const risk = Math.min(atr ? 2 * atr : px * 0.08, px * 0.24);
  const reward = Math.min(2 * risk, px * 0.24);
  return buy ? {stop: r2(px - risk, px), tgt: r2(px + reward, px)}
             : {stop: r2(px + risk, px), tgt: r2(px - reward, px)};
}

/* the order summary under the inputs: one plain row per fact */
function money(v, ccy) {
  if (v == null || isNaN(v)) return '—';
  /* 2 decimals; below 1, up to 4 when they matter (a 0.4523 share price) */
  const max = Math.abs(v) < 1 && v !== 0 ? 4 : 2;
  return Number(v).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: max}) + (ccy ? ' ' + ccy : '');
}
function sumRows(rows) {
  return `<div class="osum-title">Order summary</div><dl>${rows.filter(Boolean).map(([k, v, cls]) => k
    ? `<dt>${k}</dt><dd${cls ? ` class="${cls}"` : ''}>${v}</dd>`
    : `<dd class="full ${cls || ''}">${v}</dd>`).join('')}</dl>`;
}
/* a buy's summary: headline, the three numbers that matter, a price line
   (stop · limit · now · take-profit on one scale) and the fill status */
/* ── dragging the stop-loss / buy / take-profit along the price line ────
   A page opts in by passing {drag: <card id>} to buySummary and defining
   window.onLadderDrag(id, key, value, done). While a marker is held the
   scale is frozen (LADDER_DRAG), so the line does not jump under the
   pointer; it re-fits when you let go. */
let LADDER_DRAG = null;
function ladderGrab(e, id, k) {
  const box = e.target.closest('.ladder');
  if (!box) return;
  e.preventDefault();
  LADDER_DRAG = {id, k, lo: Number(box.dataset.lo), hi: Number(box.dataset.hi),
                 rect: box.getBoundingClientRect()};
  document.body.classList.add('ladder-dragging');
}
function ladderApply(d) {
  d.timer = null;
  if (d.x == null) return;
  const f = Math.min(1, Math.max(0, (d.x - d.rect.left) / d.rect.width));
  const v = d.lo + f * (d.hi - d.lo);
  if (window.onLadderDrag) window.onLadderDrag(d.id, d.k, r2(v, v), false);
}
/* ~60 updates a second while dragging; the last position is always applied */
window.addEventListener('pointermove', e => {
  const d = LADDER_DRAG;
  if (!d) return;
  d.x = e.clientX;
  if (!d.timer) d.timer = setTimeout(() => { if (LADDER_DRAG === d) ladderApply(d); }, 16);
});
window.addEventListener('pointerup', () => {
  const d = LADDER_DRAG;
  if (!d) return;
  clearTimeout(d.timer);
  ladderApply(d);                      // where you let go
  LADDER_DRAG = null;
  document.body.classList.remove('ladder-dragging');
  if (window.onLadderDrag) window.onLadderDrag(d.id, d.k, null, true);
});

/* keep the dragged marker on its side: a buy's stop below the purchase
   price and its take-profit above (the reverse for a sell); the purchase
   price stays between the two */
function ladderClamp(k, v, {entry, stop, tgt, buy = true}) {
  const tick = v < 1 ? 1e-4 : 0.01;
  if (k === 'entry') {
    if (stop != null) v = buy ? Math.max(v, stop + tick) : Math.min(v, stop - tick);
    if (tgt != null) v = buy ? Math.min(v, tgt - tick) : Math.max(v, tgt + tick);
  } else if (k === 'trail') {
    if (entry) v = buy ? Math.min(v, entry * 0.995) : Math.max(v, entry * 1.005);
  } else if (entry) {
    const below = (k === 'stop') === buy;
    v = below ? Math.min(v, entry - tick) : Math.max(v, entry + tick);
  }
  return r2(v, v);
}
/* Claire: on Saxo (an account with venue_stops) the trail % is a real
   TRAILING STOP that Saxo holds and executes — it replaces the stop-loss and
   sells by itself. On the built-in sims, and on the SGX shares saxo-paper
   simulates (local_exchanges), it is the watcher's ALERT — it asks you. */
function trailIsStop(acct, exchange) {
  return !!(acct && acct.venue_stops) &&
    !((acct.local_exchanges || []).includes(exchange));
}
function trailName(acct, exchange) {
  return trailIsStop(acct, exchange) ? 'trailing stop' : 'trailing alert';
}
function trailTip(acct, exchange) {
  return trailIsStop(acct, exchange)
    ? 'an order at Saxo: it replaces the stop-loss, follows the best price since you bought at this distance, never moves back, and sells automatically when hit'
    : 'not an order: the watcher alerts you (and starts a sell review) if the price falls this far below its best since you bought — it never sells by itself';
}
/* relabel every element marked data-trail="<key>": its .trail-name text
   and its tooltip follow the account (and exchange) now chosen */
function syncTrail(key, acct, exchange) {
  document.querySelectorAll(`[data-trail="${key}"]`).forEach(el => {
    const n = el.querySelector('.trail-name'), name = trailName(acct, exchange);
    if (n) n.textContent = name[0].toUpperCase() + name.slice(1);
    el.title = trailTip(acct, exchange);
  });
}
/* where the trailing alert STARTS for a trail % (it then rises with the
   best price since you bought), and the % for a dragged position */
function trailStart(entry, pct) { return entry && pct ? r2(entry * (1 - pct / 100), entry) : null; }
function trailPctAt(entry, v) {
  if (!entry || !v) return null;
  return Math.min(90, Math.max(0.5, Math.round((1 - v / entry) * 1000) / 10));
}

function ladder(pts, ccy, refs = [], opt = {}) {
  const vals = pts.filter(p => p.v != null).map(p => p.v);
  if (vals.length < 2) return '';
  let lo = Math.min(...vals), hi = Math.max(...vals);
  const frozen = LADDER_DRAG && opt.drag && LADDER_DRAG.id === opt.drag ? LADDER_DRAG : null;
  /* a 2-year high / low is drawn when it falls among (or just beyond) the
     trade's own prices — that is where it matters to this order */
  const span = hi - lo;
  refs = refs.filter(r => r.v != null && r.v >= lo - span * 0.25 && r.v <= hi + span * 0.25);
  refs.forEach(r => { lo = Math.min(lo, r.v); hi = Math.max(hi, r.v); });
  /* room to drag: a little beyond the outermost prices */
  const pad = (hi - lo) * (opt.drag ? 0.15 : 0.06) || hi * 0.02; lo -= pad; hi += pad;
  if (frozen) { lo = frozen.lo; hi = frozen.hi; }
  const x = v => ((Math.min(hi, Math.max(lo, v)) - lo) / (hi - lo) * 100).toFixed(2) + '%';
  const P = Object.fromEntries(pts.map(p => [p.k, p.v]));
  const DRAGGABLE = {stop: 1, entry: 1, target: 1, trail: 1};
  const zone = (a, b, cls) => a != null && b != null
    ? `<div class="lz ${cls}" style="left:${x(Math.min(a, b))};width:calc(${x(Math.max(a, b))} - ${x(Math.min(a, b))})"></div>` : '';
  /* labels that would overlap go up a lane: markers labelled ABOVE the
     line (now, trailing, what you paid, 2-year refs) and BELOW it (stop,
     purchase price, target) are stacked separately */
  const ABOVE = {now: 1, trail: 1, paid: 1, ref: 1};
  const marks = [...pts.filter(p => p.v != null), ...refs.map(r => ({...r, k: 'ref'}))]
    .map(p => ({...p, at: (Math.min(hi, Math.max(lo, p.v)) - lo) / (hi - lo) * 100}));
  /* the price NOW is placed first, so it always keeps the row nearest
     the line; the others take the first row where they do not overlap */
  const GAP = opt.labelGap || 13, lanes = {up: [], down: []};
  marks.slice().sort((p, q) => (q.k === 'now') - (p.k === 'now') || p.at - q.at).forEach(p => {
    const side = ABOVE[p.k] ? lanes.up : lanes.down;
    let lane = 0;
    while (side[lane] && side[lane].some(x => Math.abs(p.at - x) < GAP)) lane++;
    (side[lane] = side[lane] || []).push(p.at); p.lane = lane;
  });
  const LANE = 26, upN = lanes.up.length, downN = lanes.down.length;
  const lab = p => p.lane ? ` style="${ABOVE[p.k] ? `bottom:${30 + p.lane * LANE}px` : `position:relative;top:${p.lane * LANE}px`}"` : '';
  return `<div class="ladder${opt.drag ? ' can-drag' : ''}" data-lo="${lo}" data-hi="${hi}"${
    upN > 1 || downN > 1 ? ` style="margin-top:calc(2.1rem + ${Math.max(0, upN - 1) * LANE}px);height:${66 + Math.max(0, downN - 1) * LANE}px"` : ''}>
    <div class="lbar"></div>
    ${zone(P.stop, P.entry, 'loss')}${zone(P.entry, P.target, 'gain')}${(opt.zones || []).map(z => zone(...z)).join('')}
    ${marks.filter(p => p.k !== 'ref').map(p => `<div class="lm ${p.k}${opt.drag && DRAGGABLE[p.k] ? ' drag' : ''}${
      frozen && frozen.k === p.k ? ' held' : ''}" style="left:${x(p.v)}"${opt.drag && DRAGGABLE[p.k]
      ? ` title="drag to change" onpointerdown='ladderGrab(event, ${JSON.stringify(opt.drag)}, "${p.k}")'` : ''}>
      <span class="ltick"></span>${p.k === 'now' ? '<span class="ldot"></span>' : ''}<span class="llab"${lab(p)}>${
        p.k === 'now' ? `<span class="lnow">NOW <b>${money(p.v, '')}</b></span>` : `<b>${money(p.v, '')}</b><br>${p.label}`}</span></div>`).join('')}
    ${marks.filter(p => p.k === 'ref').map(r => `<div class="lm ref" style="left:${x(r.v)}" title="${esc(r.tip || '')}">
      <span class="ltick"></span><span class="llab"${lab(r)}><b>${money(r.v, '')}</b><br>${r.label}</span></div>`).join('')}
  </div>`;
}
/* where the price (and this trade) sits in a range: today's and 2 years' */
function rangeRow(label, r, now, ccy, opt = {}) {
  if (!r || r.high == null || r.low == null || r.high <= r.low) return '';
  const {stop, tgt, dated} = opt;
  const ext = [r.low, r.high, now, ...(dated ? [stop, tgt] : [])].filter(v => v != null);
  const lo = Math.min(...ext), hi = Math.max(...ext);
  const x = v => ((v - lo) / (hi - lo) * 100).toFixed(2) + '%';
  const d = t => dated && t ? ` <span class="muted">(${new Date(t * 1000).toLocaleDateString([], {month: 'short', year: 'numeric'})})</span>` : '';
  const pos = now != null ? Math.round((now - r.low) / (r.high - r.low) * 100) : null;
  const where = pos == null ? '' : pos > 100 ? ` · now <b class="warn">above the high</b>`
    : pos < 0 ? ` · now <b class="warn">below the low</b>` : ` · now <b>${pos}%</b> of the way up`;
  return `<div class="rrow"><span class="rlab">${label}</span>
    <div class="rbar">
      <div class="r-span" style="left:${x(r.low)};width:calc(${x(r.high)} - ${x(r.low)})"></div>
      ${dated && stop != null && tgt != null ? `<div class="r-trade" style="left:${x(stop)};width:calc(${x(tgt)} - ${x(stop)})" title="your stop-loss → take-profit"></div>` : ''}
      ${now != null ? `<div class="r-now" style="left:${x(now)}" title="now ${money(now, ccy)}"></div>` : ''}
    </div>
    <span class="rtxt"><b>${money(r.low, '')}</b>${d(r.low_at)} – <b>${money(r.high, '')}</b>${d(r.high_at)} ${ccy}${where}${
      dated && tgt != null && tgt > r.high ? ' · <span class="warn">take-profit above the 2-year high</span>' : ''}</span></div>`;
}
function ranges(lv, r2y, now, stop, tgt, ccy) {
  const today = lv && lv.day_low != null ? {low: lv.day_low, high: lv.day_high} : null;
  const rows = rangeRow('Today', today, now, ccy) + rangeRow('2 years', r2y, now, ccy, {stop, tgt, dated: true});
  return rows ? `<div class="ranges">${rows}</div>` : '';
}
function buySummary(o) {
  const {shares, px, fx, acct, ccy, mkt, live, lim, stop, tgt, trail, fee, amount, ticker, lot, r2y} = o;
  const ac = esc(acct.ccy || ''), conv = v => fx && fx !== 1 ? ` <span class="muted">≈ ${money(v * fx, ac)}</span>` : '';
  const cost = shares && px ? shares * px : null;
  const loss = shares && stop && px ? (px - stop) * shares : null;
  const gain = shares && tgt && px ? (tgt - px) * shares : null;
  /* a purchase price above the market fills at once, at about the market */
  const fillsNow = !mkt && o.status == null && live != null && lim != null && live < lim;
  const head = shares
    ? `Buy <b>${fmt(shares, 1)} ${esc(ticker)}</b> ${mkt ? `now at about <b>${money(px, ccy)}</b>`
      : fillsNow ? `now at about <b>${money(live, ccy)}</b> <span class="muted">(your purchase price ${money(lim, ccy)} is above it)</span>`
      : `at up to <b>${money(lim, ccy)}</b> a share`}
       — <b>${money(cost, ccy)}</b>${fx && fx !== 1 ? ` (≈ ${money(cost * fx, ac)})` : ''} plus ${money(fee, ac)} fee,
       ${acct.cash ? `${fmt((cost || 0) * (fx || 1) / acct.cash * 100, 1)}% of the account's cash` : ''}`
    : `<span class="bad">${amount && px ? `Too small for one ${lot > 1 ? lot + '-share lot' : 'share'} — raise the amount.` : 'Enter an amount to spend or a number of shares.'}</span>`;
  const tile = (label, main, sub, cls) => `<div class="otile ${cls || ''}"><span>${label}</span><b>${main}</b>${sub ? `<small>${sub}</small>` : ''}</div>`;
  const tiles = `<div class="otiles">
    ${tile('Most you can lose', loss != null ? '−' + money(loss, ccy) : 'no stop-loss',
       loss != null ? `${fmt((px - stop) / px * 100, 1)}% · if it falls to ${money(stop, ccy)}${fx && fx !== 1 ? ` · ≈ −${money(loss * fx, ac)}` : ''}` : 'set a stop-loss', 'loss')}
    ${tile('You could make', gain != null ? '+' + money(gain, ccy) : '—',
       gain != null ? `${fmt((tgt - px) / px * 100, 1)}% · if it rises to ${money(tgt, ccy)}${fx && fx !== 1 ? ` · ≈ +${money(gain * fx, ac)}` : ''}` : 'no take-profit set', 'gain')}
    ${tile('Reward : risk', loss && gain ? `${fmt(gain / loss, 1)} : 1` : '—',
       loss && gain ? (gain / loss >= 2 ? 'good — gains twice the risk or more' : gain / loss >= 1 ? 'gain is larger than the risk' : 'risk is larger than the gain') : '')}
  </div>`;
  const buyAt = mkt ? px : lim, ts = trailStart(buyAt, trail);
  const tStop = trailIsStop(acct, o.exchange);
  const lad = ladder([
    {k: 'stop', v: stop, label: 'stop-loss'},
    ...(ts ? [{k: 'trail', v: ts, label: `${trailName(acct, o.exchange)} ${fmt(trail, 1)}%`}] : []),
    {k: 'entry', v: mkt ? px : lim, label: mkt ? 'you buy' : 'purchase price'},
    ...(!mkt && live ? [{k: 'now', v: live, label: 'now'}] : []),
    {k: 'target', v: tgt, label: 'take-profit'}], ccy,
    r2y ? [{v: r2y.high, label: '2-yr high', tip: '2-year high'}, {v: r2y.low, label: '2-yr low', tip: '2-year low'}] : [],
    {drag: o.drag});
  let status = o.status;
  if (status) {}
  else if (mkt) status = `<span class="ok">● Fills straight away</span> at the current price (sent at most 1% above it, so it cannot run away).`;
  else if (live && lim && live > lim) status = `<span class="warn">● Waiting:</span> ${esc(ticker)} is at ${money(live, ccy)} and must
    <b>fall ${fmt((live - lim) / live * 100, 1)}%</b> to ${money(lim, ccy)} before it buys. If it doesn't by the close, the order is cancelled.`;
  else if (live && lim) status = `<span class="ok">● Fills straight away</span> at about the current price
    (${money(live, ccy)}) — a purchase price above it does not make you pay more, the order buys at the best price on offer.
    The stop-loss, take-profit and shares are worked out from ${money(live, ccy)}.`;
  else status = '<span class="muted">● Waiting for a live price…</span>';
  return `<div class="osum-head">${o.lead ? o.lead + ' ' : ''}${head}</div>${tiles}${lad}${ranges(o.lv, r2y, live ?? px, stop, tgt, ccy)}
    <div class="osum-line">${status}</div>
    <div class="osum-line muted">On <b>${esc(acct.label)}</b> · ${trailLine(ts, trail, stop, buyAt, ccy, tStop)}</div>`;
}
/* what the trailing alert does for THIS order: it starts at the purchase
   price − trail %, and only adds anything once it has risen above the stop */
function trailLine(ts, trail, stop, buyAt, ccy, isStop = false) {
  if (isStop) return trailStopLine(ts, trail, stop, buyAt, ccy);
  if (!ts) return 'no trailing alert.';
  const base = `trailing alert <b>${fmt(trail, 1)}%</b>: starts at <b>${money(ts, ccy)}</b> and rises with the best price after you buy`;
  if (!stop) return base + '.';
  if (ts <= stop) {
    const rise = (stop / (1 - trail / 100)) / buyAt * 100 - 100;
    return base + ` — <span class="warn">it starts ${ts === stop ? 'at' : 'below'} your stop-loss (${money(stop, ccy)}),
      so it only warns you once the price has risen about ${fmt(rise, 1)}%</span>.`;
  }
  if ((ts - stop) / buyAt < 0.01)
    return base + ` — <span class="warn">only ${money(ts - stop, ccy)} above your stop-loss, so at first it
      fires about when the stop sells; a smaller % warns you earlier</span>.`;
  return base + ` — ${money(ts - stop, ccy)} above your stop-loss, so it warns you before the stop sells.`;
}


/* Claire, on Saxo: the trail % turns the stop-loss into a Saxo trailing
   stop. It is sent starting at the stop-loss, trailing by (trail % × the
   purchase price) — a fixed distance — so it sits at whichever is higher of
   the stop-loss and (best price since you bought − that distance). */
function trailStopLine(ts, trail, stop, buyAt, ccy) {
  if (!trail) return 'no trailing stop — the stop-loss stays fixed.';
  if (!stop) return `<span class="warn">a trailing stop needs a stop-loss to start from</span>.`;
  const d = buyAt ? buyAt * trail / 100 : null;
  const base = `<b>trailing stop ${fmt(trail, 1)}%</b> at Saxo: the stop-loss follows the best price since you buy
    ${d ? `at <b>${money(d, ccy)}</b> below it` : ''}, never moves back, and <b>sells automatically</b> when hit`;
  if (ts != null && ts <= stop) {
    const rise = d ? (stop + d) / buyAt * 100 - 100 : null;
    return base + ` — it stays at your stop-loss (${money(stop, ccy)}) until the price has risen
      ${rise != null ? `about ${fmt(rise, 1)}%` : 'enough'}, then climbs with it. The desk's alert stays as a backup.`;
  }
  return base + ` — it starts at about ${money(ts, ccy)}, above your stop-loss. The desk's alert stays as a backup.`;
}

/* a SELL's summary — an exit (selling shares the desk holds) or a short
   (selling shares it does not hold). Same shape as buySummary: headline,
   three tiles, the price line, the ranges and the fill status.
   o = {shares, px (where it fills), ccy, acct, mkt, live, lim, ticker,
        exit, held, avg (cost per share of the shares held), stop, tgt,
        trail, fx, r2y, lv, lead, drag} */
function sellSummary(o) {
  const {shares, px, ccy, acct = {}, mkt, live, lim, ticker, exit, held, avg, stop, tgt, fx, r2y} = o;
  const ac = esc(acct.ccy || '');
  const conv = v => fx && fx !== 1 && v != null ? ` (≈ ${money(v * fx, ac)})` : '';
  const proceeds = shares && px ? shares * px : null;
  /* a sale price BELOW the market sells at once, at about the market */
  const fillsNow = !mkt && live != null && lim != null && live > lim;
  const verb = exit ? 'Sell' : 'Short';
  const head = shares
    ? `${verb} <b>${fmt(shares, 4)} ${esc(ticker)}</b> ${mkt ? `now at about <b>${money(px, ccy)}</b>`
      : fillsNow ? `now at about <b>${money(live, ccy)}</b> <span class="muted">(your sale price ${money(lim, ccy)} is below it)</span>`
      : `at no less than <b>${money(lim, ccy)}</b> a share`}
       — ${exit ? 'you get' : 'you receive'} about <b>${money(proceeds, ccy)}</b>${conv(proceeds)} before fees${
       exit && held ? `, ${fmt(Math.min(shares, held) / held * 100, 0)}% of the ${fmt(held, 4)} shares the desk holds` : ''}`
    : '<span class="bad">Enter the number of shares to sell.</span>';
  const tile = (label, main, sub, cls) => `<div class="otile ${cls || ''}"><span>${label}</span><b>${main}</b>${sub ? `<small>${sub}</small>` : ''}</div>`;
  let tiles;
  if (exit) {
    const pl = shares && px && avg ? (px - avg) * shares : null;
    const left = held != null && shares ? Math.max(0, held - shares) : null;
    tiles = `<div class="otiles">
      ${tile('You get', proceeds != null ? money(proceeds, ccy) : '—', proceeds != null ? `before fees${conv(proceeds)}` : '', '')}
      ${tile(pl == null ? 'Profit / loss' : pl >= 0 ? 'Profit on these shares' : 'Loss on these shares',
         pl == null ? '—' : (pl >= 0 ? '+' : '−') + money(Math.abs(pl), ccy),
         pl == null ? (avg ? '' : 'what you paid is not known here')
           : `${fmt((px - avg) / avg * 100, 1)}% · you paid ${money(avg, ccy)} a share`, pl == null ? '' : pl >= 0 ? 'gain' : 'loss')}
      ${tile('Left after', left == null ? '—' : left > 1e-9 ? `${fmt(left, 4)} shares` : 'none',
         left == null ? '' : left > 1e-9 ? (px ? `worth about ${money(left * px, ccy)}` : '') : 'the whole position is sold')}
    </div>`;
  } else {
    const loss = shares && stop && px ? (stop - px) * shares : null;
    const gain = shares && tgt && px ? (px - tgt) * shares : null;
    tiles = `<div class="otiles">
      ${tile('Most you can lose', loss != null ? '−' + money(loss, ccy) : 'no stop-loss',
         loss != null ? `${fmt((stop - px) / px * 100, 1)}% · if it rises to ${money(stop, ccy)}${conv(loss)}` : 'a short needs a stop-loss above the price', 'loss')}
      ${tile('You could make', gain != null ? '+' + money(gain, ccy) : '—',
         gain != null ? `${fmt((px - tgt) / px * 100, 1)}% · if it falls to ${money(tgt, ccy)}${conv(gain)}` : 'no take-profit set', 'gain')}
      ${tile('Reward : risk', loss && gain ? `${fmt(gain / loss, 1)} : 1` : '—',
         loss && gain ? (gain / loss >= 2 ? 'good — gains twice the risk or more' : gain / loss >= 1 ? 'gain is larger than the risk' : 'risk is larger than the gain') : '')}
    </div>`;
  }
  const at = mkt ? px : lim;
  const pts = exit
    ? [...(avg ? [{k: 'paid', v: avg, label: 'you paid'}] : []),
       {k: 'entry', v: at, label: mkt ? 'you sell' : 'sale price'},
       ...(!mkt && live ? [{k: 'now', v: live, label: 'now'}] : [])]
    : [{k: 'target', v: tgt, label: 'take-profit'},
       {k: 'entry', v: at, label: mkt ? 'you short' : 'sale price'},
       ...(!mkt && live ? [{k: 'now', v: live, label: 'now'}] : []),
       {k: 'stop', v: stop, label: 'stop-loss'}];
  const zones = exit && avg && px ? [[avg, px, px >= avg ? 'gain' : 'loss']] : [];
  const lad = ladder(pts, ccy,
    r2y ? [{v: r2y.high, label: '2-yr high', tip: '2-year high'}, {v: r2y.low, label: '2-yr low', tip: '2-year low'}] : [],
    {drag: o.drag, zones});
  let status;
  if (mkt) status = `<span class="ok">● Sells straight away</span> at the current price (sent at most 1% below it, so it cannot run away).`;
  else if (live && lim && live < lim) status = `<span class="warn">● Waiting:</span> ${esc(ticker)} is at ${money(live, ccy)} and must
    <b>rise ${fmt((lim - live) / live * 100, 1)}%</b> to ${money(lim, ccy)} before it sells. If it doesn't by the close, the order is cancelled.`;
  else if (live && lim) status = `<span class="ok">● Sells straight away</span> at about the current price
    (${money(live, ccy)}) — a sale price below it does not get you less, the order sells at the best price on offer.`;
  else status = '<span class="muted">● Waiting for a live price…</span>';
  const foot = exit
    ? (String(acct.broker || '').startsWith('saxo')
      ? `At Saxo the desk first cancels this position's stop-loss / take-profit (Saxo will not take a sell while
         they rest), then sells; any shares left are protected again at the same prices.`
      : 'An exit: it sells shares the desk holds — no stop-loss or take-profit.')
    : `<span class="warn">Short sale</span> — the desk holds no ${esc(ticker)}, so selling borrows shares; the stop-loss
       buys them back if the price rises to it.`;
  return `<div class="osum-head">${o.lead ? o.lead + ' ' : ''}${head}</div>${tiles}${lad}${ranges(o.lv, r2y, live ?? px, null, null, ccy)}
    <div class="osum-line">${status}</div>
    <div class="osum-line muted">${acct.label ? `On <b>${esc(acct.label)}</b> · ` : ''}${foot}</div>`;
}
