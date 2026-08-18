/* FleetMem — shared shell behaviour for both the 2D and 3D views.
   Everything except the viewport itself lives here, so the two views cannot drift apart. */

export const State = { latest: null };

/* ---------- theme ---------------------------------------------------------- */
export function initTheme() {
  const saved = localStorage.getItem('fleetmem-theme');
  if (saved) document.documentElement.setAttribute('data-theme', saved);
  paintThemeButtons();
}
export function setTheme(mode) {
  if (mode === 'auto') { localStorage.removeItem('fleetmem-theme');
                         document.documentElement.removeAttribute('data-theme'); }
  else { localStorage.setItem('fleetmem-theme', mode);
         document.documentElement.setAttribute('data-theme', mode); }
  paintThemeButtons();
  window.dispatchEvent(new CustomEvent('fleetmem:theme'));
}
function paintThemeButtons() {
  const current = localStorage.getItem('fleetmem-theme') || 'auto';
  document.querySelectorAll('[data-theme-btn]').forEach(b =>
    b.classList.toggle('on', b.dataset.themeBtn === current));
}
export function isDark() {
  const set = document.documentElement.getAttribute('data-theme');
  if (set) return set === 'dark';
  return matchMedia('(prefers-color-scheme: dark)').matches;
}
export function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

/* ---------- transient status ---------------------------------------------- */
export function toast(message, ok = false) {
  const el = document.getElementById('toast');
  el.textContent = message;
  el.className = ok ? 'ok' : '';
  el.style.display = 'block';
  clearTimeout(el._t);
  el._t = setTimeout(() => (el.style.display = 'none'), ok ? 3400 : 8000);
}
function showConn(message) {
  const el = document.getElementById('conn');
  if (!el) return;
  if (message) { el.textContent = message; el.style.display = 'block'; }
  else el.style.display = 'none';
}

/* A request that fails must look like it failed. Returning null silently is how a broken
   button ends up looking merely idle. */
export async function call(url, options, label) {
  try {
    const res = await fetch(url, options);
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      const d = body.detail;
      toast(`${label} failed (HTTP ${res.status}). ` +
        (typeof d === 'object' && d
          ? `${d.message || ''} ${(d.errors || []).join('; ')}`
          : d || 'No detail returned.'), false);
      return null;
    }
    if (body.errors && body.errors.length) toast(`${label}: ${body.errors.join('; ')}`, false);
    return body;
  } catch (err) {
    toast(`${label} failed: ${err.message}`, false);
    return null;
  }
}

/* ---------- live connection ------------------------------------------------ */
/* A silently dead socket is indistinguishable from an idle fleet, so say so and reconnect. */
export function connect(onState) {
  let attempts = 0;
  (function open() {
    const proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
    const ws = new WebSocket(proto + location.host + '/ws');
    ws.onopen = () => { attempts = 0; showConn(null); };
    ws.onmessage = e => { showConn(null); State.latest = JSON.parse(e.data); onState(State.latest); };
    ws.onclose = () => {
      attempts++;
      showConn(`Lost connection to the fleet. Reconnecting (attempt ${attempts})…`);
      setTimeout(open, Math.min(1000 * attempts, 6000));
    };
    ws.onerror = () => ws.close();
  })();
}

/* ---------- actions -------------------------------------------------------- */
const asJson = body => ({ method: 'POST', headers: { 'content-type': 'application/json' },
                          body: JSON.stringify(body) });

