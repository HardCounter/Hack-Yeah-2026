/* Metrics: KPIs, controls, gateway performance, budget and the audit log, read from the read API (docs/rest.md). */
(() => {
  const POLL_MS = 5000;
  const EVALUATED = 'kinds=prompt,tool_use,egress';  // actions the gateway decided on; session and control events have no decision
  let security = null, usage = null, perf = null, budget = null, actions = [], reachable = true;
  let aggregates = true;  // false once the API answers 501 for /metrics/*: the figures are then derived here

  const emptyRow = (text, span) => { const tr = el('tr'), td = el('td', 'empty', text); td.colSpan = span; tr.append(td); return tr; };
  const pct = (a, b) => a && b ? Math.round(a / b * 100) : 0;
  const ms = v => v == null ? '—' : (v < 10 ? v.toFixed(1) : Math.round(v)) + ' ms';
  const usd = v => v == null ? '—' : '$' + v.toFixed(v < 1 ? 4 : 2);
  const verdict = a => VERDICT[a.decision] || '—';
  const rule = a => a.decision === 'ALLOW' ? '—' : a.triggered_rules[0]?.rule_id || a.reason_code || '—';
  const what = a => a.kind === 'prompt' ? `prompt · ${a.name}` : a.name;
  const time = a => new Date(a.ts);
  const sum = xs => xs.reduce((a, b) => a + b, 0);
  const quantiles = xs => {
    const s = [...xs].sort((a, b) => a - b), q = p => s.length ? s[Math.min(s.length - 1, Math.floor(p * s.length))] : null;
    return { p50: q(.5), p95: q(.95), p99: q(.99) };
  };
  // The store-backed read API defers the /metrics aggregates (501, docs/rest.md §3), so the same figures are
  // derived from the action list it does serve. A list row names only the auditors that acted, so the
  // per-auditor run counts and latencies stay empty.
  function derive(rows) {
    const by = { ALLOW: 0, BLOCK: 0, REDACT: 0, REQUIRE_APPROVAL: 0, ALERT: 0 }, acted = {};
    for (const a of rows) { by[a.decision]++; for (const t of a.triggered_rules) acted[t.auditor] = (acted[t.auditor] || 0) + 1; }
    const ran = rows.filter(a => a.executed).map(a => a.usage.latency_ms), gateway = rows.map(a => a.interception_overhead_ms ?? 0);
    return {
      security: { actions: { by_decision: by } },
      usage: { totals: { total_tokens: sum(rows.map(a => a.usage.total_tokens)), cost_usd: sum(rows.map(a => a.usage.cost_usd)), source: 'estimated' } },
      perf: { actions_evaluated: rows.length, interception_overhead_ms: quantiles(gateway), backend_latency_ms: quantiles(ran),
        overhead_share: ran.length ? sum(gateway) / (sum(gateway) + sum(ran)) : null,
        by_auditor: Object.entries(acted).map(([auditor, n]) => ({ auditor, acted: n })) },
    };
  }

  function kpi(view, label, value, sub, tone) {
    const d = el('a', 'kpi'); d.href = '#metrics/' + view; d.title = 'Show the records behind this number';
    const v = el('span', 'value' + (tone ? ' v-' + tone : ''), value);
    d.append(el('span', 'label', label), v, el('span', 'sub', sub));
    return d;
  }

  function render(fresh) {
    const by = security?.actions.by_decision, n = perf?.actions_evaluated ?? 0, num = v => security ? fmtNum(v) : '—';
    // A task is one agent session on the risk map; it succeeded if it was not halted at critical risk
    const tasks = { total: App.sessions.length }, tasksOk = App.sessions.filter(s => Risk.level(s.loss) !== 'CRITICAL').length;
    $('#kpis').replaceChildren(
      kpi('requests', 'Requests', perf ? fmtNum(n) : '—', aggregates ? 'prompts and tool calls, last 24 h' : 'prompts and tool calls recorded'),
      kpi('blocked', 'Blocked', num(by?.BLOCK), `requests · ${pct(by?.BLOCK, n)}% of all`, 'block'),
      kpi('redacted', 'Redacted', num(by?.REDACT), `requests · ${pct(by?.REDACT, n)}% forwarded clean`, 'warn'),
      kpi('held', 'Held for approval', num(by?.REQUIRE_APPROVAL), 'requests awaiting a human', 'hold'),
      kpi('tasks', 'Tasks succeeded', tasks.total ? pct(tasksOk, tasks.total) + '%' : '—', `${tasksOk} of ${tasks.total} agent tasks`, 'allow'),
      kpi('spend', 'Model spend', usage ? usd(usage.totals.cost_usd) : '—',
        usage ? `${fmtNum(usage.totals.total_tokens)} tokens${usage.totals.source === 'reported' ? '' : ' · estimate'}` : 'tokens'),
      kpi('latency', 'Gateway p95', ms(perf?.interception_overhead_ms.p95), perf ? `all checks · p50 ${ms(perf.interception_overhead_ms.p50)}` : 'all checks'),
    );

    const cells = (name, ...values) => { const tr = el('tr'); tr.append(el('td', null, name), ...values.map(v => el('td', 'n mono cap', v))); return tr; };
    if (perf) {
      const det = perf.by_method?.deterministic, sem = perf.by_method?.semantic;
      const row = (name, runs, p) => cells(name, fmtNum(runs), ms(p.p50), ms(p.p95), ms(p.p99));
      $('#perfTable').replaceChildren(...(det ? [row('Deterministic checks', det.runs, det), row('AI check', sem.runs, sem)] : []),
        row('Gateway total', perf.actions_evaluated, perf.interception_overhead_ms), row('Model and tool calls', perf.actions_evaluated, perf.backend_latency_ms));
      $('#perfTxt').replaceChildren(sem ? `AI check skipped on ${fmtNum(sem.skipped)} requests that a deterministic control had already blocked.`
        : 'Computed from the recorded actions; the split by check type is not served for recorded evidence.', el('br'),
        perf.overhead_share == null ? 'No executed requests in this window.' : `The gateway accounts for ${Math.round(perf.overhead_share * 100)}% of end-to-end time.`);
      const count = v => v == null ? '—' : fmtNum(v);
      $('#ctlTable').replaceChildren(...(perf.by_auditor.length ? perf.by_auditor.map(a =>
        cells(a.auditor + (a.method === 'semantic' ? ' · AI' : ''), count(a.runs), count(a.acted), ms(a.p50), ms(a.p95)))
        : [emptyRow('No control has acted yet.', 5)]));
    } else {
      $('#perfTable').replaceChildren(emptyRow('Read API unreachable.', 5));
      $('#ctlTable').replaceChildren(emptyRow('Read API unreachable.', 5));
      $('#perfTxt').textContent = '';
    }

    const tokens = budget?.budgets?.find(b => b.resource === 'tokens'), calls = budget?.budgets?.find(b => b.resource === 'tool_calls');
    if (tokens) {
      const p = Math.min(tokens.used / tokens.limit * 100, 100), fill = $('#budFill');
      fill.style.width = p + '%';
      fill.className = 'fill' + (tokens.exceeded ? ' block' : p > 80 ? ' warn' : '');
      $('#budMeter').setAttribute('aria-valuemax', tokens.limit); $('#budMeter').setAttribute('aria-valuenow', tokens.used);
      $('#budTxt').textContent = `${fmtNum(tokens.used)} / ${fmtNum(tokens.limit)} tokens`;
      $('#costTxt').replaceChildren(`Session ${budget.session_id}` + (calls ? `: ${calls.used} of ${calls.limit} tool calls` : '') + `, ${usd(budget.usage.cost_usd)} spent (estimate).`,
        el('br'), 'A request that would cross a limit is blocked before the model is called.');
    }

    const filter = $('#mFilter').value;
    const shown = actions.filter(a => !filter || a.decision === filter).slice(0, 25);  // full history is in the export
    $('#audit').replaceChildren(...(shown.length ? shown.map((a, i) => {
      const tr = link(`#metrics/requests/${encodeURIComponent(a.event_id)}`);
      if (fresh && i === 0 && a === actions[0]) tr.className = 'fresh';
      const t = el('td', 'mono cap', fmtTime(time(a))); t.title = a.ts;
      tr.append(t, td(null, badge(verdict(a))), el('td', 'mono cap', rule(a)), el('td', 'mono cap', what(a)), el('td', 'mono cap muted', a.session_id));
      return tr;
    }) : [emptyRow(reachable ? 'No decisions match this filter.' : 'Read API unreachable.', 5)]));
  }

  $('#mFilter').onchange = () => render(false);

  /* Export */
  function download(name, text, type) {
    const a = el('a'); a.href = URL.createObjectURL(new Blob([text], { type }));
    a.download = name; a.click(); URL.revokeObjectURL(a.href);
  }
  const jsonl = rows => rows.map(r => JSON.stringify(r)).join('\n') + '\n';
  const json = r => JSON.stringify(r, null, 2) + '\n';
  const today = () => new Date().toISOString().slice(0, 10);
  // Every action the API holds, page by page (docs/rest.md §2.1)
  async function allActions(query) {
    const rows = [];
    for (let cursor = null; ;) {
      const page = await App.read(`/actions?${query}&limit=1000${cursor ? '&cursor=' + cursor : ''}`);
      rows.push(...page.items);
      if (!(cursor = page.next_cursor)) return rows;
    }
  }
  const exportActions = (name, query) => async () => download(`${name}-${today()}.jsonl`, jsonl(await allActions(query)), 'application/x-ndjson');
  $('#mExport').onclick = exportActions('audit-log', EVALUATED);
  // The server's audit export of one session: every record plus a SHA-256 footer (docs/rest.md §4.17)
  App.downloadSession = s => { const a = el('a'); a.href = `/api/v1/export/sessions/${encodeURIComponent(s.id)}`; a.download = ''; a.click(); };

  /* Drill-down: #metrics/<view> lists the records behind one KPI, #metrics/<view>/<id> shows one record */
  const overhead = a => a.interception_overhead_ms ?? 0;
  const VIEWS = {
    requests: { title: 'All requests', sub: 'Every prompt and tool call the gateway decided on, newest first.', pick: () => true, query: EVALUATED },
    blocked: { title: 'Blocked requests', sub: 'Stopped before reaching the model or tool.', pick: a => a.decision === 'BLOCK', query: 'decisions=BLOCK' },
    redacted: { title: 'Redacted requests', sub: 'Forwarded with personal data or secrets masked.', pick: a => a.decision === 'REDACT', query: 'decisions=REDACT' },
    held: { title: 'Held for approval', sub: 'Waiting for a human to approve the action.', pick: a => a.decision === 'REQUIRE_APPROVAL', query: 'decisions=REQUIRE_APPROVAL' },
    spend: { title: 'Model spend', sub: 'Requests that reached the model. Token counts and cost are the gateway\'s estimates.', pick: a => a.usage.total_tokens > 0, query: 'kinds=prompt&statuses=completed,redacted' },
    latency: { title: 'Gateway latency', sub: 'Time in all checks per request, slowest first.', pick: () => true, sort: (a, b) => overhead(b) - overhead(a), query: EVALUATED },
    tasks: { title: 'Agent tasks', sub: 'One agent session each. A task succeeded if it was not halted at critical risk.' },
  };
  const td = (cls, ...kids) => { const c = el('td', cls); c.append(...kids); return c; };
  function link(href, ...cells) {  // a table row that opens one record
    const tr = el('tr'); tr.tabIndex = 0; tr.append(...cells);
    tr.onclick = () => location.hash = href; tr.onkeydown = k => { if (k.key === 'Enter') location.hash = href; };
    return tr;
  }
  function table(heads, rows, empty) {
    const w = el('div', 'scroll'), t = el('table'), h = el('tr');
    h.append(...heads.map(([text, cls]) => el('th', cls, text)));
    t.append(el('thead'), el('tbody')); t.tHead.append(h);
    t.tBodies[0].append(...(rows.length ? rows : [emptyRow(empty, heads.length)]));
    w.append(t); return w;
  }
  function facts(pairs) {
    const d = el('dl', 'facts');
    for (const [k, v] of pairs) { const dd = el('dd', typeof v === 'string' ? 'mono' : null); dd.append(v); d.append(el('dt', null, k), dd); }
    return d;
  }
  function head(back, title, sub, exportLabel, exportFn) {
    $('#dBack').href = back; $('#dTitle').textContent = title; $('#dSub').textContent = sub;
    const b = $('#dExport'); b.hidden = !exportFn; b.textContent = exportLabel || ''; b.onclick = exportFn;
  }

  function taskView(id) {
    const v = VIEWS.tasks, s = id && App.sessions.find(x => x.id === id);
    if (s) {
      head('#metrics/tasks', s.label, `Session ${s.id}`, 'Download the audit export (NDJSON)', () => App.downloadSession(s));
      return [App.sessionCard(s)];
    }
    if (id) return head('#metrics/tasks', 'Task not found', 'This session is no longer listed by the read API.'), [];
    head('#metrics', v.title, v.sub);
    const rows = [...App.sessions].sort((a, b) => b.loss - a.loss).map(s => {
      const lvl = Risk.level(s.loss);
      return link(`#metrics/tasks/${encodeURIComponent(s.id)}`, el('td', 'cap', s.label), el('td', 'n mono cap', s.steps),
        el('td', 'n mono cap', s.P.toFixed(2)), el('td', 'n mono cap', s.maxC), el('td', 'n mono cap', s.loss.toFixed(2)),
        td(null, badge(lvl)), td(null, badge(lvl === 'CRITICAL' ? 'FAIL' : 'PASS')));
    });
    return [table([['Session'], ['Steps', 'n'], ['P', 'n'], ['C', 'n'], ['Exp. loss', 'n'], ['Level'], ['Outcome']], rows, 'No agent tasks yet.')];
  }

  // One action in full (docs/rest.md §5.4), fetched when the record is opened
  function recordView(view, id) {
    head('#metrics/' + view, 'Request', id);
    const body = el('div'); body.append(el('p', 'empty', 'Loading…'));
    App.read('/actions/' + encodeURIComponent(id)).then(r => {
      if (path[1] !== id) return;  // the user moved on while this was loading
      const a = r.summary, meta = r.event?.interception_metadata, params = r.event?.action_details?.parameters;
      head('#metrics/' + view, `${verdict(a)[0] + verdict(a).slice(1).toLowerCase()} request`, `${fmtTime(time(a))} · ${a.ts} · ${a.event_id}`,
        'Download this record (JSON)', () => download(`${a.event_id}.json`, json(r), 'application/json'));
      const acted = a.triggered_rules.map(t => t.auditor).join(', ');
      const session = el('a', null, a.session_id); session.href = `#metrics/tasks/${encodeURIComponent(a.session_id)}`;
      body.replaceChildren(
        facts([['Decision', badge(verdict(a))], ['Decided by', acted || 'no control acted'], ['Rule', rule(a)], ['Reason code', a.reason_code || '—'],
          ['Action', what(a)], ['Session', session], ['Agent', a.agent_id], ['Case', a.case_id || '—'], ['Policy version', (a.policy_version || '—').slice(0, 12)],
          ['Tokens', `${fmtNum(a.usage.input_tokens)} in / ${fmtNum(a.usage.output_tokens)} out (${a.usage.source})`], ['Cost', usd(a.usage.cost_usd)],
          ['Gateway time', ms(a.interception_overhead_ms)], ['Model or tool time', ms(a.usage.latency_ms)],
          ...(params && Object.keys(params).length ? [['Arguments (allowlisted)', JSON.stringify(params)]] : [])]),
        table([['Control'], ['Result'], ['Rule'], ['Time', 'n']], (meta?.auditor_decisions || []).map(c => {
          const tr = el('tr'); tr.append(el('td', null, c.auditor), td(null, c.decision === 'ALLOW' ? el('span', 'faint', 'passed') : badge(VERDICT[c.decision])),
            el('td', 'mono cap', c.decision === 'ALLOW' ? '—' : c.rule_id || '—'), el('td', 'n mono cap', ms(c.latency_ms)));
          return tr;
        }), 'No controls ran.'),
        ...(r.detections?.length ? [table([['Detection'], ['Severity'], ['Source'], ['Reason']], r.detections.map(d => {
          const tr = el('tr'); tr.append(el('td', 'mono cap', d.name), td(null, badge(d.severity.toUpperCase())), el('td', 'cap', d.source), el('td', 'cap muted', d.reason_text || d.reason || '—'));
          return tr;
        }), '')] : []));
    }).catch(e => { if (path[1] === id) { head('#metrics/' + view, 'Record not found', e.message); body.replaceChildren(); } });
    return [body];
  }

  function eventView(view, id) {
    if (id) return recordView(view, id);
    const v = VIEWS[view], list = actions.filter(v.pick);
    if (v.sort) list.sort(v.sort);
    head('#metrics', v.title, `${v.sub} Newest ${fmtNum(list.length)} shown.`, 'Download all records (JSONL)', exportActions(view, v.query));
    const rows = list.map(a => link(`#metrics/${view}/${encodeURIComponent(a.event_id)}`, el('td', 'mono cap', fmtTime(time(a))), td(null, badge(verdict(a))),
      el('td', 'mono cap', rule(a)), el('td', 'cap', a.triggered_rules.map(t => t.auditor).join(', ') || '—'), el('td', 'n mono cap', fmtNum(a.usage.total_tokens)),
      el('td', 'n mono cap', ms(a.interception_overhead_ms)), el('td', 'mono cap', what(a)), el('td', 'mono cap muted', a.session_id)));
    return [table([['Time'], ['Decision'], ['Rule'], ['Decided by'], ['Tokens', 'n'], ['Gateway', 'n'], ['Action'], ['Session']], rows,
      reachable ? 'No records of this type yet.' : 'Read API unreachable.')];
  }

  let path = [];  // [view, id] from #metrics/<view>/<id>
  function show(fresh) {
    const [view, id] = path, drill = view in VIEWS;
    $('#mMain').hidden = drill; $('#mDrill').hidden = !drill;
    if (!drill) return render(fresh);
    $('#dBody').replaceChildren(...(view === 'tasks' ? taskView(id) : eventView(view, id)));
  }

  // Everything on this tab is polled from the read API. A failed poll keeps the last good numbers.
  async function load() {
    try {
      const a = await App.read(`/actions?${EVALUATED}&limit=1000`);
      let served = null;
      if (aggregates) {
        try { served = await Promise.all(['/metrics/security', '/metrics/usage?group_by=agent', '/metrics/performance'].map(p => App.read(p))); }
        catch (e) { if (e.status === 501) aggregates = false; }
      }
      a.items.sort((x, y) => x.ts < y.ts ? 1 : x.ts > y.ts ? -1 : 0);  // newest first, whatever order the API lists them in
      const d = derive(a.items), fresh = actions.length > 0 && a.items[0]?.event_id !== actions[0]?.event_id;
      [security, usage, perf] = served || [d.security, d.usage, d.perf];
      actions = a.items; reachable = true;
      // The budget meter follows the newest session
      const newest = (await App.read('/sessions?limit=1')).items[0];
      if (newest) budget = await App.read(`/sessions/${encodeURIComponent(newest.session_id)}/usage`).catch(() => budget);
      if (!path[1]) show(fresh);  // a detail view is left as is; a list refreshes
    } catch {
      reachable = false; perf = null;
      if (!path[1]) show(false);
    }
  }
  load(); setInterval(load, POLL_MS);
  // The API labels every read (docs/rest.md §1): the page must not pass example numbers off as recorded evidence
  fetch('/api/v1/health', { cache: 'no-store' }).then(r => { $('#mSource').hidden = r.headers.get('X-Data-Source') !== 'example'; }).catch(() => {});

  // The risk map loaded agent sessions from the read API
  App.on('sessions', () => { if (!path[1]) show(false); });
  App.on('tab', (t, rest) => { if (t === 'metrics') { path = rest; show(false); scrollTo(0, 0); } });
})();
