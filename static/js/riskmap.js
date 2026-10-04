/* Risk map (Metrics tab): agent sessions on likelihood x impact, scored with the shared Risk model. */
(() => {
  const W = 680, H = 400, M = { l: 116, r: 16, t: 14, b: 50 };
  const PW = W - M.l - M.r, PH = H - M.t - M.b;
  const x = p => M.l + p * PW;
  const y = c => M.t + (1 - c / 10) * PH;
  const IMPACT_TICKS = [[1, 'read'], [4, 'egress'], [5, 'write'], [8, 'create client'], [10, 'irreversible']];
  const ACTION = { LOW: '—', MEDIUM: 'Finding raised', HIGH: 'Require approval, 15 min', CRITICAL: 'Halt session, 30 min' };

  // Seeded PRNG so the sample sessions are the same on every load
  let seed = 2026;
  const rnd = () => { seed |= 0; seed = seed + 0x6D2B79F5 | 0; let t = Math.imul(seed ^ seed >>> 15, 1 | seed); t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t; return ((t ^ t >>> 14) >>> 0) / 4294967296; };
  const choose = xs => xs[Math.floor(rnd() * xs.length)];

  function sampleSession(i) {
    const app = `APP-${String(11 + i * 3).padStart(4, '0')}`;
    const s = Risk.newSession(`ses-${app}`, `${app} · agent ${choose(['A', 'B', 'C'])}`);
    const tools = ['read_application', 'read_documents', 'extract_fields', 'check_registry', 'screen_sanctions', 'compute_risk',
      'fetch_url', 'request_more_docs', 'create_client', 'reject_application', 'send_email', 'run_code'];
    const done = new Set(), n = 3 + Math.floor(rnd() * 7);
    for (let k = 0; k < n; k++) {
      const tool = k < 2 ? 'read_application' : choose(tools), signals = [];
      if (rnd() < .07) signals.push('out_of_scope_target');
      if (rnd() < .10) signals.push('gateway_redacted');
      if (tool === 'run_code' || rnd() < .06) signals.push('gateway_blocked');
      if (tool === 'create_client' && !done.has('screen_sanctions')) signals.push('missing_prerequisite');
      if (tool === 'create_client' && done.has('create_client')) signals.push('repeated_side_effect');
      if (done.has('fetch_url')) signals.push('untrusted_external_content');
      const executed = !signals.includes('gateway_blocked');
      Risk.step(s, { name: tool, signals, C: Risk.tools[tool], executed });
      if (executed) done.add(tool);
    }
    return s;
  }

  // The worked example from docs/trajectory-risk-model.md (risky_onboarding.jsonl)
  function documentedSession() {
    const s = Risk.newSession('ses-APP-0003', 'APP-0003 · replay');
    [['read_application', []], ['read_application', ['out_of_scope_target']], ['read_documents', []], ['extract_fields', []],
      ['check_registry', []], ['create_client', ['missing_prerequisite']], ['create_client', ['missing_prerequisite', 'repeated_side_effect']],
      ['run_code', ['gateway_blocked']], ['delete_client', ['out_of_contract_tool', 'out_of_scope_target']]]
      .forEach(([tool, signals]) => Risk.step(s, { name: tool, signals, C: Risk.tools[tool], executed: !signals.includes('gateway_blocked') }));
    return s;
  }

  const samples = App.sessions = [documentedSession(), ...Array.from({ length: 17 }, (_, i) => sampleSession(i))];
  const jitter = Object.fromEntries(samples.map(s => [s.id, (rnd() - .5) * .5]));  // keeps equal-impact dots apart

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
  const describe = s => `${s.label}, likelihood ${s.P.toFixed(2)}, impact ${s.maxC}, expected loss ${s.loss.toFixed(2)}, level ${Risk.level(s.loss)}, ${s.steps} steps`;
  const td = (cls, ...kids) => { const c = el('td', cls); c.append(...kids); return c; };
  // Session card: header with level, key figures, then one table row per step
  function card(s) {
    const lvl = Risk.level(s.loss), head = el('div', 'pop-head');
    head.append(el('strong', null, s.label), badge(lvl));
    const facts = el('dl', 'pop-facts');
    for (const [k, v] of [['Likelihood P', s.P.toFixed(2)], ['Impact C', s.maxC], ['Expected loss', s.loss.toFixed(2)], ['Action', ACTION[lvl]]])
      facts.append(el('dt', null, k), el('dd', 'mono', String(v)));
    const t = el('table', 'pop-steps'), hr = el('tr');
    hr.append(...['#', 'Step', 'Impact', 'Status', 'Signals'].map(h => el('th', null, h)));
    t.append(el('thead'), el('tbody')); t.tHead.append(hr);
    t.tBodies[0].append(...s.log.map((st, i) => {
      const tr = el('tr');
      tr.append(el('td', 'n mono', i + 1), el('td', 'mono', st.name), el('td', 'n mono', st.C),
        td(null, badge(st.executed ? 'ALLOWED' : 'BLOCKED')),
        el('td', st.signals.length ? 'v-warn' : 'faint', st.signals.join(', ').replace(/_/g, ' ') || '—'));
      return tr;
    }));
    const cap = el('p', 'cap muted', `${s.steps} steps (actions taken)`);
    pop.className = `pop lvl-${lvl}`;
    pop.replaceChildren(head, facts, cap, t);
  }
  // Same card for a dot on the map and a row in the session table
  function hoverCard(target, s) {
    const show = () => {
      card(s); pop.hidden = false;
      const r = target.getBoundingClientRect();  // runtime coordinates are measured, not design values
      pop.style.left = Math.max(0, Math.min(r.left + scrollX, innerWidth - pop.offsetWidth - 8)) + 'px';
      pop.style.top = (r.bottom + scrollY) + 'px';
    };
    const hide = () => pop.hidden = true;
    target.addEventListener('mouseenter', show); target.addEventListener('focus', show);
    target.addEventListener('mouseleave', hide); target.addEventListener('blur', hide);
  }
  function point(s) {
    const lvl = Risk.level(s.loss);
    const c = node('circle', { cx: x(s.P), cy: y(Math.min(10, s.maxC + jitter[s.id])), r: 5 + Math.min(s.steps, 12) * .5,
      class: `pt lvl-${lvl}`, tabindex: 0, role: 'img', 'aria-label': describe(s) });
    hoverCard(c, s);
    return c;
  }

  function render() {
    for (const s of samples) svg.append(point(s));
    $('#riskTable').replaceChildren(...[...samples].sort((a, b) => b.loss - a.loss).slice(0, 8).map(s => {
      const tr = el('tr'), lvl = Risk.level(s.loss), b = el('td');
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
  render();
})();
