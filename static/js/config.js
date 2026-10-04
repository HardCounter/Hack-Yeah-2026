/* Config: list presets and saved configs from the server, edit one, make it active or save it. */
(() => {
  const form = $('#cfgForm'), f = form.elements;
  const PRESETS = ['lenient', 'standard', 'strict'];
  const AUDITORS = {
    signature: { id: 'signature-scanner', type: 'pattern_scanner', config: { patterns: [], action: 'BLOCK' } },
    privacy: { id: 'privacy-scanner', type: 'classified_scanner', config: { classes: ['pesel', 'iban'], action: 'REQUIRE_APPROVAL' } },
    secret: { id: 'secret-scanner', type: 'classified_scanner', config: { classes: ['aws_access_key', 'private_key', 'api_key'], action: 'REQUIRE_APPROVAL' } },
  };
  const auditor = (c, key) => c.auditors.find(a => a.id === AUDITORS[key].id) || structuredClone(AUDITORS[key]);

  let TOOLS = [];      // every tool the gateway knows; the lenient preset allows all of them
  let loaded = null;   // config as loaded from the server; fields the form does not show are kept
  let editing = null;  // its name
  let dirty = false;

  const say = (text, bad) => { const m = $('#cfgMsg'); m.textContent = text; m.className = 'cap ' + (bad ? 'v-block' : 'v-allow'); };

  function showState() {
    const s = $('#cfgState');
    const active = editing === App.configName && !dirty;
    s.textContent = dirty ? 'Unsaved changes' : active ? 'Active' : PRESETS.includes(editing) ? 'Preset' : 'Saved';
    s.className = 'badge ' + (dirty ? 'warn' : active ? 'allow' : 'neutral');
    $('#cfgActivate').disabled = active;
    $('#cfgActivate').textContent = dirty ? 'Apply changes' : 'Make active';
  }
  const markDirty = () => { dirty = true; showState(); };

  const showOutputs = () => form.querySelectorAll('output[data-for]').forEach(o => {
    const v = Number(f[o.dataset.for].value);
    o.textContent = o.dataset.for === 'block_threshold' ? Math.round(v * 100) + '%' : fmtNum(v);
  });
  form.addEventListener('input', e => { showOutputs(); if (!['name', 'description'].includes(e.target.name)) markDirty(); });
  form.addEventListener('change', e => { if (e.target.type === 'checkbox') markDirty(); });

  function renderTools(c) {
    const tb = $('#cfgTools'); tb.textContent = '';
    for (const t of TOOLS) {
      const allow = el('input'); allow.type = 'checkbox'; allow.name = 'allow'; allow.value = t; allow.checked = c.allowed_tools.includes(t);
      const appr = el('input'); appr.type = 'checkbox'; appr.name = 'approve'; appr.value = t; appr.checked = c.require_approval.includes(t);
      allow.setAttribute('aria-label', `Allow ${t}`); appr.setAttribute('aria-label', `${t} needs approval`);
      const sync = () => { appr.disabled = !allow.checked; if (!allow.checked) appr.checked = false; };
      allow.addEventListener('change', sync); sync();
      const a = el('td'), b = el('td'); a.append(allow); b.append(appr);
      const tr = el('tr'); tr.append(el('td', 'mono cap', t), a, b); tb.append(tr);
    }
  }

  function fill(name, c) {
    loaded = structuredClone(c); editing = name; dirty = false;
    $('#cfgEditing').textContent = name;
    $('#cfgDesc').textContent = c.description || 'No description.';
    f.tokens.value = c.budget.tokens;
    f.tool_calls.value = c.budget.tool_calls;
    f.max_output_tokens.value = c.max_output_tokens;
    f.cost_usd.value = c.budget.cost_usd ?? '';
    f.block_threshold.value = c.semantic_guard?.block_threshold ?? 0.9;
    const sig = auditor(c, 'signature');
    f.patterns.value = sig.config.patterns.join('\n');
    f.patterns_action.value = sig.config.action;
    f.privacy_action.value = auditor(c, 'privacy').config.action;
    f.secret_action.value = auditor(c, 'secret').config.action;
    f.name.value = PRESETS.includes(name) ? '' : name;
    f.description.value = c.description || '';
    renderTools(c); showOutputs(); showState();
    $$('#cfgList button').forEach(b => b.setAttribute('aria-pressed', b.value === name));
  }

  function collect() {
    const c = structuredClone(loaded);
    const checked = n => $$(`#cfgTools input[name=${n}]:checked`).map(x => x.value);
    c.description = f.description.value.trim() || c.description;
    c.allowed_tools = checked('allow');
    c.require_approval = checked('approve');
    c.budget = { ...c.budget, tokens: +f.tokens.value, tool_calls: +f.tool_calls.value, cost_usd: f.cost_usd.value === '' ? null : +f.cost_usd.value };
    c.max_output_tokens = +f.max_output_tokens.value;
    c.semantic_guard = { on_error: 'BLOCK', ...c.semantic_guard, block_threshold: +f.block_threshold.value };
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
    const items = await App.api('');
    const ul = $('#cfgList'); ul.textContent = '';
    const group = (title, list) => {
      if (!list.length) return;
      ul.append(el('li', 'picker-group', title));
      for (const it of list) {
        const li = el('li'), b = el('button'); b.type = 'button'; b.value = it.name;
        const name = el('span', 'mono', it.name); name.append(el('span', 'sub', it.description || ''));
        b.append(name);
        if (it.name === App.configName) b.append(badge('ACTIVE'));
        b.setAttribute('aria-pressed', it.name === editing);
        b.onclick = () => load(it.name);
        li.append(b); ul.append(li);
      }
    };
    group('Presets', items.filter(i => i.preset));
    group('Saved', items.filter(i => !i.preset));
  }

  $('#cfgActivate').onclick = async () => {
    const c = collect();
    const name = dirty ? `${editing} (edited)` : editing;
    App.setActive(name, c);
    loaded = c; dirty = false;
    say(`"${name}" is now active in this browser.`);
    showState(); refreshList();
  };

  $('#cfgDownload').onclick = () => {
    const c = collect(), name = f.name.value.trim() || editing;
    const a = el('a'); a.href = URL.createObjectURL(new Blob([JSON.stringify({ ...c, name }, null, 2) + '\n'], { type: 'application/json' }));
    a.download = name + '.json'; a.click(); URL.revokeObjectURL(a.href);
  };

  form.addEventListener('submit', async e => {
    e.preventDefault();
    const name = f.name.value.trim();
    if (!f.name.checkValidity() || !name) { say('Enter a name: lowercase letters, digits, - and _.', true); f.name.focus(); return; }
    if (PRESETS.includes(name)) { say('Presets are read-only. Choose another name.', true); f.name.focus(); return; }
    const c = { ...collect(), name };
    try {
      await App.api('/' + encodeURIComponent(name), {
        method: 'PUT', body: JSON.stringify(c),
        headers: { 'Content-Type': 'application/json' },
      });
      fill(name, c);
      await refreshList();
      say(`Saved as "${name}" on the server.`);
    } catch (err) { say('Not saved: ' + err.message, true); }
  });

  $('#cfgReload').onclick = () => refreshList().catch(e => say('Could not list configs: ' + e.message, true));
  App.on('config', () => { showState(); refreshList().catch(() => {}); });

  document.addEventListener('DOMContentLoaded', async () => {
    try {
      TOOLS = (await App.api('/lenient')).allowed_tools;
      await refreshList();
      let start = 'standard';
      try { start = localStorage.getItem('activeConfig') || start; } catch {}
      if (!await load(start)) await load('standard');
    } catch (e) { $('#cfgList').replaceChildren(el('li', 'empty', 'Could not load configs: ' + e.message)); }
  });
})();
