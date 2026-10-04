/* Config: the three server configs (docs/rest.md §4.18-4.19). Edit one, save it with PUT /configs/{name},
   make it active with PUT /config-selection. */
(() => {
  const form = $('#cfgForm'), f = form.elements;
  const THRESHOLDS = ['block_threshold', 'approve_threshold', 'alert_threshold'];  // highest first
  const AUDITORS = {
    signature: { id: 'signature-scanner', type: 'pattern_scanner', config: { patterns: [], action: 'BLOCK' } },
    privacy: { id: 'privacy-scanner', type: 'classified_scanner', config: { classes: ['pesel', 'iban'], action: 'REQUIRE_APPROVAL' } },
    secret: { id: 'secret-scanner', type: 'classified_scanner', config: { classes: ['aws_access_key', 'private_key', 'api_key'], action: 'REQUIRE_APPROVAL' } },
  };
  const MODEL_LISTS = { agent: 'cfgAgentModels', judge: 'cfgJudgeModels' };
  // Everything a checkbox can offer: these defaults plus whatever any server config already allows.
  const CATALOG = {
    tools: new Set(),
    agent: new Set(['llama3.2', 'gpt-4.1-mini', 'gpt-4.1', 'gpt-5-mini', 'claude-sonnet-5-5']),
    judge: new Set(['llama-guard3', 'gpt-4.1-mini', 'gpt-4o-mini', 'claude-haiku-4-5']),
  };
  const auditor = (c, key) => c.auditors.find(a => a.id === AUDITORS[key].id) || structuredClone(AUDITORS[key]);
  const models = { agent: c => c.allowed_models, judge: c => c.intercept.semantic_guard.allowed_models };
  function learn(c) {
    c.allowed_tools.forEach(t => CATALOG.tools.add(t));
    for (const kind in models) models[kind](c).forEach(m => CATALOG[kind].add(m));
  }

  let items = [];      // GET /configs: name, description, revision, selected, requires_selection
  let loaded = null;   // config as loaded from the server; fields the form does not show are kept
  let editing = null;  // its name
  let dirty = false;

  const say = (text, bad) => { const m = $('#cfgMsg'); m.textContent = text; m.className = 'cap ' + (bad ? 'v-block' : 'v-allow'); };
  const item = name => items.find(i => i.name === name) || {};
  // Active = the selected config at the selected revision. A saved edit stays inactive until it is selected again.
  const isActive = name => item(name).selected && !item(name).requires_selection;

  function showState() {
    const s = $('#cfgState'), active = isActive(editing) && !dirty, stale = item(editing).selected && !active && !dirty;
    s.textContent = dirty ? 'Unsaved changes' : active ? 'Active' : stale ? 'Saved, older revision active' : 'Saved';
    s.className = 'badge ' + (dirty || stale ? 'warn' : active ? 'allow' : 'neutral');
    $('#cfgActivate').disabled = active;
    $('#cfgActivate').textContent = dirty ? 'Save and make active' : 'Make active';
    $('#cfgSave').disabled = !dirty;
  }
  const markDirty = () => { dirty = true; showState(); };

  const showOutputs = () => form.querySelectorAll('output[data-for]').forEach(o => {
    const v = Number(f[o.dataset.for].value);
    o.textContent = THRESHOLDS.includes(o.dataset.for) ? Math.round(v * 100) + '%' : fmtNum(v);
  });
  // The server requires block >= approval >= alert: moving one slider pushes the others out of its way.
  function orderThresholds(moved) {
    const i = THRESHOLDS.indexOf(moved);
    if (i < 0) return;
    const v = +f[moved].value;
    THRESHOLDS.forEach((n, j) => {
      if (j < i && +f[n].value < v) f[n].value = v;
      if (j > i && +f[n].value > v) f[n].value = v;
    });
  }
  // The management token is typed by the operator and kept for this tab only; it is never part of a config.
  try { f.token.value = sessionStorage.getItem('configToken') || ''; } catch {}
  form.addEventListener('input', e => {
    if (e.target.name === 'token') { try { sessionStorage.setItem('configToken', f.token.value); } catch {} return; }
    orderThresholds(e.target.name); showOutputs(); markDirty();
  });
  form.addEventListener('change', e => { if (e.target.type === 'checkbox' || e.target.tagName === 'SELECT') markDirty(); });

  function renderTools(c) {
    const tb = $('#cfgTools'); tb.textContent = '';
    for (const t of CATALOG.tools) {
      const allow = el('input'); allow.type = 'checkbox'; allow.name = 'allow'; allow.value = t; allow.checked = c.allowed_tools.includes(t);
      const appr = el('input'); appr.type = 'checkbox'; appr.name = 'approve'; appr.value = t; appr.checked = c.require_approval.includes(t);
      allow.setAttribute('aria-label', `Allow ${t}`); appr.setAttribute('aria-label', `${t} needs approval`);
      const sync = () => { appr.disabled = !allow.checked; if (!allow.checked) appr.checked = false; };
      allow.addEventListener('change', sync); sync();
      const a = el('td'), b = el('td'); a.append(allow); b.append(appr);
      const tr = el('tr'); tr.append(el('td', 'mono cap', t), a, b); tb.append(tr);
    }
  }

  const allowedModels = kind => $$(`#${MODEL_LISTS[kind]} input:checked`).map(x => x.value);
  function showModelSummary(kind) {
    const on = allowedModels(kind), all = $$(`#${MODEL_LISTS[kind]} input`).length;
    $(`#${MODEL_LISTS[kind]}Sum`).textContent = `${on.length} of ${all} allowed`;
  }
  function renderModels(kind, allowed) {
    const ul = $(`#${MODEL_LISTS[kind]} ul`); ul.textContent = '';
    for (const m of CATALOG[kind]) {
      const box = el('input'); box.type = 'checkbox'; box.name = kind + '_model'; box.value = m; box.checked = allowed.includes(m);
      const state = el('span', '', box.checked ? 'allowed' : 'not allowed');
      box.addEventListener('change', () => { state.textContent = box.checked ? 'allowed' : 'not allowed'; showModelSummary(kind); });
      const lab = el('label'); lab.append(box, m, state);
      const li = el('li'); li.append(lab); ul.append(li);
    }
    showModelSummary(kind);
  }
  // Close an open model dropdown on Escape or a click outside it.
  document.addEventListener('click', e => $$('details.multi[open]').forEach(d => { if (!d.contains(e.target)) d.open = false; }));
  document.addEventListener('keydown', e => {
    if (e.key !== 'Escape') return;
    $$('details.multi[open]').forEach(d => { d.open = false; d.querySelector('summary').focus(); });
  });

  // A select shows the stored action even when the page has no option for it (e.g. REDACT).
  function choose(select, value) {
    if (![...select.options].some(o => o.value === value)) select.add(new Option(value, value));
    select.value = value;
  }

  function fill(name, c) {
    loaded = structuredClone(c); editing = name; dirty = false;
    learn(c);
    $('#cfgEditing').textContent = label(name);
    $('#cfgDesc').textContent = c.description || 'No description.';
    f.tokens.value = c.budget.tokens;
    f.tool_calls.value = c.budget.tool_calls;
    f.max_output_tokens.value = c.max_output_tokens;
    f.cost_usd.value = c.budget.cost_usd ?? '';
    const g = c.intercept.semantic_guard;
    for (const n of THRESHOLDS) f[n].value = g[n];
    const sig = auditor(c, 'signature');
    f.patterns.value = sig.config.patterns.join('\n');
    choose(f.patterns_action, sig.config.action);
    choose(f.privacy_action, auditor(c, 'privacy').config.action);
    choose(f.secret_action, auditor(c, 'secret').config.action);
    f.description.value = c.description || '';
    for (const kind in models) renderModels(kind, models[kind](c));
    renderTools(c); showOutputs(); showState();
    $$('#cfgList button').forEach(b => b.setAttribute('aria-pressed', b.value === name));
  }

  function collect() {
    const c = structuredClone(loaded);
    const checked = n => $$(`#cfgTools input[name=${n}]:checked`).map(x => x.value);
    c.description = f.description.value.trim();
    c.allowed_tools = checked('allow');
    c.require_approval = checked('approve');
    c.budget = { ...c.budget, tokens: +f.tokens.value, tool_calls: +f.tool_calls.value, cost_usd: f.cost_usd.value === '' ? null : +f.cost_usd.value };
    c.max_output_tokens = +f.max_output_tokens.value;
    c.allowed_models = allowedModels('agent');
    c.intercept.semantic_guard = { ...c.intercept.semantic_guard, block_threshold: +f.block_threshold.value,
      approve_threshold: +f.approve_threshold.value, alert_threshold: +f.alert_threshold.value,
      allowed_models: allowedModels('judge') };
    const set = (key, conf) => {
      const a = auditor(c, key); a.config = { ...a.config, ...conf };
      c.auditors = c.auditors.filter(x => x.id !== a.id).concat(a);
    };
    set('signature', { patterns: f.patterns.value.split('\n').map(s => s.trim()).filter(Boolean), action: f.patterns_action.value });
    set('privacy', { action: f.privacy_action.value });
    set('secret', { action: f.secret_action.value });
    for (const a of c.auditors) if (a.type === 'tool_allowlist') a.config.allowed_tools = c.allowed_tools;
    return c;
  }

  // What the server would reject anyway, said in plain words before the request is sent.
  function problem(c) {
    if (!c.allowed_tools.length) return 'Allow at least one tool.';
    if (!c.allowed_models.length) return 'Allow at least one agent model.';
    if (!c.intercept.semantic_guard.allowed_models.length) return 'Allow at least one judge model.';
    return null;
  }

  function explain(err) {
    if (err.status === 401) { f.token.focus(); return 'The management token is missing or wrong.'; }
    if (err.status === 409) return 'The config changed on the server. Reload it and try again.';
    return err.message;
  }
  function writeOptions(body) {
    return { method: 'PUT', body: JSON.stringify(body),
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + f.token.value.trim() } };
  }

  async function load(name) {
    if (dirty && !confirmDiscard()) return false;
    try { fill(name, await App.api('/' + encodeURIComponent(name))); say(''); return true; }
    catch (e) { say(`Could not load ${name}: ${e.message}`, true); return false; }
  }
  // Inline confirmation instead of a blocking dialog: the first click warns, the second discards.
  let warned = false;
  function confirmDiscard() {
    if (warned) { warned = false; return true; }
    warned = true; say('Unsaved changes. Click again to discard them.', true);
    setTimeout(() => warned = false, 4000);
    return false;
  }

  async function refreshList() {
    items = await App.api('');
    $('#cfgList').replaceChildren(...items.map(it => {
      const li = el('li'), b = el('button'); b.type = 'button'; b.value = it.name;
      b.append(el('span', 'mono', label(it.name)));
      if (it.selected) b.append(badge('ACTIVE'));
      b.setAttribute('aria-pressed', it.name === editing);
      b.onclick = () => load(it.name);
      li.append(b);
      return li;
    }));
    showState();
  }

  // PUT /configs/{name}: the complete config, never a patch. Returns the update result, or null when nothing was sent.
  async function save() {
    const c = collect(), bad = problem(c);
    if (bad) { say(bad, true); return null; }
    const result = await App.api('/' + encodeURIComponent(editing), writeOptions(c));
    loaded = c; dirty = false;
    $('#cfgDesc').textContent = c.description || 'No description.';
    await refreshList();
    return result;
  }

  form.addEventListener('submit', async e => {
    e.preventDefault();
    try {
      const result = await save();
      if (result) say(result.requires_selection
        ? `Saved "${label(editing)}". It applies after you make it active.`
        : `Saved "${label(editing)}".`);
    } catch (err) { say('Not saved: ' + explain(err), true); }
  });

  // PUT /config-selection with the revision the page last saw; a stale revision is refused with 409.
  $('#cfgActivate').onclick = async () => {
    try {
      const revision = dirty ? (await save())?.revision : item(editing).revision;
      if (!revision) return;
      await App.request('/api/v1/config-selection', writeOptions({ name: editing, revision }));
      App.setActive(editing, structuredClone(loaded));  // emits 'config', which refreshes the list
      say(`"${label(editing)}" is now active for new agent sessions.`);
    } catch (err) { say('Not made active: ' + explain(err), true); refreshList().catch(() => {}); }
  };

  $('#cfgDownload').onclick = () => {
    const a = el('a'); a.href = URL.createObjectURL(new Blob([JSON.stringify(collect(), null, 2) + '\n'], { type: 'application/json' }));
    a.download = editing + '.json'; a.click(); URL.revokeObjectURL(a.href);
  };

  $('#cfgReload').onclick = () => refreshList().catch(e => say('Could not list configs: ' + e.message, true));
  App.on('config', () => refreshList().catch(() => {}));

  document.addEventListener('DOMContentLoaded', async () => {
    try {
      await refreshList();
      for (const it of items) learn(await App.api('/' + encodeURIComponent(it.name)));
      await load((items.find(i => i.selected) || items[0]).name);
    } catch (e) { $('#cfgList').replaceChildren(el('li', 'empty', 'Could not load configs: ' + e.message)); }
  });
})();
