/* Risk map (Metrics tab): agent sessions on likelihood x impact, scored with the shared Risk model. */
(() => {
  const W = 680, H = 400, M = { l: 116, r: 16, t: 14, b: 50 };
  const PW = W - M.l - M.r, PH = H - M.t - M.b;
  const x = p => M.l + p * PW;
  const y = c => M.t + (1 - c / 10) * PH;
  const IMPACT_TICKS = [[1, 'read'], [4, 'egress'], [5, 'write'], [8, 'create client'], [10, 'irreversible']];
  const ACTION = { LOW: '—', MEDIUM: 'Finding raised', HIGH: 'Require approval, 15 min', CRITICAL: 'Halt session, 30 min' };

  // Sessions come from the read API (docs/rest.md): every step is a tool call the gateway really decided on.
  const get = path => App.read(path);
  async function loadSession(summary) {
    const id = encodeURIComponent(summary.session_id);
    const [detail, trajectory, trace] = await Promise.all([get(`/sessions/${id}`),
      get(`/trajectories/session/${id}?view=full&kinds=tool_use&include_detections=false&limit=1000`),
      get(`/sessions/${id}/decisions?limit=1000`).catch(() => null)]);  // control-plane trace; older APIs lack it
    const steps = trajectory.segments.flatMap(g => g.steps).map(({ summary: a, event }) => {
      const d = event?.action_details || {};
      return { event_id: a.event_id, name: a.name, side_effect: a.side_effect, status: a.status, decision: a.decision, executed: a.executed,
        args: d.parameters, error: d.error, result: d.result, rule: a.triggered_rules?.[0], reason: a.reason_code, usage: Mock.usage(a) };
    });
    const s = Risk.newSession(summary.session_id, `${summary.case_id} · ${summary.agent_id}`);
    s.decisions = trace?.items || []; s.summary = trace?.summary || [];
    // The backend trajectory-risk plugin scores each step; its numbers win over the local recomputation
    const risk = Object.fromEntries(s.decisions.filter(d => d.plugin === 'trajectory-risk' && d.factors?.level).map(d => [d.trigger_event_id, d.factors]));
    const local = Risk.signalsFor(steps, detail.contract);
    let seen = {}, last;  // backend signals are session totals per name; a step's own are the increase
    local.forEach((own, k) => {
      const st = steps[k], held = st.status === 'pending_approval', f = risk[st.event_id];
      const signals = f ? Object.entries(f.signals || {}).flatMap(([n, c]) => Array(Math.max(0, c - (seen[n] || 0))).fill(n)) : own;
      if (f) { seen = f.signals || {}; last = f; }
      Risk.step(s, { event_id: st.event_id, name: st.name, signals, C: Risk.tools[st.name] ?? Risk.sideEffect[st.side_effect] ?? 1, executed: st.executed,
        trace: s.decisions.filter(d => d.trigger_event_id === st.event_id),
        args: st.args, ms: st.executed ? st.usage?.latency_ms : null, tokens: st.usage && { in: st.usage.input_tokens, out: st.usage.output_tokens, sim: st.usage.sim },
        decision: { verdict: VERDICT[st.decision] || (st.executed ? 'ALLOWED' : 'BLOCKED'), by: st.rule?.auditor, reason: st.rule?.rule_id || st.reason },
        result: st.error ? { status: 'error', summary: st.error }
          : st.executed ? { status: 'ok', data: st.result, redacted: st.status === 'redacted' }
          : { status: held ? 'held' : 'blocked', summary: held ? 'Not executed, waiting for approval' : 'Not executed, stopped at the gateway' } });
    });
    if (last) Object.assign(s, { P: last.failure_probability, loss: last.expected_loss, level: last.level.toUpperCase() });
    const v = s.decisions.findLast(d => d.plugin === 'outcome-verifier' && d.outcome === 'decided');
    if (v) s.verdict = { decision: v.decision, reasoning: v.reasoning, factors: v.factors || {} };
    return s;
  }
  // Keeps equal-impact dots apart; stable per session so a refresh does not move them
  const jitter = id => (Mock.draw(id) - .5) * .5;

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
  const describe = s => `${s.label}, likelihood ${s.P.toFixed(2)}, impact ${s.maxC}, expected loss ${s.loss.toFixed(2)}, level ${Risk.of(s)}, ${s.steps} steps`;
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
    // Tool time only for an executed step; tokens may be a simulated planner turn (js/mock.js)
    const foot = [st.ms != null && ms(st.ms), st.tokens && Mock.sim(`${fmtNum(st.tokens.in ?? 0)} in / ${fmtNum(st.tokens.out ?? 0)} out tokens`, st.tokens.sim)].filter(Boolean);
    if (foot.length) { const p = el('p', 'step-foot mono'); foot.forEach((f, i) => p.append(...(i ? [' · '] : []), f)); d.append(p); }

    return d;
  }
  // Session card: header with level, key figures, then one table row per step
  function card(s) {
    const lvl = Risk.of(s), head = el('div', 'pop-head'), f = s.verdict?.factors;
    head.append(el('strong', null, s.label), badge(lvl), ...(s.verdict ? [badge(code(s.verdict.decision))] : []));
    const facts = el('dl', 'pop-facts');
    for (const [k, v] of [['Likelihood P', s.P.toFixed(2)], ['Impact C', s.maxC], ['Expected loss', s.loss.toFixed(2)], ['Action', ACTION[lvl]],
      ...(s.verdict ? [['Outcome', `${code(s.verdict.decision)}${f?.checks_total != null ? ` · ${f.checks_passed}/${f.checks_total} checks` : ''}`]] : [])])
      facts.append(el('dt', null, k), el('dd', 'mono', String(v)));
    const t = el('table', 'pop-steps'), hr = el('tr');
    hr.append(...['#', 'Step', 'Impact', 'Status', 'Signals'].map(h => el('th', null, h)));
    t.append(el('thead'), el('tbody')); t.tHead.append(hr);
    t.tBodies[0].append(...s.log.map((st, i) => {
      const tr = el('tr', 'step'), sig = st.signals || [];
      tr.append(el('td', 'n mono', i + 1), el('td', 'mono', st.name), el('td', 'n mono', st.C),
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
    const cap = el('p', 'cap muted', `${s.steps} steps (actions taken)`);
    const d = el('div', `session-card lvl-${lvl}`); d.append(head, facts, cap, t);
    return d;
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
    const lvl = Risk.of(s);
    const c = node('circle', { cx: x(s.P), cy: y(Math.min(10, s.maxC + jitter(s.id))), r: 5 + Math.min(s.steps, 12) * .5,
      class: `pt lvl-${lvl}`, tabindex: 0, role: 'img', 'aria-label': describe(s) });
    hoverCard(c, s);
    return c;
  }

  function render(note) {
    svg.querySelectorAll('.pt').forEach(n => n.remove());
    for (const s of App.sessions) svg.append(point(s));
    if (!App.sessions.length) {
      const c = el('td', 'cap muted', note || 'No agent sessions yet.'); c.colSpan = 6;
      return $('#riskTable').replaceChildren(el('tr').appendChild(c).parentNode);
    }
    $('#riskTable').replaceChildren(...[...App.sessions].sort((a, b) => b.loss - a.loss).slice(0, 8).map(s => {
      const tr = el('tr'), lvl = Risk.of(s), b = el('td');
      b.append(badge(lvl));
      tr.append(el('td', 'cap', s.label), el('td', 'n mono cap', s.P.toFixed(2)), el('td', 'n mono cap', s.maxC),
        el('td', 'n mono cap', s.loss.toFixed(2)), b, el('td', 'cap muted', ACTION[lvl]));
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
  async function load() {
    let note;
    try {
      const { items } = await get('/sessions?limit=50');
      App.sessions = await Promise.all(items.map(loadSession));
    } catch (e) {
      App.sessions = []; note = `Sessions unavailable: ${e.message}`;
    }
    render(note);
    App.emit('sessions');
  }
  App.sessions = [];
  render('Loading sessions…');
  load(); setInterval(load, 30000);
})();
