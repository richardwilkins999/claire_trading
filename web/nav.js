function nav(active) {
  const items = [["/", "Mission Control"], ["/trading", "Trading"],
    ["/portfolio", "Portfolio"], ["/markets", "Markets"],
    ["/agents", "Agents"], ["/providers", "Providers"]];
  document.write('<nav>' + items.map(([href, label]) =>
    `<a href="${href}" class="${href === active ? 'active' : ''}">${label}</a>`
  ).join('') + '</nav><div id="msg"></div>');
}
async function api(path, opts) {
  const r = await fetch(path, opts ? {method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(opts)} : undefined);
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
function ts(t) { return t ? new Date(t * 1000).toLocaleString() : '—'; }
