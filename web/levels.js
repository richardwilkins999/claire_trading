/* Change the protection of a position you HOLD: its stop-loss, take-profit
   and trailing alert (Home → Today, and the Orders page).

   What may be changed is decided per broker on the server
   (app/capabilities.py) and arrives with each position as `can_change`;
   a field that may not be changed is shown locked, with the reason. The
   server checks again on Save — nothing here is trusted.

   - stop-loss / take-profit  → POST /api/position-levels: moves the orders
     resting for this position (every one of them, when it was bought in
     parts) to the new price. No approval card: it is your own instruction
     about orders that already exist, like Cancel on the Orders page.
   - trailing alert           → POST /api/position-floor: the desk's own
     alert, never an order.

   A page calls levelsRegister(p) for each position of /api/portfolio while
   rendering, puts <div id="lvlbox-KEY"></div> where the editor should
   open (KEY = levelsKey(p)), calls levelsRestore() after every re-render,
   and sets levelsSaved to its reload function. levelsOpen(key) fetches
   /api/portfolio itself when the position is not registered (Orders page).
   Needs nav.js and orderform.js. Every server string is escaped. */
const LVLPOS = {}, LVLS = {};
let levelsSaved = () => {};

function levelsKey(p) { return p.account_id + '|' + p.instrument_id; }
function levelsRegister(p) { LVLPOS[levelsKey(p)] = p; }
function levelsBusy() { return Object.keys(LVLS).length > 0; }

/* what may be changed on this position, each with the reason when not */
function levelsCan(p) {
  const c = p.can_change || {}, f = protectionFacts(p), where = c.venue_label || 'this broker';
  const part = (flag, leg, word) => !leg
    ? {ok: false, why: `No ${word} order is resting for this position, so there is nothing to move.`}
    : !flag ? {ok: false, why: `${where} does not let the desk change a ${word} once the trade is done.`}
    : {ok: true, why: `Moves the ${word} order${leg.all.length > 1 ? 's' : ''} resting at ${where}.`};
  const stop = f.trails
    ? {ok: false, why: 'This is a trailing stop held at Saxo: it follows the price by itself, and its distance cannot be changed from the desk.'}
    : part(c.stop_loss, f.sl, 'stop-loss');
  return {stop, target: part(c.take_profit, f.tp, 'take-profit'),
          trail: {ok: c.trailing_alert !== false, why: "The desk's own alert: it asks you before selling, it never sells."}};
}

/* the editor's value for a level dragged to `price` (Home's bar, Portfolio's
   price line): a price, or for the trailing alert the % that puts it there */
function levelsPreset(key, part, price) {
  const p = LVLPOS[key];
  if (part !== 'pct') return {[part]: String(r2(price, price))};
  const lv = (p && p.levels) || {}, ref = lv.alert_ref ?? lv.avg_cost_native;
  if (!ref) return {pct: null};
  const pct = (p.side === 'short' ? price / ref - 1 : 1 - price / ref) * 100;
  return {pct: String(Math.min(90, Math.max(0.5, Math.round(pct * 10) / 10)))};
}

async function levelsOpen(key, preset) {
  if (!LVLPOS[key]) {
    try {
      const d = await api('/api/portfolio');
      Object.values(d.exchanges || {}).flat().forEach(levelsRegister);
    } catch (e) { return toast('Could not load the position: ' + e.message, false); }
  }
  const p = LVLPOS[key];
  if (!p) return toast('The desk holds no such position any more.', false);
  const f = protectionFacts(p), lv = f.lv;
  /* an editor already open keeps what was typed; a preset (a dragged
     handle) overrides its own field only. A preset key with no value just
     says which field to put the cursor in. */
  const set = Object.fromEntries(Object.entries(preset || {}).filter(([, v]) => v != null && v !== ''));
  LVLS[key] = {stop: f.sl ? String(f.sl.price) : '', target: f.tp ? String(f.tp.price) : '',
               pct: lv.alert_pct != null ? String(lv.alert_pct) : '',
               ...(LVLS[key] || {}), ...set, msg: '', busy: false};
  levelsRender(key);
  const first = preset && Object.keys(preset)[0];
  const el = document.getElementById(`lv-${first || 'stop'}-${key}`);
  if (el && !el.disabled) { el.focus(); el.select(); }
}
function levelsClose(key) {
  delete LVLS[key];
  const el = document.getElementById('lvlbox-' + key);
  if (el) el.innerHTML = '';
  levelsSaved(false);                      /* let the page refresh again */
}
function levelsRestore() { Object.keys(LVLS).forEach(k => { if (LVLPOS[k]) levelsRender(k); }); }
function levelsTyped(key) {
  const s = LVLS[key];
  if (!s) return;
  for (const n of ['stop', 'target', 'pct']) {
    const el = document.getElementById(`lv-${n}-${key}`);
    if (el) s[n] = el.value;
  }
  const m = document.getElementById('lv-msg-' + key);
  if (m) m.innerHTML = levelsPreview(key);
}

