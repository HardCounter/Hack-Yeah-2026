const $ = s => document.querySelector(s);
const form = $('#form');
const f = form.elements;
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };

let TOOLS = [];      // every tool the gateway knows; the lenient preset allows all of them
let current = null;  // last loaded policy; fields the editor does not show are kept as they are

const api = async (path, opts) => {
  const r = await fetch('/api/v1/policies' + path, opts);
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || `HTTP ${r.status}`);
  return body;
};

/* Auditors are found by id; a missing one is added with a safe default */
const AUDITORS = {
  signature: { id: 'signature-scanner', type: 'pattern_scanner', config: { patterns: [], action: 'BLOCK' } },
  privacy: { id: 'privacy-scanner', type: 'classified_scanner', config: { classes: ['pesel', 'iban'], action: 'REDACT' } },
  secret: { id: 'secret-scanner', type: 'classified_scanner', config: { classes: ['aws_access_key', 'private_key', 'api_key'], action: 'REDACT' } },
};
const auditor = (p, key) => p.auditors.find(a => a.id === AUDITORS[key].id) || structuredClone(AUDITORS[key]);

const showOutputs = () => form.querySelectorAll('output[data-for]').forEach(o => {
  const v = Number(f[o.dataset.for].value);
  o.textContent = o.dataset.for === 'block_threshold' ? v.toFixed(2) : v.toLocaleString('en-GB');
});
form.addEventListener('input', showOutputs);

function renderTools(p) {
  const tb = $('#tools'); tb.textContent = '';
  for (const t of TOOLS) {
    const tr = el('tr');
    const allow = el('input'); allow.type = 'checkbox'; allow.name = 'allow'; allow.value = t; allow.checked = p.allowed_tools.includes(t);
    const appr = el('input'); appr.type = 'checkbox'; appr.name = 'approve'; appr.value = t; appr.checked = p.require_approval.includes(t);
    allow.setAttribute('aria-label', `Allow ${t}`); appr.setAttribute('aria-label', `${t} needs approval`);
    const sync = () => { appr.disabled = !allow.checked; if (!allow.checked) appr.checked = false; };
    allow.addEventListener('change', sync); sync();
    const a = el('td'); a.append(allow); const b = el('td'); b.append(appr);
    tr.append(el('td', 'mono cap', t), a, b); tb.append(tr);
  }
}

function fill(p) {
  current = structuredClone(p);
  f.tokens.value = p.budget.tokens;
  f.tool_calls.value = p.budget.tool_calls;
  f.max_output_tokens.value = p.max_output_tokens;
  f.cost_usd.value = p.budget.cost_usd ?? '';
  f.block_threshold.value = p.semantic_guard?.block_threshold ?? 0.9;
  const sig = auditor(p, 'signature');
  f.patterns.value = sig.config.patterns.join('\n');
  f.patterns_action.value = sig.config.action;
  f.privacy_action.value = auditor(p, 'privacy').config.action;
  f.secret_action.value = auditor(p, 'secret').config.action;
  renderTools(p);
  f.name.value = p.name || '';
  f.description.value = p.description || '';
  showOutputs();
}

function collect() {
  const p = structuredClone(current);
  const checked = name => [...form.querySelectorAll(`input[name=${name}]:checked`)].map(x => x.value);
  p.name = f.name.value.trim();
  p.description = f.description.value.trim();
  p.allowed_tools = checked('allow');
  p.require_approval = checked('approve');
  p.budget = { ...p.budget, tokens: +f.tokens.value, tool_calls: +f.tool_calls.value,
    cost_usd: f.cost_usd.value === '' ? null : +f.cost_usd.value };
  p.max_output_tokens = +f.max_output_tokens.value;
  p.semantic_guard = { ...(p.semantic_guard || { on_error: 'BLOCK' }), block_threshold: +f.block_threshold.value };
  const set = (key, config) => {
    const a = auditor(p, key); a.config = { ...a.config, ...config };
    p.auditors = p.auditors.filter(x => x.id !== a.id).concat(a);
  };
  set('signature', { patterns: f.patterns.value.split('\n').map(s => s.trim()).filter(Boolean), action: f.patterns_action.value });
  set('privacy', { action: f.privacy_action.value });
  set('secret', { action: f.secret_action.value });
  // keep the tool allowlist auditor, if any, in step with the checkboxes
  for (const a of p.auditors) if (a.type === 'tool_allowlist') a.config.allowed_tools = p.allowed_tools;
  return p;
}

const PRESETS = ['lenient', 'standard', 'strict'];
let selected = null;
async function load(name) {
  form.setAttribute('aria-busy', 'true');
  try {
    const p = await api('/' + encodeURIComponent(name));
    selected = name; fill(p);
    if (PRESETS.includes(name)) f.name.value = '';  // presets are read-only, so ask for a new name
    document.querySelectorAll('#list button').forEach(b => b.setAttribute('aria-pressed', b.value === name));
    say('');
  } catch (e) { say(`Could not load ${name}: ${e.message}`, true); }
  form.removeAttribute('aria-busy');
}

async function refreshList() {
  const items = await api('');
  const ul = $('#list'); ul.textContent = '';
  for (const it of items) {
    const li = el('li'), b = el('button');
    b.type = 'button'; b.value = it.name; b.title = it.description;
    b.append(el('span', null, it.name), el('span', 'kind', it.preset ? 'preset' : 'saved'));
    b.setAttribute('aria-pressed', it.name === selected);
    b.onclick = () => load(it.name);
    li.append(b); ul.append(li);
  }
}

function say(text, bad) {
  const m = $('#saveMsg'); m.textContent = text; m.className = 'cap ' + (bad ? 'msg-err' : 'msg-ok');
}

form.addEventListener('submit', async e => {
  e.preventDefault();
  if (!f.name.checkValidity()) { say('Give the policy a name: lowercase letters, digits, - and _.', true); f.name.focus(); return; }
  const p = collect();
  try {
    await api('/' + encodeURIComponent(p.name), {
      method: 'PUT', body: JSON.stringify(p),
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + f.token.value },
    });
    selected = p.name; current = p;
    await refreshList();
    say(`Saved as “${p.name}”.`);
  } catch (err) { say('Not saved: ' + err.message, true); }
});

$('#download').onclick = () => {
  const p = collect(), name = p.name || 'policy';
  const a = el('a'); a.href = URL.createObjectURL(new Blob([JSON.stringify(p, null, 2) + '\n'], { type: 'application/json' }));
  a.download = name + '.json'; a.click(); URL.revokeObjectURL(a.href);
};

(async () => {
  form.setAttribute('aria-busy', 'true');
  try {
    TOOLS = (await api('/lenient')).allowed_tools;
    selected = 'standard';
    await refreshList();
    await load('standard');
    $('#loading').hidden = true;
  } catch (e) { $('#loading').textContent = 'Could not load policies: ' + e.message; }
})();
