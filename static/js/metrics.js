/* Metrics: KPIs, controls, gateway performance, budget and the audit log, read from the read API (docs/rest.md). */
(() => {
  const POLL_MS = 5000;
  const EVALUATED = 'kinds=prompt,tool_use,egress';  // actions the gateway decided on; session and control events have no decision
  let security = null, usage = null, perf = null, budget = null, actions = [], reachable = true;
  let aggregates = true;  // false once the API answers 501 for /metrics/*: the figures are then derived here

  const emptyRow = (text, span) => { const tr = el('tr'), td = el('td', 'empty', text); td.colSpan = span; tr.append(td); return tr; };
  const pct = (a, b) => a && b ? Math.round(a / b * 100) : 0;
  const usd = v => v == null ? '—' : '$' + v.toFixed(v < 1 ? 4 : 2);
  const verdict = a => VERDICT[a.decision] || '—';
  const rule = a => a.decision === 'ALLOW' ? '—' : a.triggered_rules?.[0]?.rule_id || a.reason_code || '—';
  const what = a => a.kind === 'prompt' ? `prompt · ${a.name}` : a.name;
  const time = a => new Date(a.ts);
  const sum = xs => xs.reduce((a, b) => a + b, 0);
  const quantiles = xs => {
    const s = [...xs].sort((a, b) => a - b), q = p => s.length ? s[Math.min(s.length - 1, Math.max(0, Math.ceil(p * s.length) - 1))] : null;
    return { p50: q(.5), p95: q(.95), p99: q(.99) };
  };
  // Legacy API fallback: measurements absent from evidence remain unknown.
  function derive(rows) {
    const by = { ALLOW: 0, BLOCK: 0, REDACT: 0, REQUIRE_APPROVAL: 0, ALERT: 0 }, acted = {};
    const evaluated = rows.filter(a => Object.hasOwn(by, a.decision));
    for (const a of evaluated) {
      by[a.decision]++;
      for (const name of new Set((a.triggered_rules || []).map(t => t.auditor))) acted[name] = (acted[name] || 0) + 1;
    }
    const ran = evaluated.filter(a => a.executed).map(a => a.usage?.latency_ms).filter(v => v != null);
    const gateway = evaluated.map(Mock.gateway).filter(v => v != null);
    const use = evaluated.filter(a => a.kind === 'prompt' && a.executed).map(Mock.usage);
    const measured = key => use.some(u => u[key] == null) ? null : sum(use.map(u => u[key]));
    return {
      security: { actions: { by_decision: by } },
      usage: { totals: { total_tokens: measured('total_tokens'), cost_usd: null, source: 'estimated' } },
      perf: { actions_evaluated: evaluated.length, interception_overhead_ms: quantiles(gateway), backend_latency_ms: quantiles(ran),
        backend_actions_evaluated: evaluated.filter(a => a.executed).length,
        overhead_share: gateway.length === evaluated.length && ran.length && sum(gateway) + sum(ran) > 0
          ? sum(gateway) / (sum(gateway) + sum(ran)) : null,
        by_method: {}, by_auditor: Object.entries(acted).map(([auditor, n]) => ({ auditor, acted: n, runs: null, p50: null, p95: null })) },
    };
  }

  const outcome = s => s.verification_status || s.verdict?.decision;
  const ok = s => outcome(s) === 'VERIFIED_SUCCESS';

  function kpi(view, label, value, sub, tone) {
    const d = el('a', 'kpi'); d.href = '#metrics/' + view; d.title = 'Show the records behind this number';
    const v = el('span', 'value' + (tone ? ' v-' + tone : ''), value);
    d.append(el('span', 'label', label), v, el('span', 'sub', sub));
    return d;
  }

  // Bar chart over time, oldest left, shown above a drill-down table. value(rows in a slice) sets a bar's height,
  // fmt formats the y axis, yTitle names it; hover a bar for tip(rows, value).
  function timeChart(rows, value, tip, yTitle, fmt) {
    rows = rows.filter(a => viewMeasurement(a, yTitle));
    const N = 20, W = 640, H = 240, L = 64, R = 16, T = 12, B = 48, pw = W - L - R, ph = H - T - B, bw = pw / N;
    const NS = 'http://www.w3.org/2000/svg';
    const node = (tag, attrs, text) => { const e = document.createElementNS(NS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); if (text != null) e.textContent = text; return e; };
    const svg = node('svg', { class: 'chart', viewBox: `0 0 ${W} ${H}`, role: 'img' });
    if (!rows.length) return svg;
    const end = Date.now(), start = Math.min(...rows.map(a => +time(a))), w = Math.max(1, (end - start) / N);
    const slices = Array.from({ length: N }, () => []);
    for (const a of rows) slices[Math.min(N - 1, Math.floor((time(a) - start) / w))].push(a);
    const vals = slices.map(b => b.length ? value(b) : 0), peak = Math.max(...vals);
    // y scale: 4 steps of 1, 2 or 5 x 10^k, the top tick at or above the peak
    const raw = (peak || 1) / 4, mag = 10 ** Math.floor(Math.log10(raw)), step = [1, 2, 5, 10].find(f => f * mag >= raw) * mag;
    const top = step * Math.ceil((peak || 1) / step), y = v => T + ph - v / top * ph;
    svg.setAttribute('aria-label', `${yTitle} over time, ${fmtTime(new Date(start))} to ${fmtTime(new Date(end))}, peak ${fmt(peak)}`);
    for (let v = 0; v <= top + step / 2; v += step) {
      svg.append(node('line', { class: 'grid', x1: L, x2: W - R, y1: y(v), y2: y(v) }),
        node('text', { class: 'tick', x: L - 6, y: y(v), 'text-anchor': 'end', 'dominant-baseline': 'middle' }, v ? fmt(v) : '0'));
    }
    for (let i = 0; i <= N; i += 5) {
      const x = L + i * bw;
      svg.append(node('line', { class: 'axis', x1: x, x2: x, y1: T + ph, y2: T + ph + 4 }),
        node('text', { class: 'tick', x, y: T + ph + 16, 'text-anchor': i === 0 ? 'start' : i === N ? 'end' : 'middle' }, fmtTime(new Date(start + i * w))));
    }
    svg.append(node('line', { class: 'axis', x1: L, x2: W - R, y1: T + ph, y2: T + ph }),
      node('line', { class: 'axis', x1: L, x2: L, y1: T, y2: T + ph }),
      node('text', { class: 'title', x: L + pw / 2, y: H - 6, 'text-anchor': 'middle' }, 'Time (Warsaw)'),
      node('text', { class: 'title', transform: `translate(14 ${T + ph / 2}) rotate(-90)`, 'text-anchor': 'middle' }, yTitle));
    vals.forEach((n, i) => {
      const h = n ? Math.max(1, n / top * ph) : 0, g = node('g', {});
      g.append(node('title', {}, `${fmtTime(new Date(start + i * w))}–${fmtTime(new Date(start + (i + 1) * w))}
${tip(slices[i], n)}`),
        node('rect', { class: 'hit', x: L + i * bw, y: T, width: bw, height: ph }),
        node('rect', { class: 'bar', x: L + i * bw + 2, y: T + ph - h, width: bw - 4, height: h }));
      svg.append(g);
    });
    return svg;
  }
  const viewMeasurement = (a, title) => title === 'Tokens' ? a.kind === 'prompt' && Mock.usage(a).total_tokens != null : Mock.gateway(a) != null;
  const CHARTS = {
    spend: list => timeChart(list, b => sum(b.map(a => Mock.usage(a).total_tokens)),
      (b, n) => `${fmtNum(n)} tokens · ${usd(b.some(a => Mock.usage(a).cost_usd == null) ? null : sum(b.map(a => Mock.usage(a).cost_usd)))}`, 'Tokens', fmtNum),
    latency: list => timeChart(list, b => Math.max(...b.map(overhead)),
      (b, n) => `slowest ${ms(n)} · ${fmtNum(b.length)} requests`, 'Slowest gateway time', ms),
  };

  function render(fresh) {
    const by = security?.actions.by_decision, n = perf?.actions_evaluated ?? 0, num = v => security ? fmtNum(v) : '—';
    // A task is one agent session on the risk map; it succeeded if the outcome checker verified it,
    // or (no verdict recorded) if it was not halted at critical risk
    const tasks = { total: App.sessions.length }, tasksOk = App.sessions.filter(ok).length;
    $('#kpis').replaceChildren(
      kpi('requests', 'Requests', perf ? fmtNum(n) : '—', aggregates ? 'all recorded prompts and tool calls' : 'latest recorded prompts and tool calls'),
      kpi('blocked', 'Blocked', num(by?.BLOCK), `requests · ${pct(by?.BLOCK, n)}% of all`, 'block'),
      kpi('redacted', 'Redacted', num(by?.REDACT), `requests · ${pct(by?.REDACT, n)}% forwarded clean`, 'warn'),
      kpi('held', 'Held for approval', num(by?.REQUIRE_APPROVAL), 'requests awaiting a human', 'hold'),
      kpi('tasks', 'Tasks succeeded', tasks.total ? pct(tasksOk, tasks.total) + '%' : '—', `${tasksOk} of ${tasks.total} listed tasks · independently verified`, 'allow'),
      kpi('spend', 'Model spend', usage ? usd(usage.totals.cost_usd) : '—',
        usage ? `${fmtNum(usage.totals.total_tokens)} tokens${usage.totals.cost_usd == null ? ' · pricing unavailable' : usage.totals.source === 'reported' ? '' : ' · estimate'}` : 'tokens'),
      kpi('latency', 'Gateway p95', ms(perf?.interception_overhead_ms?.p95), perf ? `all checks · p50 ${ms(perf.interception_overhead_ms.p50)}` : 'all checks'),
    );

    const cells = (name, ...values) => { const tr = el('tr'); tr.append(el('td', null, name), ...values.map(v => td('n mono cap', v))); return tr; };
    if (perf) {
      const det = perf.by_method?.deterministic, sem = perf.by_method?.semantic;
      const row = (name, runs, p) => cells(name, fmtNum(runs), ms(p.p50), ms(p.p95), ms(p.p99));
      $('#perfTable').replaceChildren(...(det ? [row('Deterministic checks', det.runs, det)] : []), ...(sem ? [row('AI check', sem.runs, sem)] : []),
        row('Gateway total', perf.actions_evaluated, perf.interception_overhead_ms), row('Model and tool calls', perf.backend_actions_evaluated, perf.backend_latency_ms));
      $('#perfTxt').replaceChildren(sem?.runs ? `AI checks recorded on ${fmtNum(sem.runs)} requests.`
        : 'Computed from recorded gateway measurements. Missing auditor measurements remain unavailable.', el('br'),
        perf.overhead_share == null ? 'Insufficient measurements to calculate the overhead share.' : `The gateway accounts for ${Math.round(perf.overhead_share * 100)}% of end-to-end time.`);
      const count = v => v == null ? '—' : fmtNum(v);
      const ctl = perf.by_auditor || [];
      $('#ctlTable').replaceChildren(...(ctl.length ? ctl.map(a =>
        cells(a.auditor + (a.method === 'semantic' ? ' · AI' : ''), Mock.sim(count(a.runs), a.sim), count(a.acted), Mock.sim(ms(a.p50), a.sim), Mock.sim(ms(a.p95), a.sim)))
        : [emptyRow('No control has acted yet.', 5)]));
    } else {
      $('#perfTable').replaceChildren(emptyRow('Read API unreachable.', 5));
      $('#ctlTable').replaceChildren(emptyRow('Read API unreachable.', 5));
      $('#perfTxt').textContent = '';
    }

    const tokens = budget?.budgets?.find(b => b.resource === 'tokens'), calls = budget?.budgets?.find(b => b.resource === 'tool_calls');
    if (tokens) {
      const used = tokens.used, cost = budget.usage?.cost_usd;
      const p = tokens.utilisation == null ? null : Math.min(tokens.utilisation * 100, 100), fill = $('#budFill');
      fill.style.width = (p ?? 0) + '%';
      fill.className = 'fill' + (tokens.exceeded ? ' block' : p > 80 ? ' warn' : '');
      const meter = $('#budMeter');
      if (tokens.limit != null) meter.setAttribute('aria-valuemax', tokens.limit); else meter.removeAttribute('aria-valuemax');
      if (used != null) meter.setAttribute('aria-valuenow', used); else meter.removeAttribute('aria-valuenow');
      $('#budTxt').textContent = `${fmtNum(used)} / ${fmtNum(tokens.limit)} tokens${tokens.complete === false ? ' · accounting incomplete' : ''}`;
      $('#costTxt').replaceChildren(`Session ${budget.session_id}` + (calls ? `: ${fmtNum(calls.used)} of ${fmtNum(calls.limit)} tool calls` : '') + ', ',
        cost == null ? 'pricing unavailable.' : `${usd(cost)} spent.`, el('br'), 'Requests exceeding the configured budget are blocked before dispatch.');
    } else {
      $('#budFill').style.width = '0%';
      $('#budMeter').removeAttribute('aria-valuenow'); $('#budMeter').removeAttribute('aria-valuemax');
      $('#budTxt').textContent = 'No session budget recorded.'; $('#costTxt').textContent = '';
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
  const json = r => JSON.stringify(r, null, 2) + '\n';
  const exportActions = (name, query) => () => {
    const a = el('a'); a.href = '/api/v1/export/actions?' + query;
    a.download = ''; a.click();
  };
  $('#mExport').onclick = exportActions('audit-log', EVALUATED);
  // The server's audit export of one session: every record plus a SHA-256 footer (docs/rest.md §4.17)
  App.downloadSession = s => { const a = el('a'); a.href = `/api/v1/export/sessions/${encodeURIComponent(s.id)}`; a.download = ''; a.click(); };

  /* Drill-down: #metrics/<view> lists the records behind one KPI, #metrics/<view>/<id> shows one record */
  const overhead = Mock.gateway;
  const VIEWS = {
    requests: { title: 'All requests', sub: 'Every prompt and tool call the gateway decided on, newest first.', pick: () => true, query: EVALUATED },
    blocked: { title: 'Blocked requests', sub: 'Stopped before reaching the model or tool.', pick: a => a.decision === 'BLOCK', query: 'decisions=BLOCK' },
    redacted: { title: 'Redacted requests', sub: 'Forwarded with personal data or secrets masked.', pick: a => a.decision === 'REDACT', query: 'decisions=REDACT' },
    held: { title: 'Held for approval', sub: 'Waiting for a human to approve the action.', pick: a => a.decision === 'REQUIRE_APPROVAL', query: 'decisions=REQUIRE_APPROVAL' },
    spend: { title: 'Model spend', sub: 'Prompts, and the model turn that decided each tool call. Token counts and cost are the gateway\'s estimates.', pick: a => a.kind === 'prompt' && Mock.usage(a).total_tokens > 0, query: EVALUATED },
    latency: { title: 'Gateway latency', sub: 'Time in all checks per request, slowest first.', pick: () => true, sort: (a, b) => overhead(b) - overhead(a), query: EVALUATED },
    tasks: { title: 'Agent tasks', sub: 'One listed agent session each. Success requires a recorded independent verification.' },
    decisions: {},  // #metrics/decisions/<id>: one control-plane decision
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
    for (const [k, v] of pairs) { const dd = el('dd', typeof v === 'string' || v?.className === 'sim' ? 'mono' : null); dd.append(v ?? '—'); d.append(el('dt', null, k), dd); }
    return d;
  }
  function head(back, title, sub, exportLabel, exportFn) {
    $('#dBack').href = back; $('#dTitle').textContent = title; $('#dSub').textContent = sub;
    const b = $('#dExport'); b.hidden = !exportFn; b.textContent = exportLabel || ''; b.onclick = exportFn;
  }

  function sessionDecisions(id) {
    const body = el('div'), rows = [];
    let cursor = null, pending = false;
    const render = (note, more = true) => {
      body.replaceChildren(decisionTable(rows, true));
      if (note) body.append(el('p', 'cap muted', note));
      if (more) {
        const button = el('button', 'btn ghost', pending ? 'Loading decisions…' : rows.length ? 'Load more decisions' : 'Load decisions');
        button.disabled = pending; button.onclick = load; body.append(button);
      }
    };
    async function load() {
      if (pending) return;
      pending = true; render();
      try {
        const page = await App.read(`/sessions/${encodeURIComponent(id)}/decisions?limit=100${cursor ? '&cursor=' + encodeURIComponent(cursor) : ''}`);
        if (path[1] !== id) return;
        const seen = new Set(rows.map(d => d.decision_id)); rows.push(...page.items.filter(d => !seen.has(d.decision_id)));
        cursor = page.next_cursor; pending = false; render(null, page.has_more);
      } catch (e) { pending = false; render(`Decisions unavailable: ${e.message}`); }
    }
    load(); return body;
  }

  function taskView(id) {
    const v = VIEWS.tasks, s = id && App.sessions.find(x => x.id === id);
    if (s) {
      const counts = (s.summary || []).map(c => `${c.plugin} ${c.outcome} ${c.count}`).join(' · ');
      head('#metrics/tasks', s.label, `Session ${s.id}` + (counts ? ` · decisions: ${counts}` : ''), 'Download the audit export (NDJSON)', () => App.downloadSession(s));
      return [App.sessionCard(s), el('h3', null, 'Decision trace'), sessionDecisions(s.id)];
    }
    if (id) return head('#metrics/tasks', 'Task not found', 'This session is no longer listed by the read API.'), [];
    head('#metrics', v.title, v.sub);
    const rows = [...App.sessions].sort((a, b) => b.loss - a.loss).map(s => {
      const lvl = Risk.of(s);
      return link(`#metrics/tasks/${encodeURIComponent(s.id)}`, el('td', 'cap', s.label), el('td', 'n mono cap', s.steps),
        el('td', 'n mono cap', s.P == null ? '—' : s.P.toFixed(2)), el('td', 'n mono cap', s.maxC ?? '—'), el('td', 'n mono cap', s.loss == null ? '—' : s.loss.toFixed(2)),
        td(null, badge(lvl)), td(null, badge(outcome(s) ? code(outcome(s)) : 'NOT VERIFIED')));
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
      const acted = (a.triggered_rules || []).map(t => t.auditor).join(', '), u = Mock.usage(a), ds = meta?.auditor_decisions || [], lat = Mock.auditors(a, ds);
      const notRun = el('span', 'faint', '—'); notRun.title = 'not executed';
      const session = el('a', null, a.session_id); session.href = `#metrics/tasks/${encodeURIComponent(a.session_id)}`;
      body.replaceChildren(
        facts([['Decision', badge(verdict(a))], ['Decided by', acted || 'no control acted'], ['Rule', rule(a)], ['Reason code', a.reason_code || '—'],
          ['Action', what(a)], ['Session', session], ['Agent', a.agent_id], ['Case', a.case_id || '—'], ['Policy version', (a.policy_version || '—').slice(0, 12)],
          ['Tokens', Mock.sim(`${fmtNum(u.input_tokens)} in / ${fmtNum(u.output_tokens)} out${u.source ? ` (${u.source})` : ''}`, u.sim)], ['Cost', Mock.sim(usd(u.cost_usd), u.sim)],
          ['Gateway time', Mock.gatewayText(a)], ['Model or tool time', a.executed ? ms(a.usage?.latency_ms) : notRun],
          ...(params && Object.keys(params).length ? [['Arguments (allowlisted)', JSON.stringify(params)]] : [])]),
        table([['Control'], ['Result'], ['Rule'], ['Time', 'n']], ds.map((c, i) => {
          const tr = el('tr'); tr.append(el('td', null, c.auditor), td(null, c.decision === 'ALLOW' ? el('span', 'faint', 'passed') : badge(VERDICT[c.decision])),
            el('td', 'mono cap', c.decision === 'ALLOW' ? '—' : c.rule_id || '—'), td('n mono cap', Mock.sim(ms(lat[i].v), lat[i].sim)));
          return tr;
        }), a.decision === 'BLOCK' && a.reason_code ? `Blocked by ${a.reason_code} before the auditor pipeline.` : 'No controls ran.'),
        ...(r.detections?.length ? [table([['Detection'], ['Severity'], ['Source'], ['Reason']], r.detections.map(d => {
          const tr = el('tr'); tr.append(el('td', 'mono cap', d.name), td(null, badge(d.severity.toUpperCase())), el('td', 'cap', d.source), el('td', 'cap muted', d.reason_text || d.reason || '—'));
          return tr;
        }), '')] : []));
      // Control-plane plugins that decided on this request; nothing shown if the API has no trace
      App.read(`/sessions/${encodeURIComponent(a.session_id)}/decisions?trigger_event_id=${encodeURIComponent(a.event_id)}`).then(t => {
        if (path[1] === id && t.items?.length) body.append(el('h3', null, 'Control-plane decisions'), decisionTable(t.items));
      }).catch(() => {});
    }).catch(e => { if (path[1] === id) { head('#metrics/' + view, 'Record not found', e.message); body.replaceChildren(); } });
    return [body];
  }

  // Control-plane decisions (docs/rest.md §5.17); full adds the trigger step and outcome columns
  function decisionTable(ds, full) {
    const rows = ds.map(d => link(`#metrics/decisions/${encodeURIComponent(d.decision_id)}`,
      ...(full ? [el('td', 'n mono cap', d.trigger_seq ?? '—'), el('td', 'mono cap', d.trigger ? `${d.trigger.name}:${d.trigger.decision ?? '—'}` : '—')] : []),
      el('td', 'mono cap', d.plugin), ...(full ? [td(null, badge(code(d.outcome).toUpperCase()))] : []),
      td(null, badge(code(d.decision))), el('td', 'cap muted', d.reasoning || '—'), el('td', 'n mono cap', ms(d.duration_ms))));
    return table([...(full ? [['Seq', 'n'], ['Trigger']] : []), ['Plugin'], ...(full ? [['Outcome']] : []), ['Decision'], ['Reasoning'], ['Time', 'n']],
      rows, 'No control-plane decisions recorded.');
  }

  // One control-plane decision in full (docs/rest.md §4.22), fetched when opened
  function decisionView(id) {
    head('#metrics/tasks', 'Decision', id);
    const body = el('div'); body.append(el('p', 'empty', 'Loading…'));
    App.read('/decisions/' + encodeURIComponent(id)).then(d => {
      if (path[1] !== id) return;  // the user moved on while this was loading
      const back = `#metrics/tasks/${encodeURIComponent(d.session_id)}`, tr = d.trigger;
      head(back, `${d.plugin} · ${code(d.decision)}`, `${d.ts} · ${d.decision_id}`,
        'Download this record (JSON)', () => download(`${d.decision_id}.json`, json(d), 'application/json'));
      const session = el('a', null, d.session_id); session.href = back;
      let step = tr ? `#${tr.seq} ${tr.kind} · ${tr.name}${tr.decision ? ' · ' + tr.decision : ''}` : '—';
      if (tr && ['prompt', 'tool_use', 'egress'].includes(tr.kind)) { const a = el('a', null, step); a.href = `#metrics/requests/${encodeURIComponent(tr.event_id)}`; step = a; }
      body.replaceChildren(
        facts([['Plugin', `${d.plugin} ${d.plugin_version}`], ['Method', d.method], ['Outcome', badge(code(d.outcome).toUpperCase())],
          ['Decision', badge(code(d.decision))], ['Reasoning', d.reasoning || '—'], ['Reason', d.reason || '—'], ['Attempt', String(d.attempt)],
          ['Duration', ms(d.duration_ms)], ['Trigger step', step], ['Session', session]]),
        el('h3', null, 'Factors'), el('pre', 'mono', JSON.stringify(d.factors || {}, null, 2)),
        table([['Finding'], ['Severity']], (d.findings || []).map(f => {
          const r = el('tr'); r.append(el('td', 'mono cap', f.rule_id), td(null, badge(f.severity.toUpperCase()))); return r;
        }), 'No findings.'),
        table([['Adjustment'], ['Outcome'], ['Signal']], (d.adjustments || []).map(j => {
          const r = el('tr'); r.append(el('td', 'mono cap', j.action), el('td', 'cap', j.outcome), el('td', 'mono cap muted', j.signal_id || '—')); return r;
        }), 'No policy adjustments.'));
    }).catch(e => { if (path[1] === id) { head('#metrics/tasks', 'Decision not found', e.message); body.replaceChildren(); } });
    return [body];
  }

  function eventView(view, id) {
    if (id) return recordView(view, id);
    const v = VIEWS[view], list = actions.filter(v.pick);
    if (v.sort) list.sort(v.sort);
    head('#metrics', v.title, `${v.sub} Newest ${fmtNum(list.length)} shown.`, 'Download all records (JSONL)', exportActions(view, v.query));
    const rows = list.map(a => link(`#metrics/${view}/${encodeURIComponent(a.event_id)}`, el('td', 'mono cap', fmtTime(time(a))), td(null, badge(verdict(a))),
      el('td', 'mono cap', rule(a)), el('td', 'cap', (a.triggered_rules || []).map(t => t.auditor).join(', ') || '—'), td('n mono cap', Mock.tokensText(a)),
      td('n mono cap', Mock.gatewayText(a)), el('td', 'mono cap', what(a)), el('td', 'mono cap muted', a.session_id)));

    return [...(CHARTS[view] ? [CHARTS[view](list)] : []),
      table([['Time'], ['Decision'], ['Rule'], ['Decided by'], ['Tokens', 'n'], ['Gateway', 'n'], ['Action'], ['Session']], rows,
      reachable ? 'No records of this type yet.' : 'Read API unreachable.')];
  }

  let path = [];  // [view, id] from #metrics/<view>/<id>
  function show(fresh) {
    const [view, id] = path, drill = view in VIEWS;
    $('#mMain').hidden = drill; $('#mDrill').hidden = !drill;
    if (!drill) return render(fresh);
    $('#dBody').replaceChildren(...(view === 'tasks' ? taskView(id) : view === 'decisions' ? decisionView(id) : eventView(view, id)));
  }

  // Everything on this tab is polled from the read API. A failed poll keeps the last good numbers.
  let loading = false;
  async function load() {
    if (loading) return;
    loading = true;
    try {
      const a = await App.read(`/actions?${EVALUATED}&limit=1000`);
      let served = null;
      if (aggregates) {
        try { served = await Promise.all(['/metrics/security', '/metrics/usage?group_by=agent', '/metrics/performance'].map(p => App.read(p))); }
        catch (e) { if (e.status === 501) aggregates = false; else throw e; }
      }
      a.items.sort((x, y) => x.ts < y.ts ? 1 : x.ts > y.ts ? -1 : 0);  // newest first, whatever order the API lists them in
      const d = derive(a.items), fresh = actions.length > 0 && a.items[0]?.event_id !== actions[0]?.event_id;
      [security, usage, perf] = served || [d.security, d.usage, d.perf];
      actions = a.items; reachable = true;
      // The budget meter follows the newest session
      const newest = (await App.read('/sessions?limit=1')).items[0];
      budget = newest ? await App.read(`/sessions/${encodeURIComponent(newest.session_id)}/usage`).catch(() => null) : null;
      if (!path[1]) show(fresh);  // a detail view is left as is; a list refreshes
    } catch {
      reachable = false; security = null; usage = null; perf = null; budget = null;
      if (!path[1]) show(false);
    } finally { loading = false; }
  }
  load(); setInterval(load, POLL_MS);
  // The API labels every read (docs/rest.md §1): the page must not pass example numbers off as recorded evidence
  fetch('/api/v1/health', { cache: 'no-store' }).then(r => { $('#mSource').hidden = r.headers.get('X-Data-Source') !== 'example'; }).catch(() => {});

  // The risk map loaded agent sessions from the read API
  App.on('sessions', () => { if (!path[1]) show(false); });
  App.on('tab', (t, rest) => { if (t === 'metrics') { path = rest; show(false); scrollTo(0, 0); } });
})();
