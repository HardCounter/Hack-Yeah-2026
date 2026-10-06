/* Risk map (Metrics tab): agent sessions on likelihood x impact, scored with the shared Risk model. */
(() => {
  const W = 680, H = 400, M = { l: 116, r: 16, t: 14, b: 50 };
  const PW = W - M.l - M.r, PH = H - M.t - M.b;
  const x = p => M.l + p * PW;
  const y = c => M.t + (1 - c / 10) * PH;
  const IMPACT_TICKS = [[1, 'read'], [4, 'egress'], [5, 'write'], [8, 'create client'], [10, 'irreversible']];

  // Sessions come from the read API (docs/rest.md): every step is a tool call the gateway really decided on.
  const get = path => App.read(path);
  const details = new Map();
  const level = s => s.level || 'UNAVAILABLE';
  const figure = (v, places = 0) => v == null ? '—' : Number(v).toFixed(places);
  function fromSummary(summary) {
    const r = summary.risk;
    const verification = summary.verification;
    return { id: summary.session_id, label: `${summary.case_id || summary.session_id} · ${summary.agent_id || 'unknown agent'}`,
      P: r?.failure_probability ?? null, loss: r?.expected_loss ?? null, maxC: r?.max_consequence ?? null,
      level: r?.level?.toUpperCase() || null, steps: summary.risk_step_count ?? summary.action_count,
      verification_status: summary.verification_status, log: [], decisions: [],
      activeControls: (summary.active_interventions || []).map(i => code(i.action)).join(', ') || 'None',
      verdict: verification ? { decision: verification.verification_status, factors: {
        checks_total: verification.checks.length, checks_passed: verification.checks.filter(c => c.status === 'PASS').length } } : null };
  }
  function detailState(s) {
    if (!details.has(s.id)) details.set(s.id, { log: [], cursor: null, started: false, hasMore: true, loading: null, error: null });
    return details.get(s.id);
  }
  async function loadPage(s) {
    const state = detailState(s);
    if (state.loading) return state.loading;
    if (state.started && !state.hasMore) return;
    state.loading = (async () => {
      try {
        const query = new URLSearchParams({ view: 'full', kinds: 'tool_use,egress', include_detections: 'false', limit: '100' });
        if (state.cursor) query.set('cursor', state.cursor);
        const trajectory = await get(`/trajectories/session/${encodeURIComponent(s.id)}?${query}`);
        const steps = trajectory.segments.flatMap(g => g.steps).map(({ summary: a, event, risk, trace }) => {
          const d = event?.action_details || {}, held = a.status === 'pending_approval';
          return { event_id: a.event_id, name: a.name, C: risk?.consequence ?? null, P: risk?.probability ?? null,
            risk: risk?.expected_loss ?? null, signals: risk?.signals || [], executed: a.executed,
            args: d.parameters, trace: trace || [], ms: a.executed ? a.usage?.latency_ms : null,
            tokens: a.usage?.input_tokens != null || a.usage?.output_tokens != null
              ? { in: a.usage.input_tokens, out: a.usage.output_tokens } : null,
            decision: { verdict: VERDICT[a.decision] || (a.executed ? 'ALLOWED' : 'BLOCKED'),
              by: a.triggered_rules?.[0]?.auditor, reason: a.triggered_rules?.[0]?.rule_id || a.reason_code },
            result: d.error ? { status: 'error', summary: d.error }
              : a.executed ? { status: 'ok', data: d.result, redacted: a.status === 'redacted' }
              : { status: held ? 'held' : 'blocked', summary: held ? 'Not executed, waiting for approval' : 'Not executed, stopped at the gateway' } };
        });
        const known = new Set(state.log.map(st => st.event_id));
        state.log.push(...steps.filter(st => !known.has(st.event_id)));
        state.cursor = trajectory.next_cursor; state.hasMore = trajectory.has_more;
        state.started = true; state.error = null;
      } catch (e) { state.error = e.message; }
      finally { state.loading = null; }
    })();
    return state.loading;
  }
  // A stable small visual offset separates sessions with equal impact.
  const jitter = id => { let hash = 0; for (const ch of id) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0; return (hash % 101 / 100 - .5) * .5; };

  const svg = $('#riskMap');
  const NS = 'http://www.w3.org/2000/svg';
  const node = (tag, attrs, text) => { const e = document.createElementNS(NS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); if (text != null) e.textContent = text; return e; };

  // Iso-risk curve P x C = T (C = T / P): to its right, one such step alone reaches that level
  function isoCurve(T, label) {
    const pts = [];
    for (let p = T / 10; p <= 1.0001; p += 0.01) pts.push(`${x(p).toFixed(1)},${y(Math.min(10, T / p)).toFixed(1)}`);
    return [node('polyline', { points: pts.join(' '), class: 'iso' }),
      node('text', { x: x(T / 10) - 6, y: y(10) + 12, 'text-anchor': 'end', class: 'iso-label' }, label)];
  }

  function drawFrame() {
    svg.append(node('rect', { x: M.l, y: M.t, width: PW, height: PH, class: 'plot' }));
    for (let p = 0; p <= 1; p += .125) {  // labelled every 0.25, a line in between too
      svg.append(node('line', { x1: x(p), x2: x(p), y1: M.t, y2: M.t + PH, class: 'grid-line' }));
      if (p * 4 % 1 === 0) svg.append(node('text', { x: x(p), y: M.t + PH + 16, 'text-anchor': 'middle', class: 'tick' }, p.toFixed(2)));
    }
    for (let c = 1; c <= 10; c++) svg.append(node('line', { x1: M.l, x2: M.l + PW, y1: y(c), y2: y(c), class: 'grid-line' }));
    for (const [c, name] of IMPACT_TICKS) {
      svg.append(node('text', { x: M.l - 6, y: y(c) + 4, 'text-anchor': 'end', class: 'tick' }, `${c} ${name}`));
    }
    svg.append(node('line', { x1: M.l, x2: M.l + PW, y1: M.t + PH, y2: M.t + PH, class: 'axis' }),
      node('line', { x1: M.l, x2: M.l, y1: M.t, y2: M.t + PH, class: 'axis' }),
      ...isoCurve(3, 'one step → medium'), ...isoCurve(8, 'one step → high'),
      node('text', { x: M.l + PW / 2, y: H - 8, 'text-anchor': 'middle', class: 'axis-title' }, 'Likelihood the session has gone wrong (P)'),
      node('text', { x: 14, y: M.t + PH / 2, 'text-anchor': 'middle', transform: `rotate(-90 14 ${M.t + PH / 2})`, class: 'axis-title' }, 'Impact of riskiest executed step (C)'));
  }

  const pop = $('#pop');
  const describe = s => `${s.label}, likelihood ${figure(s.P, 2)}, impact ${s.maxC}, expected loss ${figure(s.loss, 2)}, level ${level(s)}, ${s.steps} steps`;
  const td = (cls, ...kids) => { const c = el('td', cls); c.append(...kids); return c; };
  const verdict = st => st.decision?.verdict || (st.executed ? 'ALLOWED' : 'BLOCKED');
  const RESULT = { ok: 'allow', blocked: 'block', error: 'block', held: 'hold' };
  // One step's call details: reasoning, arguments, gateway decision, result, cost. Every field is optional.
  function stepDetail(st) {
    const dec = st.decision || {}, res = st.result, v = verdict(st), d = el('div', 'step-detail ' + (TONE[v] || 'neutral'));
    const sec = (title, ...kids) => { const s = el('section'); s.append(el('h4', null, title), ...kids); d.append(s); };
    const pre = o => el('pre', 'mono', JSON.stringify(o, null, 2));
    if (st.thought) sec('Thought', el('blockquote', null, st.thought));
    if (st.args && Object.keys(st.args).length) sec('Arguments', pre(st.args));
    const g = el('p'); g.append(badge(v), dec.by ? ` by ${dec.by}` : '', dec.reason ? ` · ${dec.reason}` : ''); sec('Gateway decision', g);
    if (res) {
      const r = el('p'); r.append(el('span', 'badge ' + (RESULT[res.status] || 'neutral'), res.status || '—'), ...(res.redacted ? [' ', badge('REDACTED')] : []), ' ', res.summary || '');
      sec('Result', r, ...(res.data != null ? [pre(res.data)] : []));
    }
    // Control-plane plugins that decided on this step (docs/rest.md §4.21)
    if (st.trace?.length) sec('Control plane', ...st.trace.map(t => {
      const p = el('p'); p.append(el('span', 'mono', t.plugin), ' ', badge(code(t.decision)), ` ${t.reasoning || ''} · ${ms(t.duration_ms)}`,
        ...(t.adjustments || []).map(j => ` · ${j.action} ${j.outcome}`));
      return p;
    }));
    // Display only measurements present in persisted evidence.
    const foot = [st.ms != null && ms(st.ms), st.tokens && `${st.tokens.in == null ? '—' : fmtNum(st.tokens.in)} in / ${st.tokens.out == null ? '—' : fmtNum(st.tokens.out)} out tokens`].filter(Boolean);
    if (foot.length) { const p = el('p', 'step-foot mono'); foot.forEach((f, i) => p.append(...(i ? [' · '] : []), f)); d.append(p); }

    return d;
  }
  // Session card: header with level, key figures, then one table row per step
  function cardContent(s, state, refresh) {
    const lvl = level(s), head = el('div', 'pop-head'), f = s.verdict?.factors;
    head.append(el('strong', null, s.label), badge(lvl), ...(s.verdict ? [badge(code(s.verdict.decision))] : []));
    const facts = el('dl', 'pop-facts');
    for (const [k, v] of [['Likelihood P', figure(s.P, 2)], ['Impact C', figure(s.maxC)], ['Expected loss', figure(s.loss, 2)], ['Active controls', s.activeControls],
      ...(s.verdict ? [['Outcome', `${code(s.verdict.decision)}${f?.checks_total != null ? ` · ${f.checks_passed}/${f.checks_total} checks` : ''}`]] : [])])
      facts.append(el('dt', null, k), el('dd', 'mono', String(v)));
    const t = el('table', 'pop-steps'), hr = el('tr');
    hr.append(...['#', 'Step', 'Impact', 'Status', 'Signals'].map(h => el('th', null, h)));
    t.append(el('thead'), el('tbody')); t.tHead.append(hr);
    t.tBodies[0].append(...state.log.map((st, i) => {
      const tr = el('tr', 'step'), sig = st.signals || [];
      tr.append(el('td', 'n mono', i + 1), el('td', 'mono', st.name), el('td', 'n mono', figure(st.C)),
        td(null, badge(verdict(st))),
        el('td', sig.length ? 'v-warn' : 'faint', sig.join(', ').replace(/_/g, ' ') || '—'));
      // Click or Enter/Space opens the call details in a full-width row below; several can be open
      let row;
      const toggle = () => {
        if (row) { row.remove(); row = null; }
        else { const c = td('step-cell', stepDetail(st)); c.colSpan = 5; row = el('tr', 'step-row'); row.append(c); tr.after(row); }
        tr.setAttribute('aria-expanded', !!row);
      };
      tr.tabIndex = 0; tr.setAttribute('aria-expanded', false); tr.title = 'Show call details';
      tr.onclick = toggle; tr.onkeydown = k => { if (k.key === 'Enter' || k.key === ' ') { k.preventDefault(); toggle(); } };
      return tr;
    }));
    const cap = el('p', 'cap muted', `${state.log.length} of ${s.steps} recorded tool and egress steps`);
    const d = el('div', `session-card lvl-${lvl}`); d.append(head, facts, cap, t);
    if (state.error) d.append(el('p', 'muted', `Steps unavailable: ${state.error}`));
    if (state.hasMore) {
      const more = el('button', 'btn ghost', state.loading ? 'Loading steps…' : state.started ? 'Load more steps' : 'Load steps');
      more.disabled = !!state.loading;
      more.onclick = async () => { const pending = loadPage(s); refresh(); await pending; refresh(); };
      d.append(more);
    }
    return d;
  }
  function card(s) {
    const container = el('div'), state = detailState(s);
    const refresh = () => container.replaceChildren(cardContent(s, state, refresh));
    refresh();
    if (!state.started && !state.loading) { const pending = loadPage(s); refresh(); pending.then(refresh); }
    else if (state.loading) state.loading.then(refresh);
    return container;
  }
  App.sessionCard = card;
  // Same card for a dot on the map and a row in the session table. It stays open while the
  // cursor or focus is on the target or inside the card, so its buttons can be used.
  let hideTimer;
  const keep = () => clearTimeout(hideTimer);
  const hide = () => { keep(); hideTimer = setTimeout(() => pop.hidden = true, 250); };
  pop.addEventListener('mouseenter', keep); pop.addEventListener('focusin', keep);
  pop.addEventListener('mouseleave', hide); pop.addEventListener('focusout', hide);
  addEventListener('hashchange', () => { keep(); pop.hidden = true; });  // e.g. after Open details
  function hoverCard(target, s) {
    const show = () => {
      keep();
      const open = el('a', 'btn ghost', 'Open details'); open.href = `#metrics/tasks/${encodeURIComponent(s.id)}`;
      const dl = el('button', 'btn ghost', 'Audit export (NDJSON)'); dl.type = 'button'; dl.onclick = () => App.downloadSession(s);
      const bar = el('div', 'pop-actions'); bar.append(open, dl);
      pop.replaceChildren(card(s), bar); pop.hidden = false;
      const r = target.getBoundingClientRect();  // runtime coordinates are measured, not design values
      pop.style.left = Math.max(0, Math.min(r.left + scrollX, innerWidth - pop.offsetWidth - 8)) + 'px';
      pop.style.top = (r.bottom + scrollY) + 'px';
    };
    target.addEventListener('mouseenter', show); target.addEventListener('focus', show);
    target.addEventListener('mouseleave', hide); target.addEventListener('blur', hide);
  }
  function point(s) {
    const lvl = level(s);
    const c = node('circle', { cx: x(s.P), cy: y(Math.min(10, s.maxC + jitter(s.id))), r: 5 + Math.min(s.steps, 12) * .5,
      class: `pt lvl-${lvl}`, tabindex: 0, role: 'img', 'aria-label': describe(s) });
    hoverCard(c, s);
    return c;
  }

  function render(note) {
    svg.querySelectorAll('.pt').forEach(n => n.remove());
    for (const s of App.sessions) if (s.P != null && s.maxC != null) svg.append(point(s));
    if (!App.sessions.length) {
      const c = el('td', 'cap muted', note || 'No agent sessions yet.'); c.colSpan = 6;
      return $('#riskTable').replaceChildren(el('tr').appendChild(c).parentNode);
    }
    $('#riskTable').replaceChildren(...[...App.sessions].sort((a, b) => b.loss - a.loss).slice(0, 8).map(s => {
      const tr = el('tr'), lvl = level(s), b = el('td');
      b.append(badge(lvl));
      tr.append(el('td', 'cap', s.label), el('td', 'n mono cap', figure(s.P, 2)), el('td', 'n mono cap', figure(s.maxC)),
        el('td', 'n mono cap', figure(s.loss, 2)), b, el('td', 'cap muted', s.activeControls));
      tr.tabIndex = 0; tr.setAttribute('aria-label', describe(s)); hoverCard(tr, s);
      return tr;
    }));
  }

  drawFrame();
  // Legend: colour = level and what happens; one short reading note
  const MEANING = { LOW: 'Low', MEDIUM: 'Medium · review', HIGH: 'High · approval', CRITICAL: 'Critical · stopped' };
  const swatch = (cls, text) => { const s = el('span'); s.append(el('i', cls), text); return s; };
  $('#riskLegend').append(
    ...Risk.levels.slice().reverse().map(([lvl]) => swatch(lvl, MEANING[lvl])),
    el('p', 'legend-note', 'Size - steps taken'), el('p', 'legend-note', 'Hover a dot for its steps'));
  let loading = false;
  async function load() {
    if (loading) return;
    loading = true;
    let note;
    try {
      const { items } = await get('/sessions?limit=50');
      App.sessions = items.map(fromSummary);
      details.clear();
    } catch (e) {
      App.sessions = []; note = `Sessions unavailable: ${e.message}`;
    }
    loading = false;
    render(note);
    App.emit('sessions');
  }
  App.sessions = [];
  render('Loading sessions…');
  load(); setInterval(load, 30000);
})();
