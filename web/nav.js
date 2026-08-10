function nav(active) {
  const items = [["/", "Mission Control"], ["/claire", "Claire"],
    ["/trading", "Trading"], ["/portfolio", "Portfolio"],
    ["/markets", "Markets"], ["/agents", "Agents"],
    ["/providers", "Providers"]];
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
