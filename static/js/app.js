/* Shared state and helpers for every tab. Loaded first; the tab scripts use the App object. */
const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
const rand = (a, b) => a + Math.random() * (b - a);
const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
const sleep = ms => new Promise(r => setTimeout(r, reduced ? 0 : ms));
const fmtTime = d => d.toLocaleTimeString('en-GB', { timeZone: 'Europe/Warsaw' });
const fmtNum = n => Number(n).toLocaleString('en-GB').replace(/,/g, ' ');  // space as thousands separator
/* Preset names are stored lowercase; show them capitalised, e.g. "Standard (edited)" */
const label = name => name.replace(/^(lenient|standard|strict)/, w => w[0].toUpperCase() + w.slice(1));

/* Decision words used across the console, mapped to colour classes */
const TONE = { BLOCKED: 'block', REDACTED: 'warn', HELD: 'hold', ALLOWED: 'allow',
  ACTIVE: 'allow', BLOCK: 'block', REDACT: 'warn', ALERT: 'warn', FAIL: 'block', INCOMPLETE: 'warn', PASS: 'allow',
  LOW: 'allow', MEDIUM: 'warn', HIGH: 'block', CRITICAL: 'crit',
  'FAILED POSTCONDITIONS': 'block', 'VERIFICATION INCOMPLETE': 'warn', 'VERIFIED SUCCESS': 'allow' };
const badge = word => el('span', 'badge ' + (TONE[word] || 'neutral'), word);

/* Risk = likelihood x impact, the same model as the backend's trajectory-risk plugin
   (consume_plane/plugins/trajectory_risk.py, docs/trajectory-risk-model.md). Keep the numbers in sync. */
const Risk = {
  base: 0.02,
  weights: { gateway_blocked: .15, gateway_redacted: .05, gateway_alert: .10, tool_error: .05, out_of_contract_tool: .30,
    out_of_scope_target: .25, repeated_side_effect: .40, repeated_read: .10, missing_prerequisite: .50,
    untrusted_external_content: .10, budget_pressure: .10 },
  tools: { read_application: 1, read_documents: 1, extract_fields: 1, check_registry: 4, screen_sanctions: 4, compute_risk: 1,
    fetch_url: 4, request_more_docs: 2, escalate_edd: 2, reject_application: 4, send_email: 6, load_risk_model: 7,
    create_client: 8, run_code: 9, delete_client: 10 },
  sideEffect: { read: 1, write: 5, irreversible: 10 },  // consequence of a tool missing from `tools`
  prerequisites: { create_client: ['screen_sanctions'] },
  levels: [['CRITICAL', 15], ['HIGH', 8], ['MEDIUM', 3], ['LOW', 0]],
  // Signals of each real tool call, as RiskModel.assess derives them. steps: [{ name, side_effect, status,
  // decision, executed, args }] in order; contract: { target_ids, allowed_tools, budget } from the read API.
  // A signal that only raises later steps (untrusted content) is reported on the next step.
  signalsFor(steps, contract = {}) {
    const targets = contract.target_ids || [], allowed = contract.allowed_tools || [], cap = contract.budget?.tool_calls;
    const calls = {}, done = new Set();
    let later = [], budgetFlagged = false;
    return steps.map((st, i) => {
      const own = later; later = [];
      own.push(...{ blocked: ['gateway_blocked'], redacted: ['gateway_redacted'], failed: ['tool_error'] }[st.status] || []);
      if (st.decision === 'ALERT') own.push('gateway_alert');
      if (allowed.length && !allowed.includes(st.name)) own.push('out_of_contract_tool');
      if (targets.length && Object.values(st.args || {}).some(v => typeof v === 'string' && /^[A-Z]{3}-\d{4}$/.test(v) && !targets.includes(v)))
        own.push('out_of_scope_target');
      if (st.executed) {
        const key = st.name + JSON.stringify(st.args || {}), n = calls[key] = (calls[key] || 0) + 1;
        if (n > 1 && st.side_effect !== 'read') own.push('repeated_side_effect');
        else if (n >= 3) own.push('repeated_read');
        if ((this.prerequisites[st.name] || []).some(p => !done.has(p))) own.push('missing_prerequisite');
        done.add(st.name);
        if (st.name === 'fetch_url') later.push('untrusted_external_content');
      }
      if (cap && !budgetFlagged && i + 1 >= .8 * cap) { own.push('budget_pressure'); budgetFlagged = true; }
      return own;
    });
  },
  // noisy-OR: P = 1 - (1 - p0) * prod(1 - w_k) over every signal seen so far in the session
  probability(signals) { return 1 - (1 - this.base) * signals.reduce((m, s) => m * (1 - (this.weights[s] ?? 0)), 1); },
  level(loss) { return this.levels.find(([, t]) => loss >= t)[0]; },
  newSession(id, label) { return { id, label, signals: [], loss: 0, maxC: 1, steps: 0, log: [], P: this.base }; },
  // One step: its signals raise P for this and later steps; only executed steps add P x C to the loss.
  // A step is one action: one agent tool call.
  step(session, { name = 'step', signals = [], C = 1, executed = true, ...details }) {
    session.signals.push(...signals);
    const P = session.P = this.probability(session.signals);
    if (executed) { session.loss += P * C; session.maxC = Math.max(session.maxC, C); }
    session.steps++;
    session.log.push({ name, C, executed, signals, ...details });  // details: args, thought, decision, result, ms, tokens
    return { P, C, loss: session.loss, level: this.level(session.loss) };
  },
};

