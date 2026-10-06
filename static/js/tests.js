/* Tests: the guardrail suite, run on the server (web/suite.py), plus persisted independent outcome verification. */
(() => {
  // Area codes come from @pytest.mark.area in tests/control_layer; a code missing here is listed under its own name.
  const AREAS = [
    ['PII', 'Personal data', 'PESEL and IBAN never reach the model or leave by email'],
    ['SEC', 'Secrets', 'API keys and private keys are blocked or redacted'],
    ['INJ', 'Prompt injection', 'Direct and indirect overrides are blocked'],
    ['EXP', 'Exploit signatures', 'Malicious code, unsafe model files and blocklisted hosts'],
    ['BUD', 'Budget and rate', 'Tool-call, token and rate limits hold'],
    ['ACC', 'Access and scope', 'A wrong identity, tool or case is denied'],
    ['ACT', 'Action guards', 'No client is created unless every check passed'],
    ['CFG', 'Policy and feedback', 'Presets, pinned sessions, halt and strict mode'],
    ['LEG', 'Legitimate traffic', 'Clean requests go through'],
  ];
  let run = { status: 'not_run', cases: [] }, failure = null, timer = null;

  // A case has no expected result: it shows the control layer's final decision, with every call behind it on hover.
  const word = c => c.state === 'pending' ? 'QUEUED' : c.state === 'not_run' ? 'NOT RUN' : VERDICT[c.status] || 'NO DECISION';
  const pop = $('#pop');
  function status(c) {
    const b = badge(word(c)); b.tabIndex = 0;
    const calls = c.steps.map((s, i) => `${String(i + 1).padStart(2, '0')} ${s.kind} ${s.name}  ${VERDICT[s.decision] || s.decision}${s.reason ? '  ' + s.reason : ''}`);
    const text = [c.title, ...(calls.length ? calls : [c.state === 'done' ? 'no control-layer decision was made' : word(c).toLowerCase()]),
      ...(c.ms != null ? [`${fmtNum(c.ms)} ms`] : [])].join('\n');
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
  // One case: its final decision, its name and a button that runs only this case.
  function caseRow(c, running) {
    const row = el('div', 'case'), go = el('button', 'run', '▶'); go.type = 'button';
    go.title = 'Run this case'; go.setAttribute('aria-label', `Run ${c.title}`);
    go.disabled = running; go.onclick = () => refresh(true, c.id);
    row.append(status(c), el('span', 'name', c.title), go);
    return row;
  }

  function render() {
    const cases = run.cases, running = run.status === 'running';
    const done = cases.filter(c => c.state === 'done'), never = !done.length && !running;
    const count = w => done.filter(c => word(c) === w).length, alerts = count('ALERT');
    const k = (label, value, sub, tone) => { const d = el('div', 'kpi'); d.append(el('span', 'label', label), el('span', 'value' + (tone ? ' v-' + tone : ''), value), el('span', 'sub', sub)); return d; };
    const tile = (label, w, tone) => k(label, never ? '–' : fmtNum(count(w)), never ? '' : 'final decision', count(w) ? tone : null);
    $('#tKpis').replaceChildren(
      k('Cases', cases.length ? fmtNum(cases.length) : '–', never ? 'not run yet' : running ? 'running…' : `${done.length} run`),
      k('Allowed', never ? '–' : fmtNum(count('ALLOWED')), never ? '' : alerts ? `plus ${alerts} with an alert` : 'final decision', count('ALLOWED') ? 'allow' : null),
      tile('Blocked', 'BLOCKED', 'block'), tile('Redacted', 'REDACTED', 'warn'), tile('Held', 'HELD', 'hold'),
      failure || run.error ? k('Last run', 'error', failure || run.error, 'block')
        : k('Last run', running ? 'now' : run.finished_at ? fmtTime(new Date(run.finished_at)) : '–',
            run.config ? `${run.duration_ms != null ? (run.duration_ms / 1000).toFixed(1) + ' s · ' : ''}config ${label(run.config.name)}` : 'press Run suite'),
    );
    const extra = [...new Set(cases.map(c => c.area))].filter(a => !AREAS.some(([code]) => code === a)).map(a => [a, a, '']);
    $('#families').replaceChildren(...[...AREAS, ...extra].map(([code, name, proves]) => {
      const tr = el('tr'), c = el('td'); c.append(...cases.filter(m => m.area === code).map(m => caseRow(m, running)));
      const area = el('td', null, name); area.append(el('span', 'sub cap muted', proves));
      tr.append(area, c);
      return tr;
    }));
    $('#runSuite').disabled = running; $('#runSuite').textContent = running ? 'Running…' : 'Run suite';
  }

  // The server keeps one latest run for every browser; poll it while it is going so cases fill in as they
  // finish. start posts a new run: the whole suite, or only the case with the given id.
  async function refresh(start, caseId) {
    clearTimeout(timer);
    const post = { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(caseId ? { case: caseId } : {}) };
    try { run = await App.request('/api/suite/runs' + (start === true ? '' : '/latest'), start === true ? post : { cache: 'no-store' }); failure = null; }
    catch (e) { failure = e.message; }
    render();
    if (run.status === 'running' && !failure) timer = setTimeout(refresh, 700);
  }
  $('#runSuite').onclick = () => refresh(true);

  /* Persisted outcome checks. A session without verification is never marked successful. */
  let outcomes = [], selectedSession = null, selection = 0, outcomeLoading = false, outcomeCursor = null;
  const CHECK = { PASS: 'Verified', FAIL: 'Failed', INCOMPLETE: 'Evidence incomplete' };
  function renderOutcomes(note) {
    const list = $('#scenarios');
    list.replaceChildren(...outcomes.map(s => {
      const li = el('li'), b = el('button'); b.type = 'button';
      const t = el('span', null, `${s.case_id || 'No case'} · ${s.agent_id || 'Unknown agent'}`);
      t.append(el('span', 'sub', s.session_id));
      b.append(t, badge(s.verification_status ? code(s.verification_status) : 'NOT VERIFIED'));
      b.setAttribute('aria-pressed', s.session_id === selectedSession);
      b.onclick = () => openOutcome(s.session_id);
      li.append(b); return li;
    }));
    if (note) list.append(el('li', 'cap muted', note));
    else if (!outcomes.length) list.append(el('li', 'cap muted', 'No recorded sessions yet. Run a session to see its verification checks here.'));
    if (outcomeCursor) {
      const li = el('li'), more = el('button', 'btn ghost', 'Load more sessions'); more.type = 'button';
      more.onclick = () => loadOutcomes(true); more.disabled = outcomeLoading; li.append(more); list.append(li);
    }
  }
  async function openOutcome(id) {
    selectedSession = id; const version = ++selection;
    renderOutcomes(); $('#scenOut').hidden = false;
    const verdict = $('#sVerdict'); verdict.hidden = false; verdict.className = 'verdict';
    $('#sWord').textContent = 'Loading verification…'; $('#sReason').textContent = id;
    $('#evidence').replaceChildren(); $('#timeline').replaceChildren();
    try {
      const v = await App.read(`/sessions/${encodeURIComponent(id)}/verification`);
      if (version !== selection) return;
      const checks = v.checks || [], word = v.verification_status ? code(v.verification_status) : 'NOT VERIFIED';
      verdict.className = 'verdict in ' + (TONE[word] || 'neutral');
      $('#sWord').textContent = word;
      const passed = checks.filter(c => c.status === 'PASS').length;
      $('#sReason').textContent = v.verification_status
        ? `${passed} of ${checks.length} independent checks passed${v.verified_at ? ' · ' + fmtTime(new Date(v.verified_at)) : ''}.`
        : 'No independent verification has been recorded for this session.';
      $('#evidence').replaceChildren(...checks.map(c => {
        const tr = el('tr', c.status === 'FAIL' ? 'bad' : '');
        const status = el('td'); status.append(badge(c.status));
        tr.append(el('td', 'mono cap', c.id), el('td', 'cap', c.evidence_source || '—'), el('td', 'cap', c.detail || '—'), status);
        return tr;
      }));
      $('#timeline').replaceChildren(...checks.map(c => {
        const li = el('li'); li.append(el('span', 'mono', c.id), badge(c.status), el('span', 'check', CHECK[c.status] || c.status)); return li;
      }));
    } catch (e) {
      if (version !== selection) return;
      $('#sWord').textContent = 'Verification unavailable'; $('#sReason').textContent = e.message;
      verdict.className = 'verdict block';
    }
  }
  async function loadOutcomes(more = false) {
    if (outcomeLoading) return;
    outcomeLoading = true;
    let note;
    if (!more) renderOutcomes('Loading recorded sessions…');
    try {
      const query = more && outcomeCursor ? '&cursor=' + encodeURIComponent(outcomeCursor) : '';
      const page = await App.read('/sessions?limit=50' + query);
      outcomes = more ? [...outcomes, ...page.items.filter(s => !outcomes.some(old => old.session_id === s.session_id))] : page.items;
      outcomeCursor = page.next_cursor;
      renderOutcomes();
      if (!selectedSession && outcomes.length) await openOutcome(outcomes[0].session_id);
    } catch (e) { note = `Recorded outcomes unavailable: ${e.message}`; }
    finally { outcomeLoading = false; renderOutcomes(note); }
  }

  render(); renderOutcomes();
  App.on('tab', t => { if (t === 'tests') { refresh(); loadOutcomes(); } });
})();