export async function race() {
  const r = await call('/api/race', asJson({ resource: 'dock-3', robots: ['R1', 'R2'] }), 'Race');
  if (!r) return;
  const won = (r.results || []).find(x => x.granted);
  const lost = (r.results || []).find(x => !x.granted);
  toast(`${won ? won.robot : '?'} holds dock-3. ` +
        `${lost ? lost.robot : '?'} was refused by the database and re-routed.`, true);
}
export async function task(id) {
  const r = await call('/api/task', asJson({ robot_id: id, task: 'deliver pallet' }), `Task ${id}`);
  if (r) toast(r.granted ? `${id} claimed ${r.granted}.`
                         : `${id} found every candidate held.`, true);
}
export async function release() {
  if (await call('/api/reset', { method: 'POST' }, 'Release')) toast('All claims released.', true);
}
export async function reseed() {
  const r = await call('/api/reseed', { method: 'POST' }, 'Restore');
  if (r) { toast(`Fleet memory restored — ${r.lessons} lessons.`, true); recall(); }
}
export async function recall() {
  const q = document.getElementById('q').value;
  const r = await call('/api/recall?q=' + encodeURIComponent(q), undefined, 'Recall');
  if (!r) return;
  document.getElementById('hits').innerHTML = r.hits.length
    ? r.hits.map(h => `<div class="hit">
        <div class="d">${h.distance} · ${h.robot_id}</div>
        <div class="t">${escapeHtml(h.lesson)}</div>
        <div class="m">${h.location || 'no location'} · ${h.provider}</div></div>`).join('')
    : `<div class="empty">No lessons stored. The fleet's memory may have been reset.
       <br><button class="btn" style="margin-top:8px" onclick="FleetMem.reseed()">
       Restore baseline lessons</button></div>`;
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

/* ---------- shared rendering ---------------------------------------------- */
const LEASE_FULL = 90;   // seconds; matches FleetMemory.DEFAULT_LEASE_SECONDS

export function renderAnnunciator(state) {
  const ttl = state.claim_ttl || {};
  const claims = state.claims || {};
  const host = document.getElementById('annunciator');
  host.innerHTML = (state.docks || []).map(d => {
    const who = claims[d.id];
    const secs = ttl[d.id];
    const low = secs !== undefined && secs <= 20;
    const pct = secs !== undefined ? Math.max(0, Math.min(100, (secs / LEASE_FULL) * 100)) : 0;
    return `<div class="slot ${who ? 'held' : ''}">
      <div><div class="rid">${d.id}</div>
        <div class="who ${who ? '' : 'free'}">${who ? 'held by ' + who : 'available'}</div></div>
      <div class="lease ${low ? 'low' : ''}">${secs !== undefined ? 'lease ' + secs + 's' : ''}</div>
      ${who ? `<div class="bar"><i class="${low ? 'low' : ''}" style="width:${pct}%"></i></div>` : ''}
    </div>`;
  }).join('');
}

export function renderFeed(state) {
  document.getElementById('feed').innerHTML = (state.log || []).slice().reverse()
    .map(l => `<div class="k-${l.kind}">${String(l.t).padStart(4, '0')} · ${escapeHtml(l.text)}</div>`)
    .join('');
}

export async function renderLamps() {
  const h = await (await fetch('/healthz')).json().catch(() => ({}));
  const set = (id, label, live) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.className = 'lamp ' + (live ? 'live' : 'degraded');
    el.querySelector('b').textContent = label;
  };
  const target = h.config?.target ?? 'unknown';
  set('lamp-db', target, target === 'CockroachDB Cloud');
  const emb = h.embeddings_provider ?? 'unknown';
  set('lamp-emb', emb, emb.startsWith('bedrock'));
  const rea = h.reasoning_provider ?? 'unknown';
  set('lamp-rea', rea, rea.startsWith('bedrock'));
  const store = h.artifact_store?.enabled;
  set('lamp-s3', store ? h.artifact_store.bucket.replace(/^fleetmem-/, 's3 · ') : 'no bucket', !!store);
}

export function bindShell() {
  initTheme();
  document.querySelectorAll('[data-theme-btn]').forEach(b =>
    b.addEventListener('click', () => setTheme(b.dataset.themeBtn)));
  addEventListener('keydown', e => {
    if (e.target.tagName === 'INPUT') return;
    if (e.key === 'r' || e.key === 'R') race();
    if (e.key === 'x' || e.key === 'X') release();
  });
  window.FleetMem = { race, task, release, reseed, recall, setTheme };
}
