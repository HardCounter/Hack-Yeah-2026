/* Shared state and helpers for every tab. Loaded first; the tab scripts use the App object. */
const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
const rand = (a, b) => a + Math.random() * (b - a);
const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
const sleep = ms => new Promise(r => setTimeout(r, reduced ? 0 : ms));
const fmtTime = d => d.toLocaleTimeString('en-GB', { timeZone: 'Europe/Warsaw' });
const fmtNum = n => n == null || !Number.isFinite(Number(n)) ? '—' : Number(n).toLocaleString('en-GB').replace(/,/g, ' ');  // space as thousands separator
/* Preset names are stored lowercase; show them capitalised, e.g. "Standard (edited)" */
const ms = v => v == null ? '—' : (v < 1 ? v.toFixed(2) : v < 10 ? v.toFixed(1) : Math.round(v)) + ' ms';
const label = name => name.replace(/^(lenient|standard|strict)/, w => w[0].toUpperCase() + w.slice(1));

/* The read API's Decision enum (docs/rest.md §5.1) and the word the console shows for it */
const VERDICT = { ALLOW: 'ALLOWED', BLOCK: 'BLOCKED', REDACT: 'REDACTED', REQUIRE_APPROVAL: 'HELD', ALERT: 'ALERT' };
/* Decision words used across the console, mapped to colour classes */
const TONE = { BLOCKED: 'block', REDACTED: 'warn', HELD: 'hold', ALLOWED: 'allow',
  ACTIVE: 'allow', BLOCK: 'block', REDACT: 'warn', ALERT: 'warn', FAIL: 'block', INCOMPLETE: 'warn', PASS: 'allow',
  LOW: 'allow', MEDIUM: 'warn', HIGH: 'block', CRITICAL: 'crit',
  'NOT VERIFIED': 'neutral', 'FAILED POSTCONDITIONS': 'block', 'VERIFICATION INCOMPLETE': 'warn', 'VERIFIED SUCCESS': 'allow',
  // control-plane plugin decisions (docs/rest.md §5.17), shown with spaces for underscores
  'NO CHANGE': 'allow', 'LEVEL RAISED': 'warn', 'VERDICT ALIGNED': 'allow', 'VERDICT ALERT': 'warn', 'VERDICT APPROVAL REQUIRED': 'hold',
  'REVIEW FAILED': 'block', 'PLUGIN FAILED': 'block', 'PLUGIN GAVE UP': 'block', DECIDED: 'allow', FAILED: 'warn', 'DEAD LETTERED': 'block' };
const badge = word => el('span', 'badge ' + (TONE[word] || 'neutral'), word);
const code = c => (c || '—').replace(/_/g, ' ');  // NO_CHANGE -> NO CHANGE

/* Risk scores and step attribution come from persisted backend decisions. */
const Risk = {
  levels: [['CRITICAL', 15], ['HIGH', 8], ['MEDIUM', 3], ['LOW', 0]],
  of(s) { return s.level || 'UNAVAILABLE'; },
};

const App = {
  config: null,        // the active config document
  configName: null,
  sessions: [],        // agent sessions (tasks) on the risk map
  listeners: {},
  on(evt, fn) { (this.listeners[evt] ||= []).push(fn); },
  emit(evt, ...data) { (this.listeners[evt] || []).forEach(fn => fn(...data)); },

  setActive(name, config, revision) {
    this.configName = name; this.config = config; this.configRevision = revision;
    $$('.activeName').forEach(n => n.textContent = label(name));
    this.emit('config', config);
  },

  _adminToken: '',
  adminToken() { return this._adminToken; },
  setAdminToken(token) { this._adminToken = String(token || '').trim(); },

  // JSON request. A failure throws the server's error message (docs/rest.md §2.2) with the HTTP status.
  async request(url, opts) {
    const target = new URL(url, location.href);
    const method = (opts?.method || 'GET').toUpperCase();
    if (target.origin === location.origin && method === 'PUT' &&
        (target.pathname.startsWith('/api/v1/configs/') || target.pathname === '/api/v1/config-selection')) {
      const headers = new Headers(opts?.headers);
      if (this.adminToken()) headers.set('X-Admin-Token', this.adminToken());
      opts = { ...opts, headers, redirect: 'error' };
    }
    const r = await fetch(url, opts);
    const body = await r.json().catch(() => ({}));
    if (!r.ok) {
      const fields = body.error?.details?.fields;
      throw Object.assign(new Error((body.error?.message || (typeof body.detail === 'string' ? body.detail : null) || `HTTP ${r.status}`) + (fields ? `: ${fields.join(', ')}` : '')), { status: r.status, code: body.error?.code, details: body.error?.details });
    }
    return body;
  },
  api(path, opts) { return this.request('/api/v1/configs' + path, opts); },
  // Read API for dashboards (docs/rest.md): history, trajectories and usage
  read(path) { return this.request('/api/v1' + path, { cache: 'no-store' }); },
  // The active config is the one selected on the server, the same for every browser. Called again
  // on a timer and on tab changes, so a selection made elsewhere shows up in the header and metrics.
  async syncActive() {
    const selected = (await this.api('')).find(c => c.selected);
    const revision = selected?.active_revision || selected?.revision;
    if (!selected || (selected.name === this.configName && revision === this.configRevision)) return;
    this.setActive(selected.name, await this.api('/' + encodeURIComponent(selected.name) + '?view=active'), revision);
  },
};

/* Tabs: hash routing so the back button and shared links work */
const TABS = ['config', 'metrics', 'tests'];  // PROMPT AGENT YOURSELF is a link to /opencode-wrapper/
function route() {
  const [first, ...rest] = location.hash.slice(1).split('/');  // e.g. #metrics/blocked/42
  const tab = TABS.includes(first) ? first : 'metrics';
  $$('[data-view]').forEach(s => s.hidden = s.id !== 'v-' + tab);
  $$('.tabs a').forEach(a => a.hash === '#' + tab ? a.setAttribute('aria-current', 'page') : a.removeAttribute('aria-current'));
  App.emit('tab', tab, rest.map(decodeURIComponent));
  if (App.config) App.syncActive().catch(() => {});
}
addEventListener('hashchange', route);

/* Check both services; the dashboard needs the web gateway and persisted evidence. */
let healthPending = false;
async function health() {
  if (healthPending) return;
  healthPending = true;
  const probe = async url => {
    const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 5000);
    try {
      const data = await App.request(url, { cache: 'no-store', signal: controller.signal });
      if (data.status !== 'ok') throw new Error('Service unhealthy');
      return data;
    } finally { clearTimeout(timeout); }
  };
  try {
    const [gateway, evidence] = await Promise.allSettled([probe('/healthz'), probe('/api/v1/health')]);
    const g = $('#gateway');
    if (gateway.status === 'fulfilled') {
      $('#build').textContent = gateway.value.commit;
      g.replaceChildren(el('span', 'dot ' + (evidence.status === 'fulfilled' ? 'ok' : 'down')),
        evidence.status === 'fulfilled' ? 'Gateway online' : 'Degraded · evidence unavailable');
    } else {
      g.replaceChildren(el('span', 'dot down'), 'Gateway unreachable');
    }
  } finally { healthPending = false; }
}

/* Start after every tab script has registered (scripts are deferred, so DOMContentLoaded fires after them) */
document.addEventListener('DOMContentLoaded', async () => {
  route();
  health(); setInterval(health, 30000);
  await App.syncActive().catch(() => {});
  setInterval(() => App.syncActive().catch(() => {}), 10000);
});