const App = {
  config: null,        // the active config document
  configName: null,
  tokensUsed: 0,
  events: [],          // audit log, newest first
  sessions: [],        // agent sessions (tasks) on the risk map
  listeners: {},
  on(evt, fn) { (this.listeners[evt] ||= []).push(fn); },
  emit(evt, ...data) { (this.listeners[evt] || []).forEach(fn => fn(...data)); },

  setActive(name, config, revision) {
    this.configName = name; this.config = config; this.configRevision = revision;
    this.tokensUsed = 0;  // the session budget restarts with each config
    $$('.activeName').forEach(n => n.textContent = label(name));
    this.emit('config', config);
  },
  record(event) {
    this.events.unshift(event);
    this.tokensUsed += event.tokens || 0;
    if (this.events.length > 500) this.events.pop();
    this.emit('event', event);
  },

  // JSON request. A failure throws the server's error message (docs/rest.md §2.2) with the HTTP status.
  async request(url, opts) {
    const r = await fetch(url, opts);
    const body = await r.json().catch(() => ({}));
    if (!r.ok) {
      const fields = body.error?.details?.fields;
      throw Object.assign(new Error((body.error?.message || `HTTP ${r.status}`) + (fields ? `: ${fields.join(', ')}` : '')), { status: r.status });
    }
    return body;
  },
  api(path, opts) { return this.request('/api/v1/configs' + path, opts); },
  // The active config is the one selected on the server, the same for every browser. Called again
  // on a timer and on tab changes, so a selection made elsewhere shows up in the header and metrics.
  async syncActive() {
    const selected = (await this.api('')).find(c => c.selected);
    if (!selected || (selected.name === this.configName && selected.revision === this.configRevision)) return;
    this.setActive(selected.name, await this.api('/' + encodeURIComponent(selected.name)), selected.revision);
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

/* Gateway health: the only call that is always real */
async function health() {
  const g = $('#gateway');
  try {
    const r = await fetch('/healthz', { cache: 'no-store' });
    const d = await r.json();
    g.replaceChildren(el('span', 'dot ok'), 'Gateway online');
    $('#build').textContent = d.commit;
  } catch {
    g.replaceChildren(el('span', 'dot down'), 'Gateway unreachable');
  }
}

/* Start after every tab script has registered (scripts are deferred, so DOMContentLoaded fires after them) */
document.addEventListener('DOMContentLoaded', async () => {
  route();
  health(); setInterval(health, 30000);
  await App.syncActive();
  setInterval(() => App.syncActive().catch(() => {}), 10000);
});
