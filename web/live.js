// Live (real-money) trading switch, spending caps and usage — DESIGN.md §22.
// Shown in the Orders page's side panel. Wording rule: a "cap" is always
// money (account currency) or a count of orders, never shares; "limit price"
// is always a per-share price. Turning OFF works from any paired device; turning ON
// only from a browser on the desk's own PC, with the typed phrase — the
// server enforces both, this file only shows what is possible.

let LIVE = null;

function liveWhen(t) {
  if (!t) return "—";
  return new Date(t * 1000).toLocaleString([], {day: "numeric", month: "short",
    hour: "2-digit", minute: "2-digit"});
}
function livePx(n) { return n == null ? "—" : fmt(n, Math.abs(n) < 10 ? 3 : 2); }

async function liveLoad() {
  LIVE = await api('/api/live-status');
  return LIVE;
}

function liveCcy() {
  const a = (LIVE && LIVE.accounts || [])[0];
  return a ? a.base_currency : '';
}

function liveStateLine(st) {
  if (st.armed) return '<b class="bad">⚠ ARMED — real-money orders can be placed</b>';
  if (!st.enabled_in_env) return '<b>OFF</b> <span class="muted">· not enabled on this PC</span>';
  if (!st.accounts.length) return '<b>OFF</b> <span class="muted">· no live account set up</span>';
  return '<b class="ok">OFF</b> <span class="muted">· disarmed</span>';
}

// The switch: a big on/off control plus why ON may be unavailable.
function liveSwitchHtml(st) {
  const on = st.armed;
  const btn = on
    ? `<button class="reject" onclick="liveDisarm()">Turn OFF live trading</button>`
    : `<button ${st.can_arm_here ? '' : 'disabled'} onclick="liveArm()"
         title="${esc(st.arm_blocked || '')}">Turn ON live trading</button>`;
  const why = !on && st.arm_blocked
    ? `<div class="muted small" style="margin-top:.3rem">Can't turn on here: ${esc(st.arm_blocked)}</div>` : '';
  const last = !on && st.disarm_reason
    ? `<div class="muted small">last turned off ${liveWhen(st.disarmed_at)} — ${esc(st.disarm_reason)}</div>` : '';
  const armedAt = on ? `<div class="muted small">on since ${liveWhen(st.armed_at)} by ${esc(st.armed_by || '?')}</div>` : '';
  return `<div class="card live-switch" style="border:2px solid ${on ? 'var(--bad)' : 'var(--line)'};
      display:flex;gap:1rem;align-items:center;flex-wrap:wrap;justify-content:space-between">
    <div><div>Live trading (Saxo, real money): ${liveStateLine(st)}</div>
      ${armedAt}${last}${why}
      <div class="muted small">Even when ON, every live trade still needs your approval with LIVE typed, and every order has a limit price (no market orders).</div>
    </div>
    <div>${btn}</div></div>`;
}

async function liveArm() {
  const st = LIVE || {};
  const typed = await ask(
    `This turns ON real-money trading on your Saxo account.\n\n` +
    `Spending caps (buys only): ${liveCapsLine(st.limits || {})}.\n` +
    `Each trade still needs your own approval, typing LIVE.`,
    {title: 'Turn on live trading?', live: true, type: st.arm_phrase,
     ok: 'Turn live trading ON', cancel: 'Leave it off'});
  if (typed === null) return;
  try {
    LIVE = await api('/api/live-arm', {phrase: typed});
    toast('live trading is ON');
  } catch (e) { toast(e.message, false); }
  liveRefresh();
}

async function liveDisarm() {
  if (!await ask('New live orders stop at once. Orders already at Saxo keep their stop-loss / take-profit.',
      {title: 'Turn OFF live trading?', ok: 'Turn it off', cancel: 'Leave it on'})) return;
  try {
    LIVE = await api('/api/live-disarm', {reason: 'turned off from the dashboard'});
    toast('live trading is OFF');
  } catch (e) { toast(e.message, false); }
  liveRefresh();
}

// pages define window.onLiveChange to re-render after a change
function liveRefresh() { if (window.onLiveChange) window.onLiveChange(); }

// ── spending caps: what each one IS, with its unit ──────────────────────
// money caps are in the live account's currency; the order cap is a count
function liveCaps() {
  const ccy = liveCcy() || 'account currency';
  return {
    max_order_base: {label: 'Max per buy', unit: ccy, money: true,
      hint: `money: the most one live buy may cost (shares × limit price), in ${ccy}`},
    max_orders_per_day: {label: 'Max buys per day', unit: 'buys', money: false,
      hint: 'a count of buy orders (not shares), per Singapore day'},
    max_daily_notional_base: {label: 'Max spent per day', unit: ccy, money: true,
      hint: `money: the total cost of all live buys today, in ${ccy}`},
  };
}
function liveCapValue(k, v) {
  const c = liveCaps()[k];
  return c && c.money ? `${fmt(v)} ${esc(c.unit)}` : `${esc(v)} ${c ? c.unit : ''}`;
}
function liveCapsLine(lim) {
  const ccy = liveCcy();
  return `max ${fmt(lim.max_order_base)} ${ccy} per buy · max ${lim.max_orders_per_day} buys a day · ` +
         `max ${fmt(lim.max_daily_notional_base)} ${ccy} of buys a day`;
}

