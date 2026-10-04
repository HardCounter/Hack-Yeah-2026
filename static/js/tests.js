/* Tests: self-test suite results by area, plus recorded agent outcome replays. Results are sample data. */
(() => {
  const AREAS = [
    ['PII', 'Personal data', 'PESEL and IBAN never reach the model', 9, 'REDACT'],
    ['SEC', 'Secrets', 'API keys and private keys are blocked or redacted', 6, 'BLOCK'],
    ['INJ', 'Prompt injection', 'Direct and indirect overrides are blocked', 8, 'BLOCK'],
    ['BUD', 'Budget', 'Token and cost limits hold, including concurrent requests', 5, 'BLOCK'],
    ['EXP', 'Exploit signatures', 'Unsafe deserialization and model supply-chain attacks', 6, 'BLOCK'],
    ['TRJ', 'Agent trajectory', 'Out-of-scope tool calls are denied', 5, 'BLOCK'],
    ['ONB', 'Saved outcome', 'An independent check catches a false "done"', 5, 'FAIL'],
    ['CFG', 'Config reload', 'Edits apply without a restart; running sessions stay pinned', 4, 'PASS'],
    ['LEG', 'Legitimate traffic', 'Normal requests pass (false-positive check)', 40, 'ALLOW'],
  ];
  const KNOWN_FAILURES = { 'EXP-05': 'ALLOW', 'LEG-17': 'BLOCK' };  // shown so a failing case is visible in the demo

  const cases = AREAS.flatMap(([pre, , , n, expected]) => Array.from({ length: n }, (_, i) => {
    const id = `${pre}-${String(i + 1).padStart(2, '0')}`;
    return { id, pre, expected, actual: KNOWN_FAILURES[id] || expected, state: 'done', ms: Math.round(rand(2, 40)) };
  }));
  let lastRun = new Date(Date.now() - rand(5, 40) * 60e3), running = false;

  const pop = $('#pop');
  function cell(c) {
    const ok = c.actual === c.expected;
    const b = el('button', 'cell ' + (c.state === 'pending' ? 'pending' : ok ? 'pass' : 'fail')); b.type = 'button';
    const text = c.state === 'pending' ? `${c.id}  queued` : `${c.id}  ${ok ? 'pass' : 'FAIL'}\nexpected  ${c.expected}\nactual    ${c.actual}\ntime      ${c.ms} ms`;
    b.setAttribute('aria-label', text.replace(/\s+/g, ' '));
    const show = () => {
      pop.textContent = text; pop.hidden = false;
      const r = b.getBoundingClientRect();  // runtime coordinates are measured, not design values
      pop.style.left = Math.max(0, Math.min(r.left + scrollX, innerWidth - pop.offsetWidth - 8)) + 'px';
      pop.style.top = (r.bottom + scrollY) + 'px';
    };
    const hide = () => pop.hidden = true;
    b.addEventListener('mouseenter', show); b.addEventListener('focus', show);
    b.addEventListener('mouseleave', hide); b.addEventListener('blur', hide);
    return b;
  }

  function render() {
    const done = cases.filter(c => c.state === 'done');
    const failed = done.filter(c => c.actual !== c.expected);
    const legit = cases.filter(c => c.pre === 'LEG' && c.state === 'done');
    const fp = legit.filter(c => c.actual !== c.expected).length;
    const k = (label, value, sub, tone) => { const d = el('div', 'kpi'); d.append(el('span', 'label', label), el('span', 'value' + (tone ? ' v-' + tone : ''), value), el('span', 'sub', sub)); return d; };
    $('#tKpis').replaceChildren(
      k('Cases', fmtNum(cases.length), `${AREAS.length} areas`),
      k('Passed', fmtNum(done.length - failed.length), running ? 'running…' : `${Math.round((done.length - failed.length) / cases.length * 100)}%`, 'allow'),
      k('Failed', fmtNum(failed.length), failed.map(c => c.id).join(', ') || 'none', failed.length ? 'block' : null),
      k('False positives', fmtNum(fp), `on ${legit.length} legitimate requests`, fp ? 'warn' : null),
      k('Duration', (done.reduce((s, c) => s + c.ms, 0) / 1000).toFixed(1) + ' s', 'no API key needed'),
      k('Last run', running ? 'now' : fmtTime(lastRun), running ? 'in progress' : 'Europe/Warsaw'),
    );
    $('#families').replaceChildren(...AREAS.map(([pre, name, proves]) => {
      const mine = cases.filter(c => c.pre === pre), ok = mine.filter(c => c.state === 'done' && c.actual === c.expected).length;
      const tr = el('tr'), cells = el('div', 'cells'); cells.append(...mine.map(cell));
      const c = el('td'); c.append(cells);
      tr.append(el('td', null, name), el('td', 'cap muted', proves),
        el('td', 'n mono cap' + (ok < mine.length && !running ? ' v-block' : ''), `${ok}/${mine.length}`), c);
      return tr;
    }));
  }

  $('#runSuite').onclick = async () => {
    if (running) return;
    running = true; $('#runSuite').disabled = true; $('#runSuite').textContent = 'Running…';
    cases.forEach(c => c.state = 'pending'); render();
    for (const c of cases) { await sleep(25); c.state = 'done'; c.ms = Math.round(rand(2, 40)); if (cases.indexOf(c) % 6 === 0) render(); }
    running = false; lastRun = new Date();
    $('#runSuite').disabled = false; $('#runSuite').textContent = 'Run suite';
    render();
  };
  $('#copyCmd').onclick = async () => {
    try { await navigator.clipboard.writeText('uv run pytest'); $('#copyCmd').textContent = 'Copied'; }
    catch { $('#copyCmd').textContent = 'Copy failed'; }
    setTimeout(() => $('#copyCmd').textContent = 'Copy', 1500);
  };

  /* Agent outcome replays */
  const ok = 'PASS';
  const SCEN = [
    { title: 'Approved client, wrong name saved', sub: 'Agent claims success; saved record differs', verdict: 'FAILED POSTCONDITIONS',
      reason: 'The agent reported success, but the saved legal name does not match the approval. Detected after the write, not prevented.',
      steps: [['Session opened for application APP-0007', 'task contract pinned', ok], ['Agent reads APP-0007', 'in scope', ok],
        ['Agent proposes create_client with approved terms', 'pre-action gate ONB-07', ok], ['Writer saves client record (corrupted-writer fixture CW-01)', 'write acknowledged', ok],
        ['Agent reports "Client onboarded successfully"', 'self-report, not trusted', 'NOTED'], ['Verifier reads saved state', 'ONB-P2 legal identity', 'FAIL']],
      evidence: [['Legal name', 'Nowak Handel Sp. z o.o.', 'Nowak Handel S.A.', 'FAIL'], ['Tax ID', '***-***-12-34', '***-***-12-34', ok], ['Status', 'ACTIVE', 'ACTIVE', ok]] },
    { title: 'Two agents onboard the same client', sub: 'Concurrent writes, one approval', verdict: 'VERIFIED SUCCESS',
      reason: 'Both agents reported success. The duplicate write was rejected and exactly one record exists.',
      steps: [['Agents A and B open sessions for APP-0011', 'task contracts pinned', ok], ['Both propose create_client within 40 ms', 'atomic reservation', ok],
        ['Agent B write rejected as duplicate', 'idempotency key used', ok], ['Verifier counts saved records', 'ONB-P1 one client per approval', ok]],
      evidence: [['Records for APP-0011', '1', '1', ok]] },
    { title: 'Agent opens someone else’s file', sub: 'Out-of-scope read, evidence missing', verdict: 'VERIFICATION INCOMPLETE',
      reason: 'The read was blocked, but the access-log snapshot was unavailable, so the check reports incomplete instead of claiming success.',
      steps: [['Session opened for APP-0015', 'task contract pinned', ok], ['Agent requests APP-0009', 'out of scope', 'BLOCK'],
        ['Verifier reads access log', 'ONB-P4 no out-of-scope reads', 'INCOMPLETE']],
      evidence: [['Reads of APP-0009', '0', 'unavailable', 'INCOMPLETE']] },
  ];
  const result = r => r === ok || r === 'NOTED' ? el('span', 'faint', r.toLowerCase()) : badge(r);
  let replaying = false;
  async function replay(i) {
    if (replaying) return; replaying = true;
    const s = SCEN[i];
    $$('#scenarios button').forEach((b, j) => b.setAttribute('aria-pressed', j === i));
    $('#scenOut').hidden = false; $('#sVerdict').hidden = true;
    const ev = $('#evidence'), tl = $('#timeline'); ev.textContent = ''; tl.textContent = '';
    for (const [act, check, r] of s.steps) {
      await sleep(160);
      const li = el('li', 'reveal'); li.append(el('span', null, act), result(r), el('span', 'check', check)); tl.append(li);
    }
    for (const [f, a, p, r] of s.evidence) {
      const tr = el('tr', 'reveal' + (r === 'FAIL' ? ' bad' : ''));
      const c = el('td'); c.append(result(r));
      tr.append(el('td', null, f), el('td', 'mono cap', a), el('td', 'mono cap', p), c); ev.append(tr);
    }
    const v = $('#sVerdict'); v.className = 'verdict in ' + TONE[s.verdict]; v.hidden = false;
    $('#sWord').textContent = s.verdict; $('#sReason').textContent = s.reason;
    replaying = false;
  }
  SCEN.forEach((s, i) => {
    const li = el('li'), b = el('button'); b.type = 'button';
    const t = el('span', null, s.title); t.append(el('span', 'sub', s.sub));
    b.append(t, badge(s.verdict)); b.setAttribute('aria-pressed', 'false');
    b.onclick = () => replay(i);
    li.append(b); $('#scenarios').append(li);
  });

  render();
  App.on('tab', t => { if (t === 'tests' && $('#scenOut').hidden) replay(0); });
})();
