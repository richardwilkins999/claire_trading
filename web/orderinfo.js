/* What has to happen for an outstanding order to be sent / to fill, in plain
   words, with today's price beside it. Shared by the Orders and Trading pages.
   Needs nav.js (esc, fmt). Fields come from /api/orders or a run's story:
   side, qty, limit_price, stop_loss, take_profit, status, submitted_at, last,
   is_open, next_open (epoch s), filled, expires_at, account, exchange, leg,
   entry_price. */
function oiPx(n) { return n == null ? '—' : fmt(n, Math.abs(n) < 10 ? 3 : 2); }
/* "in 2 h 10 m (16:00 SGT)" — nav.js fmtUntil: your own time, its zone,
   and how long until it */
function oiWhen(t) { return t ? fmtUntil(t) : '?'; }
/* how far the price must move to reach `to`: "needs to fall 3.2% (5.80)" */
function oiMove(last, to) {
  if (last == null || !to) return '';
  const pct = (to - last) / last * 100, diff = Math.abs(to - last);
  if (Math.abs(pct) < 0.05) return 'it is right at that price now';
  return `it needs to ${pct < 0 ? 'fall' : 'rise'} <b>${fmt(Math.abs(pct), 1)}%</b> (${oiPx(diff)}) to get there`;
}
function orderNeeds(o) {
  const buy = o.side === 'buy', L = o.limit_price, last = o.last;
  const now = last != null ? `The current price is <b>${oiPx(last)}</b>` : 'No current price available';
  const closed = o.is_open === false
    ? ` ${esc(o.exchange || 'The market')} is closed — nothing can fill until it opens, ${esc(oiWhen(o.next_open))}.` : '';
  const parts = [];
  if (o.leg) {
    const stop = o.leg === 'stop';
    const at = stop ? (o.stop_loss ?? o.limit_price) : (o.limit_price ?? o.take_profit);
    const sell = o.side === 'sell';
    if (stop) parts.push(`<b>What it does:</b> if the price ${sell ? 'falls to' : 'rises to'} <b>${oiPx(at)}</b>
      or ${sell ? 'below' : 'above'}, the broker ${sell ? 'sells' : 'buys back'} your ${fmt(o.qty, 0)} shares
      straight away at the market price, to stop the loss growing. It can fill a little
      ${sell ? 'below' : 'above'} ${oiPx(at)} in a fast market.`);
    else parts.push(`<b>What it does:</b> if the price ${sell ? 'rises to' : 'falls to'} <b>${oiPx(at)}</b>
      or ${sell ? 'above' : 'below'}, the broker ${sell ? 'sells' : 'buys back'} your ${fmt(o.qty, 0)} shares
      at ${oiPx(at)} or better, to lock in the profit.`);
    parts.push(`${now}; ${oiMove(last, at) || 'distance unknown'}.`);
    if (o.entry_price) {
      const pl = (sell ? at - o.entry_price : o.entry_price - at) * o.qty;
      parts.push(`If it fills: <b class="${pl >= 0 ? 'ok' : 'bad'}">${pl >= 0 ? '+' : ''}${fmt(pl, 2)}</b>
        vs the ${oiPx(o.entry_price)} you paid (before fees).`);
    }
    parts.push('<span class="muted">The stop-loss and the take-profit are a pair: when one fills, the broker cancels the other.</span>');
    return parts.join(' ') + closed;
  }
  const verb = buy ? 'buy' : 'sell';
  const fillRule = buy
    ? `A buy limit fills only at <b>${oiPx(L)}</b> or lower`
    : `A sell limit fills only at <b>${oiPx(L)}</b> or higher`;
  const reached = last != null && L && (buy ? last <= L : last >= L);
  if (o.status === 'pending_session' && !o.submitted_at) {
    parts.push(`<b>Waiting for the market to open.</b> Claire is holding the order and sends it to
      ${esc(o.account || 'the broker')} when ${esc(o.exchange || 'the market')} opens, ${esc(oiWhen(o.next_open))}.`);
    parts.push(`${fillRule}. ${now}${reached
      ? ` — already ${buy ? 'at or below' : 'at or above'} your limit price, so it should fill soon after the open (if the price is still there).`
      : `; ${oiMove(last, L)}.`}`);
    parts.push('<span class="muted">You can still edit or cancel it — nothing has been sent yet.</span>');
    return parts.join(' ');
  }
  if (o.status === 'pending_session')
    return '<b>Being sent to the broker right now.</b> ' + now + '.';
  const left = o.filled ? o.qty - o.filled : o.qty;
  parts.push(o.status === 'partially_filled'
    ? `<b>Part filled:</b> ${fmt(o.filled, 0)} of ${fmt(o.qty, 0)} shares done; the other ${fmt(left, 0)} are still working at ${esc(o.account || 'the broker')}.`
    : `<b>Working at ${esc(o.account || 'the broker')}</b> — waiting for a ${buy ? 'seller' : 'buyer'} at your price.`);
  if (reached) parts.push(`${fillRule}. ${now} — already ${buy ? 'at or below' : 'at or above'} your limit price, so it should fill
    as soon as there are enough shares on offer at that price.${o.is_open === false ? '' : ' If it stays unfilled, check it on the broker\'s platform.'}`);
  else parts.push(`${fillRule}. ${now}; ${oiMove(last, L)} before it can ${verb}.`);
  if (o.expires_at) parts.push(`<span class="muted">If it has not filled when it expires, ${esc(oiWhen(o.expires_at))}, it is cancelled.</span>`);
  return parts.join(' ') + closed;
}