function liveMetricsHtml(st) {
  const m = st.metrics || {}, lim = st.limits || {}, caps = liveCaps();
  const accts = Object.entries(m.accounts || {});
  const usage = accts.length ? accts.map(([id, a]) => {
    const cur = esc(a.currency);
    const pctN = lim.max_daily_notional_base ? Math.min(100, 100 * a.notional_today / lim.max_daily_notional_base) : 0;
    return `<div class="statstrip">
      <div class="stat"><span>account</span>${esc(id)} (${cur})</div>
      <div class="stat"><span>buys today</span>${a.orders_today} of max ${esc(lim.max_orders_per_day)} · ${a.orders_left} left</div>
      <div class="stat"><span>spent today</span>${fmt(a.notional_today)} of max ${fmt(lim.max_daily_notional_base)} ${cur} · ${fmt(a.notional_left)} ${cur} left</div>
      <div class="stat"><span>sells today</span>${a.exits_today} <span class="muted" style="text-transform:none;letter-spacing:0">(never capped)</span></div>
      <div class="stat"><span>all time</span>${a.entries_all_time} buys · ${fmt(a.notional_all_time)} ${cur}</div>
    </div>
    <div style="height:6px;background:var(--line);border-radius:3px;margin:-.4rem 0 .8rem">
      <div style="height:6px;width:${pctN}%;background:${pctN >= 90 ? 'var(--bad)' : 'var(--warn)'};border-radius:3px"></div></div>`;
  }).join('') : '<p class="muted small">No live account yet, so nothing to track. Usage appears here after <code>bin/claire live setup</code>.</p>';

  const capsTable = `<table class="ot"><tr><th>Cap</th><th>Now</th><th>New value</th></tr>
    ${Object.entries(caps).map(([k, c]) => `<tr>
      <td>${c.label}<div class="muted small">${esc(c.hint)}</div></td>
      <td>${liveCapValue(k, lim[k])}</td>
      <td><input id="lim-${k}" type="number" min="${c.money ? 0.01 : 1}"
        step="${c.money ? 'any' : 1}" value="${esc(lim[k])}" style="width:7rem">
        <span class="muted small">${esc(c.unit)}</span></td></tr>`).join('')}
    </table>
    <button onclick="liveSaveLimits()" style="margin-top:.4rem">Save caps</button>
    <div class="muted small" style="margin-top:.3rem">Caps apply to <b>buys only</b>, from the next live buy. Selling shares the desk bought is never capped.</div>`;

  const orders = (m.orders || []).length ? `<table class="ot"><tr><th>When</th><th>Side</th><th>Instrument</th>
      <th class="num">Shares</th><th class="num">Limit price<br><span class="muted small">per share</span></th><th>Status</th></tr>
      ${m.orders.map(o => `<tr><td class="small">${liveWhen(o.placed_at || o.created_at)}</td>
        <td>${esc(o.side)}</td><td>${esc(o.instrument_id)}</td><td class="num">${fmt(o.qty, 0)}</td>
        <td class="num">${livePx(o.limit_price)}</td><td>${esc(o.status)}</td></tr>`).join('')}</table>`
    : '<p class="muted small">No live orders yet.</p>';

  const EVENT = {armed: 'turned ON', disarmed: 'turned OFF', limits: 'caps changed'};
  const events = (m.events || []).length ? `<table class="ot"><tr><th>When</th><th>What</th><th>By</th><th>Detail</th></tr>
      ${m.events.map(e => { let p = {}; try { p = JSON.parse(e.payload || '{}'); } catch (_) {}
        const detail = e.event === 'limits'
          ? Object.keys(p.after || {}).filter(k => (p.before || {})[k] !== p.after[k])
              .map(k => `${(caps[k] || {label: k}).label}: ${liveCapValue(k, p.before[k])} → ${liveCapValue(k, p.after[k])}`).join(' · ')
          : esc(p.reason || '');
        return `<tr><td class="small">${liveWhen(e.ts)}</td><td>${esc(EVENT[e.event] || e.event)}</td>
          <td class="small">${esc(e.actor)}</td><td class="small">${detail}</td></tr>`; }).join('')}</table>`
    : '<p class="muted small">Live trading has never been turned on or off, and the caps have never been changed.</p>';

  return `${liveSwitchHtml(st)}
    <h3 style="margin-top:.8rem">Today</h3>${usage}
    <h3>Spending caps</h3>${capsTable}
    <h3 style="margin-top:1rem">Live orders</h3>${orders}
    <h3 style="margin-top:1rem">History</h3>${events}`;
}

async function liveSaveLimits() {
  const lim = (LIVE && LIVE.limits) || {}, caps = liveCaps(), changed = {};
  for (const k of Object.keys(caps)) {
    const v = Number(document.getElementById('lim-' + k).value);
    if (!(v > 0)) { toast(`${caps[k].label} must be more than 0`, false); return; }
    if (!caps[k].money && !Number.isInteger(v)) { toast(`${caps[k].label} must be a whole number of buys`, false); return; }
    if (v !== Number(lim[k])) changed[k] = v;
  }
  if (!Object.keys(changed).length) { toast('no changes'); return; }
  try {
    LIVE = await api('/api/live-limits', {limits: changed});
    toast('spending caps saved ✓');
  } catch (e) { toast(e.message, false); }
  liveRefresh();
}
