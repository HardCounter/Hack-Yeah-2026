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

  const samples = [documentedSession(), ...Array.from({ length: 17 }, (_, i) => sampleSession(i))];
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
    for (const p of [0, .25, .5, .75, 1]) {
      svg.append(node('line', { x1: x(p), x2: x(p), y1: M.t, y2: M.t + PH, class: 'grid-line' }),
        node('text', { x: x(p), y: M.t + PH + 16, 'text-anchor': 'middle', class: 'tick' }, p.toFixed(2)));
    }
    for (const [c, name] of IMPACT_TICKS) {
      svg.append(node('line', { x1: M.l, x2: M.l + PW, y1: y(c), y2: y(c), class: 'grid-line' }),
        node('text', { x: M.l - 6, y: y(c) + 4, 'text-anchor': 'end', class: 'tick' }, `${c} ${name}`));
    }
    svg.append(node('line', { x1: M.l, x2: M.l + PW, y1: M.t + PH, y2: M.t + PH, class: 'axis' }),
      node('line', { x1: M.l, x2: M.l, y1: M.t, y2: M.t + PH, class: 'axis' }),
      ...isoCurve(3, 'one step → medium'), ...isoCurve(8, 'one step → high'),
      node('text', { x: M.l + PW / 2, y: H - 8, 'text-anchor': 'middle', class: 'axis-title' }, 'Likelihood the session has gone wrong (P)'),
      node('text', { x: 14, y: M.t + PH / 2, 'text-anchor': 'middle', transform: `rotate(-90 14 ${M.t + PH / 2})`, class: 'axis-title' }, 'Impact of riskiest executed step (C)'));
  }

  const pop = $('#pop');
  const describe = s => `${s.label}\nlikelihood P  ${s.P.toFixed(2)}\nimpact C      ${s.maxC}\nexpected loss ${s.loss.toFixed(2)}\nlevel         ${Risk.level(s.loss)}\naction        ${ACTION[Risk.level(s.loss)]}\n\n${s.steps} steps (actions taken):\n`
    + s.log.map((st, i) => `${String(i + 1).padStart(2)}. ${st.name.padEnd(22)} impact ${String(st.C).padStart(2)}${st.executed ? '' : '  BLOCKED'}${st.signals.length ? '  ! ' + st.signals.join(', ').replace(/_/g, ' ') : ''}`).join('\n');
  function point(s, mine) {
    const lvl = Risk.level(s.loss);
    const c = node('circle', { cx: x(s.P), cy: y(Math.min(10, s.maxC + (mine ? 0 : jitter[s.id]))), r: 5 + Math.min(s.steps, 12) * .5,
      class: `pt lvl-${lvl}${mine ? ' you' : ''}`, tabindex: 0, role: 'img', 'aria-label': describe(s).replace(/\s+/g, ' ') });
    const show = () => {
      pop.textContent = describe(s); pop.hidden = false;
      const r = c.getBoundingClientRect();  // runtime coordinates are measured, not design values
      pop.style.left = Math.max(0, Math.min(r.left + scrollX, innerWidth - pop.offsetWidth - 8)) + 'px';
      pop.style.top = (r.bottom + scrollY) + 'px';
    };
    const hide = () => pop.hidden = true;
    c.addEventListener('mouseenter', show); c.addEventListener('focus', show);
    c.addEventListener('mouseleave', hide); c.addEventListener('blur', hide);
    return c;
  }

  function render() {
    svg.querySelectorAll('.pt, .you-label').forEach(e => e.remove());
    const me = App.session, all = me.steps ? [...samples, me] : samples;
    for (const s of samples) svg.append(point(s, false));
    if (me.steps) {
      svg.append(point(me, true), node('text', { x: x(me.P) + 12, y: y(me.maxC) - 10, class: 'you-label' }, 'You'));
    }
    $('#riskTable').replaceChildren(...[...all].sort((a, b) => b.loss - a.loss).slice(0, 8).map(s => {
      const tr = el('tr', s === me ? 'me' : null), lvl = Risk.level(s.loss), b = el('td');
      b.append(badge(lvl));
      tr.append(el('td', 'cap', s === me ? 'You (Prompt it)' : s.label), el('td', 'n mono cap', s.P.toFixed(2)), el('td', 'n mono cap', s.maxC),
        el('td', 'n mono cap', s.loss.toFixed(2)), b, el('td', 'cap muted', ACTION[lvl]));
      return tr;
    }));
  }

  drawFrame();
  // Legend: what each colour means and what happens, then the reading notes on their own lines
  const MEANING = { LOW: 'under 3, no action', MEDIUM: '3 or more, flagged for review', HIGH: '8 or more, a human must approve', CRITICAL: '15 or more, session stopped' };
  const swatch = (cls, text) => { const s = el('span'); s.append(el('i', cls), text); return s; };
  $('#riskLegend').append(
    ...Risk.levels.slice().reverse().map(([lvl]) => swatch(lvl, `${lvl[0] + lvl.slice(1).toLowerCase()}: ${MEANING[lvl]}`)),
    swatch('ring', 'You: your session from Prompt it'),
    el('p', 'legend-note', 'Colour shows how much risk a session has built up over all its steps (its expected loss). Bigger dots took more steps.'),
    el('p', 'legend-note', 'A step is one action the agent took, such as reading an application, a sanctions check or creating a client. For "You", each prompt you send is a step. Hover a dot to see its steps.'),
    el('p', 'legend-note', 'Dashed lines: a single step to the right of a line is risky enough on its own to make the session medium or high.'));
  render();
  App.on('event', e => { if (e.source === 'session') render(); });
})();