/* what the typed values would do: {changes: {stop?, target?, pct?}, problems: [], lines: []} */
function levelsPlan(key) {
  const p = LVLPOS[key], s = LVLS[key], f = protectionFacts(p), can = levelsCan(p), lv = f.lv;
  const num = v => v === '' || v == null ? null : Number(v);
  const changes = {}, problems = [], lines = [];
  const px = v => money(v, ''), amt = v => (v >= 0 ? '+' : '−') + fmt(Math.abs(v), 0);
  const long = !f.short, cur = f.cur;
  const want = {stop: num(s.stop), target: num(s.target)};
  const now = {stop: f.sl && f.sl.price, target: f.tp && f.tp.price};
  for (const k of ['stop', 'target']) {
    const word = k === 'stop' ? 'stop-loss' : 'take-profit', leg = k === 'stop' ? f.sl : f.tp;
    if (!can[k].ok || want[k] == null || !leg || want[k] === now[k]) continue;
    if (!(want[k] > 0)) { problems.push(`The ${word} must be a price above zero.`); continue; }
    const below = (k === 'stop') === long;        /* this level sits below the price */
    if (cur != null && (below ? want[k] >= cur : want[k] <= cur))
      problems.push(`The ${word} must be ${below ? 'below' : 'above'} the price now (${px(cur)}) — otherwise it trades at once.`);
    changes[k] = want[k];
    const qty = leg.all.reduce((t, l) => t + l.qty, 0);
    const pl = v => f.avg == null ? null : (f.short ? f.avg - v : v - f.avg) * qty;
    lines.push(`${word[0].toUpperCase() + word.slice(1)} <b>${px(now[k])} → ${px(want[k])}</b> ${f.ccy}` +
      (pl(want[k]) == null ? '' : `: ${amt(pl(want[k]))} ${f.ccy} if it is hit (was ${amt(pl(now[k]))})`) +
      (leg.all.length > 1 ? ` · all ${leg.all.length} orders move` : ''));
  }
  const st = changes.stop ?? now.stop, tg = changes.target ?? now.target;
  if (st != null && tg != null && (changes.stop != null || changes.target != null) && (long ? st >= tg : st <= tg))
    problems.push(`The stop-loss must be ${long ? 'below' : 'above'} the take-profit.`);
  const pct = num(s.pct);
  if (can.trail.ok && pct != null && pct !== lv.alert_pct) {
    if (!(pct >= 0.5 && pct <= 90)) problems.push('The trailing alert must be between 0.5% and 90%.');
    else {
      changes.pct = pct;
      const ref = lv.alert_ref ?? lv.avg_cost_native;
      const at = ref == null ? null : ref * (1 + (f.short ? 1 : -1) * pct / 100);
      lines.push(`Trailing alert <b>${lv.alert_pct != null ? fmt(lv.alert_pct, 1) + '% → ' : ''}${fmt(pct, 1)}%</b>` +
        (at == null ? '' : `: alerts at ${px(at)} ${f.ccy}`) +
        (at != null && cur != null && (f.short ? cur >= at : cur <= at)
          ? ' <b class="bad">The price is already past it — it alerts (and starts a sell review) at once.</b>' : '') +
        (f.trails ? ' <span class="warn">This changes only the desk\'s backup alert — the trailing stop at Saxo keeps its distance.</span>' : ''));
    }
  }
  return {changes, problems, lines};
}
function levelsPreview(key) {
  const s = LVLS[key], plan = levelsPlan(key);
  if (s.msg) return s.msg;
  if (plan.problems.length) return plan.problems.map(x => `<div class="bad">${esc(x)}</div>`).join('');
  return plan.lines.length ? plan.lines.map(x => `<div>${x}</div>`).join('')
    : '<span class="muted">Type a new level, or drag a marker on the bar. Nothing changes until you press Save.</span>';
}
function levelsRender(key) {
  const el = document.getElementById('lvlbox-' + key);
  const p = LVLPOS[key], s = LVLS[key];
  if (!el || !p || !s) return;
  const can = levelsCan(p), f = protectionFacts(p), a = jsArg(key);
  const field = (n, label, unit, c) => `<label title="${esc(c.why)}"${c.ok ? '' : ' class="locked"'}>${label}
      <input id="lv-${n}-${esc(key)}" type="number" step="any" min="0" inputmode="decimal" value="${esc(s[n] ?? '')}"
        ${c.ok ? '' : 'disabled'} oninput="levelsTyped(${a})" onkeydown="if(event.key==='Enter')levelsSave(${a});if(event.key==='Escape')levelsClose(${a})"> ${unit}${
      c.ok ? '' : ' <span class="lock" aria-hidden="true">🔒</span>'}</label>`;
  const locked = [['stop-loss', can.stop], ['take-profit', can.target], ['trailing alert', can.trail]]
    .filter(([, c]) => !c.ok).map(([w, c]) => `<div class="muted">🔒 ${esc(w)}: ${esc(c.why)}</div>`).join('');
  el.innerHTML = `<div class="lvled" role="group" aria-label="Change the levels of ${esc(p.ticker)}">
    <div class="lvled-row"><b>${esc(p.ticker)}</b>
      ${field('stop', f.trails ? 'Trailing stop' : 'Stop-loss', f.ccy, can.stop)}
      ${field('target', 'Take-profit', f.ccy, can.target)}
      ${field('pct', 'Trailing alert', '%', can.trail)}
      <button onclick="levelsSave(${a})" ${s.busy ? 'disabled' : ''}>${s.busy ? 'Saving…' : 'Save'}</button>
      <button class="ghost" onclick="levelsClose(${a})">Cancel</button></div>
    <div id="lv-msg-${esc(key)}" class="lvled-msg" aria-live="polite">${levelsPreview(key)}</div>
    ${locked ? `<div class="lvled-msg">${locked}</div>` : ''}</div>`;
}
async function levelsSave(key) {
  const p = LVLPOS[key], s = LVLS[key];
  if (!p || !s || s.busy) return;
  s.msg = '';
  const plan = levelsPlan(key), c = plan.changes;
  if (plan.problems.length || !Object.keys(c).length) { levelsRender(key); return; }
  s.busy = true; levelsRender(key);
  const done = [];
  try {
    if (c.stop != null || c.target != null) {
      const body = {instrument_id: p.instrument_id, account_id: p.account_id};
      if (c.stop != null) body.stop = c.stop;
      if (c.target != null) body.target = c.target;
      const out = await api('/api/position-levels', body);
      for (const x of out.changed || [])
        if (!done.includes(x.leg)) done.push(x.leg);
      if ((out.failed || []).length)
        throw new Error(`${done.length ? 'Part of it was moved, but ' : ''}the broker refused: ${out.failed[0].error}`);
    }
    if (c.pct != null) {
      await api('/api/position-floor', {instrument_id: p.instrument_id, account_id: p.account_id,
                                        pct: c.pct, side: p.side === 'short' ? 'short' : 'long'});
      done.push('trail');
    }
  } catch (e) {
    s.busy = false;
    s.msg = `<div class="bad">${esc(e.message)}</div>`;
    levelsRender(key);
    if (done.length) levelsSaved(true);         /* show what did move */
    return;
  }
  const words = {stop: 'stop-loss', target: 'take-profit', trail: 'trailing alert'};
  toast(`${p.ticker}: ${done.map(d => words[d]).join(', ')} changed ✓`);
  delete LVLS[key];
  const el = document.getElementById('lvlbox-' + key);
  if (el) el.innerHTML = '';
  levelsSaved(true);
}
