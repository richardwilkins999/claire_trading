/* Self-contained SVG candlestick chart with SMA / volume / RSI overlays and a
   crosshair — no external libraries (design rule: no CDN at runtime). */
function sma(values, n) {
  const out = new Array(values.length).fill(null);
  let sum = 0, count = 0;
  for (let i = 0; i < values.length; i++) {
    const v = values[i];
    if (v == null) { out[i] = null; continue; }
    sum += v; count++;
    if (count > n) { sum -= values[i - n]; count = n; }
    out[i] = count === n ? sum / n : null;
  }
  return out;
}
function rsi(closes, n = 14) {
  const out = new Array(closes.length).fill(null);
  let up = 0, down = 0;
  for (let i = 1; i < closes.length; i++) {
    if (closes[i] == null || closes[i - 1] == null) continue;
    const d = closes[i] - closes[i - 1];
    const u = Math.max(d, 0), v = Math.max(-d, 0);
    if (i <= n) { up += u / n; down += v / n; }
    else { up = (up * (n - 1) + u) / n; down = (down * (n - 1) + v) / n; }
    if (i >= n) out[i] = down === 0 ? 100 : 100 - 100 / (1 + up / down);
  }
  return out;
}

function drawChart(el, data, opts) {
  opts = opts || {};
  const W = el.clientWidth || 900, H = opts.height || 420;
  const rsiH = opts.rsi ? 80 : 0, volH = opts.volume ? 60 : 0;
  const padL = 56, padR = 12, padT = 10;
  const priceH = H - rsiH - volH - padT - 24;
  const ts_ = data.timestamps, o = data.open, h = data.high, l = data.low,
    c = data.close, v = data.volume;
  const idx = [];
  for (let i = 0; i < c.length; i++) if (c[i] != null) idx.push(i);
  if (!idx.length) { el.innerHTML = '<p class="muted">no data</p>'; return; }
  const lo = Math.min(...idx.map(i => l[i] ?? c[i]));
  const hi = Math.max(...idx.map(i => h[i] ?? c[i]));
  const maxV = Math.max(1, ...idx.map(i => v[i] || 0));
  const n = idx.length;
  const cw = Math.max(2, Math.min(14, (W - padL - padR) / n - 2));
  const x = k => padL + (W - padL - padR) * (k + 0.5) / n;
  const y = p => padT + priceH * (1 - (p - lo) / (hi - lo || 1));
  const yv = q => padT + priceH + 8 + volH * (1 - q / maxV);
  const yr = q => padT + priceH + volH + 20 + rsiH * (1 - q / 100);
  let s = `<svg viewBox="0 0 ${W} ${H}" style="width:100%;background:var(--panel);border:1px solid var(--line);border-radius:10px">`;
  for (let g = 0; g <= 4; g++) {
    const p = lo + (hi - lo) * g / 4, yy = y(p);
    s += `<line x1="${padL}" y1="${yy}" x2="${W - padR}" y2="${yy}"
      stroke="var(--line)" stroke-dasharray="3 4"/>
      <text x="4" y="${yy + 4}" fill="var(--dim)" font-size="11">${p.toFixed(p < 10 ? 3 : 2)}</text>`;
  }
  idx.forEach((i, k) => {
    const up = (c[i] ?? 0) >= (o[i] ?? c[i]);
    const col = up ? 'var(--good)' : 'var(--bad)';
    if (opts.volume && v[i])
      s += `<rect x="${x(k) - cw / 2}" y="${yv(v[i])}" width="${cw}"
        height="${padT + priceH + 8 + volH - yv(v[i])}" fill="${col}" opacity=".35"/>`;
    if (h[i] != null && l[i] != null)
      s += `<line x1="${x(k)}" y1="${y(h[i])}" x2="${x(k)}" y2="${y(l[i])}" stroke="${col}"/>`;
    if (o[i] != null && c[i] != null) {
      const y1 = y(Math.max(o[i], c[i])), y2 = y(Math.min(o[i], c[i]));
      s += `<rect x="${x(k) - cw / 2}" y="${y1}" width="${cw}"
        height="${Math.max(1, y2 - y1)}" fill="${col}"/>`;
    }
  });
  const line = (vals, color, yfn) => {
    let d = '', pen = false;
    idx.forEach((i, k) => {
      if (vals[i] == null) { pen = false; return; }
      d += (pen ? 'L' : 'M') + x(k).toFixed(1) + ' ' + yfn(vals[i]).toFixed(1) + ' ';
      pen = true;
    });
    return `<path d="${d}" fill="none" stroke="${color}" stroke-width="1.6"/>`;
  };
  if (opts.sma20) s += line(sma(c, 20), '#e0b050', y);
  if (opts.sma50) s += line(sma(c, 50), '#4da3ff', y);

  /* your own levels: average cost, thesis stop/target, watcher alert */
  (opts.levels || []).forEach(lv => {
    if (lv.value == null || lv.value < lo || lv.value > hi) return;
    const yy = y(lv.value);
    s += `<line x1="${padL}" y1="${yy}" x2="${W - padR}" y2="${yy}"
      stroke="${lv.color}" stroke-width="1.2"
      stroke-dasharray="${lv.dash || '6 4'}"/>
      <text x="${padL + 4}" y="${yy - 3}" fill="${lv.color}"
        font-size="9.5">${lv.label}</text>`;
  });

  /* your fills: where you actually bought or sold */
  (opts.fills || []).forEach(f => {
    if (f.price == null || !ts_ || !ts_.length) return;
    let k = 0, best = Infinity;
    idx.forEach((i, kk) => {
      const d = Math.abs((ts_[i] || 0) - f.ts);
      if (d < best) { best = d; k = kk; }
    });
    if (f.price < lo || f.price > hi) return;
    const cx = x(k), cy = y(f.price);
    const buy = f.side === 'buy';
    const col = buy ? 'var(--good)' : 'var(--bad)';
    s += buy
      ? `<path d="M ${cx} ${cy - 7} L ${cx + 6} ${cy + 4} L ${cx - 6} ${cy + 4} Z"
          fill="${col}" stroke="#06090f" stroke-width="1"><title>bought ${
          f.qty} @ ${f.price}</title></path>`
      : `<path d="M ${cx} ${cy + 7} L ${cx + 6} ${cy - 4} L ${cx - 6} ${cy - 4} Z"
          fill="${col}" stroke="#06090f" stroke-width="1"><title>sold ${
          f.qty} @ ${f.price}</title></path>`;
  });
  if (opts.rsi) {
    const r = rsi(c);
    [30, 70].forEach(g => s += `<line x1="${padL}" y1="${yr(g)}"
      x2="${W - padR}" y2="${yr(g)}" stroke="var(--line)" stroke-dasharray="2 4"/>
      <text x="4" y="${yr(g) + 4}" fill="var(--dim)" font-size="10">${g}</text>`);
    s += line(r, '#b077e0', yr);
  }
  s += `<line id="ch-x" y1="${padT}" y2="${padT + priceH}" stroke="var(--dim)"
    stroke-dasharray="2 3" visibility="hidden"/>
    <text id="ch-t" fill="var(--text)" font-size="11" visibility="hidden"></text>
  </svg><div class="muted" id="ch-info" style="font-size:.8rem;min-height:1.2em"></div>`;
  el.innerHTML = s;
  const svg = el.querySelector('svg');
  const cx = el.querySelector('#ch-x'), info = el.querySelector('#ch-info');
  svg.addEventListener('mousemove', ev => {
    const r = svg.getBoundingClientRect();
    const px = (ev.clientX - r.left) * W / r.width;
    let k = Math.round((px - padL) / (W - padL - padR) * n - 0.5);
    k = Math.max(0, Math.min(n - 1, k));
    const i = idx[k];
    cx.setAttribute('x1', x(k)); cx.setAttribute('x2', x(k));
    cx.setAttribute('visibility', 'visible');
    const d = ts_ && ts_[i] ? new Date(ts_[i] * 1000).toLocaleDateString() : '';
    info.textContent = `${d}  O ${fmt(o[i], 3)}  H ${fmt(h[i], 3)}  ` +
      `L ${fmt(l[i], 3)}  C ${fmt(c[i], 3)}  Vol ${fmtBig(v[i])}`;
  });
  svg.addEventListener('mouseleave', () => {
    cx.setAttribute('visibility', 'hidden'); info.textContent = '';
  });
}
