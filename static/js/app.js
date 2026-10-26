// Small shared helpers. No framework — keeps the premium front-end hand-built and fast.
const CSRF = document.querySelector('meta[name="csrf-token"]')?.content || '';

async function api(url, body, method = 'POST') {
  const res = await fetch(url, {
    method,
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': CSRF, 'X-Requested-With': 'fetch' },
    body: method === 'GET' ? undefined : JSON.stringify(body || {}),
  });
  let data;
  try { data = await res.json(); } catch { data = { ok: false, error: 'Unexpected server response.' }; }
  if (!res.ok && !data.error) data.error = 'Something went wrong. Please try again.';
  return data;
}

function toast(msg, kind = 'ok') {
  let box = document.getElementById('toastBox');
  if (!box) {
    box = document.createElement('div');
    box.id = 'toastBox';
    box.style.cssText = 'position:fixed;top:14px;right:14px;z-index:999;display:flex;flex-direction:column;gap:8px;max-width:320px';
    document.body.appendChild(box);
  }
  const t = document.createElement('div');
  t.textContent = msg;
  t.style.cssText = `background:${kind === 'ok' ? '#102414' : '#2a1210'};border-left:3px solid ${kind === 'ok' ? '#4caf50' : '#ff3325'};color:${kind === 'ok' ? '#bdf2c4' : '#ffb3a8'};padding:12px 16px;font-size:.85rem;font-weight:600;box-shadow:0 10px 30px -10px #000`;
  box.appendChild(t);
  setTimeout(() => { t.style.transition = 'opacity .4s'; t.style.opacity = '0'; setTimeout(() => t.remove(), 400); }, 3200);
}

// Generic admin row actions: data-action="/api/..." data-body='{"status":"x"}' data-confirm="..." data-prompt='[...]'
document.addEventListener('click', async (e) => {
  const btn = e.target.closest('[data-action]');
  if (!btn) return;
  e.preventDefault();
  const url = btn.dataset.action;
  let body = btn.dataset.body ? JSON.parse(btn.dataset.body) : {};
  if (btn.dataset.confirm && !confirm(btn.dataset.confirm)) return;
  if (btn.dataset.prompt) {
    const fields = JSON.parse(btn.dataset.prompt);
    for (const f of fields) {
      const v = prompt(f.label);
      if (v === null) return;
      body[f.name] = v;
    }
  }
  btn.disabled = true;
  const orig = btn.textContent;
  btn.textContent = '…';
  const data = await api(url, body);
  btn.disabled = false;
  btn.textContent = orig;
  if (data.ok) {
    toast(data.message || 'Done.', 'ok');
    setTimeout(() => location.reload(), 700);
  } else {
    toast(data.error || 'Something went wrong.', 'error');
  }
});

// Mark-all-read on bell click happens server side via link; nothing else needed here.

// Responsive tables: copy each column header onto its cells so rows can stack as cards on phones.
document.querySelectorAll('table.dt').forEach(t => {
  const head = [...t.querySelectorAll('tr')].find(r => r.querySelector('th'));
  if (!head) return;
  head.classList.add('dt-head');
  const labels = [...head.children].map(th => th.textContent.trim());
  t.querySelectorAll('tr').forEach(r => {
    if (r === head) return;
    [...r.children].forEach((td, i) => { if (td.tagName === 'TD' && !td.hasAttribute('colspan')) td.setAttribute('data-label', labels[i] || ''); });
  });
});

// Mobile "More" sheet
const sheet = document.getElementById('moreSheet');
if (sheet) {
  document.querySelectorAll('[data-sheet]').forEach(b => b.addEventListener('click', () => sheet.classList.add('open')));
  sheet.addEventListener('click', e => { if (e.target === sheet || e.target.closest('a')) sheet.classList.remove('open'); });
}
