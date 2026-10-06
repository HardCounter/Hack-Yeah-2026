import assert from 'node:assert/strict';
import test from 'node:test';

import { browser, source, settle, response } from './dashboard-harness.mjs';

test('shared errors preserve standard and legacy server messages', async () => {
  let body = { detail: 'session is still busy' }, status = 409;
  const b = browser(async () => response(body, status));
  await assert.rejects(b.run("App.request('/opencode-wrapper/api/sessions')"), /session is still busy/);
  body = { error: { code: 'invalid_config', message: 'invalid configuration', details: { fields: ['budget.tokens'] } } }; status = 422;
  await assert.rejects(b.run("App.request('/api/v1/configs/standard')"), error => error.message === 'invalid configuration: budget.tokens' && error.code === 'invalid_config');
});

test('admin token is sent only to same-origin configuration mutations', async () => {
  const calls = [], b = browser(async (url, opts) => { calls.push({ url, opts }); return response({}); });
  b.run("App.setAdminToken('synthetic-token')");
  await b.run("App.request('/api/v1/configs/standard', {method:'PUT', headers:{'Content-Type':'application/json'},body:'{}'})");
  assert.equal(calls[0].opts.headers.get('X-Admin-Token'), 'synthetic-token');
  assert.equal(calls[0].opts.headers.get('Content-Type'), 'application/json');
  assert.equal(calls[0].opts.redirect, 'error');
  await b.run("App.request('/api/v1/configs')");
  await b.run("App.request('https://other.example/api/v1/configs/standard', {method:'PUT'})");
  assert.equal(calls[1].opts?.headers, undefined);
  assert.equal(calls[2].opts?.headers, undefined);
});

test('health shows degradation when evidence fails and rejects HTTP failures', async () => {
  let webStatus = 200, evidenceStatus = 503;
  const b = browser(async url => response({ status: 'ok', commit: 'abc123' }, url === '/healthz' ? webStatus : evidenceStatus));
  await b.run('health()');
  assert.equal(b.get('#gateway').textContent, 'Degraded · evidence unavailable');
  evidenceStatus = 200; await b.run('health()');
  assert.equal(b.get('#gateway').textContent, 'Gateway online');
  webStatus = 500; await b.run('health()');
  assert.equal(b.get('#gateway').textContent, 'Gateway unreachable');
});

test('recorded zero and missing measurements are not fabricated', () => {
  const b = browser(async () => response({}));
  assert.equal(b.run("Mock.gateway({interception_overhead_ms:0})"), 0);
  assert.equal(b.run('Mock.gateway({})'), null);
  assert.equal(b.run("Mock.usage({kind:'tool_use', session_id:'test-scripted-1', usage:{total_tokens:0}}).total_tokens"), 0);
  assert.equal(b.run('Mock.usage({}).total_tokens'), undefined);
  assert.equal(b.run('fmtNum(null)'), '—');
});

test('legacy metrics count valid decisions without inventing auditor runs or latencies', async () => {
  const rows = [{ event_id: 'e1', kind: 'tool_use', decision: 'BLOCK', triggered_rules: [{ auditor: 'scope' }], usage: {}, ts: '2026-10-05T10:00:00Z', session_id: 's' },
    { event_id: 'e2', kind: 'session', decision: null, triggered_rules: [], ts: '2026-10-05T10:00:01Z', usage: {} }];
  const b = browser(async url => {
    if (url.startsWith('/api/v1/metrics/')) return response({ error: { message: 'not implemented' } }, 501);
    if (url.startsWith('/api/v1/actions')) return response({ items: rows });
    if (url.startsWith('/api/v1/sessions')) return response({ items: [] });
    return response({ status: 'ok' });
  });
  b.run(source('metrics.js')); await settle();
  assert.ok(b.get('#kpis').textContent.includes('Requests1'));
  assert.ok(b.get('#ctlTable').textContent.includes('scope—1——'));
  assert.ok(!b.get('#ctlTable').textContent.includes('NaN'));
  b.get('#mExport').click();
  assert.equal(b.downloads[0], '/api/v1/export/actions?kinds=prompt,tool_use,egress');
});

test('outcome cards load persisted verification and display empty evidence honestly', async () => {
  const calls = [], b = browser(async url => {
    calls.push(url);
    if (url === '/api/suite/runs/latest') return response({ status: 'not_run', cases: [] });
    if (url === '/api/v1/sessions?limit=50') return response({ items: [{ session_id: 'ses_real', case_id: 'APP-0001', agent_id: 'agent', verification_status: 'FAILED_POSTCONDITIONS' }], next_cursor: null });
    if (url === '/api/v1/sessions/ses_real/verification') return response({ verification_status: 'FAILED_POSTCONDITIONS', checks: [{ id: 'ONB-P2', status: 'FAIL', evidence_source: 'bank_state', detail: 'IDENTITY_MISMATCH' }] });
    return response({});
  });
  b.run(source('tests.js')); b.run("App.emit('tab','tests',[])"); await settle();
  assert.equal(b.get('#sWord').textContent, 'FAILED POSTCONDITIONS');
  assert.ok(b.get('#evidence').textContent.includes('ONB-P2bank_stateIDENTITY_MISMATCHFAIL'));
  assert.ok(calls.includes('/api/v1/sessions/ses_real/verification'));
  assert.ok(!b.get('#scenarios').textContent.includes('Approved client, wrong name saved'));
});


test('risk polling uses one summary request and loads backend-scored steps only on demand', async () => {
  const calls = [], b = browser(async url => {
    calls.push(url);
    if (url === '/api/v1/sessions?limit=50') return response({ items: Array.from({length: 50}, (_, i) => ({
      session_id: `ses_${i}`, agent_id: 'agent', risk_step_count: 1,
      risk: {failure_probability: .7, expected_loss: 5.6, max_consequence: 8, level: 'high'},
      verification_status: null,
    })) });
    if (url.startsWith('/api/v1/trajectories/session/ses_0?')) return response({ segments: [{steps: [{
      summary: {event_id: 'e1', name: 'create_client', decision: 'ALLOW', executed: true, usage: {}},
      event: {action_details: {}}, risk: {consequence: 8, probability: .7, expected_loss: 5.6, signals: ['tool_error']}, trace: [],
    }]}], next_cursor: null, has_more: false });
    throw new Error(`unexpected fanout: ${url}`);
  });
  b.run(source('riskmap.js')); await settle();
  assert.equal(calls.length, 1);
  assert.equal(b.run('App.sessions.length'), 50);
  const card = b.run('App.sessionCard(App.sessions[0])'); await settle();
  assert.equal(calls.length, 2);
  assert.ok(card.textContent.includes('Impact C8'));
  assert.ok(card.textContent.includes('create_client8ALLOWEDtool error'));
  await b.intervals[0](); await settle();
  assert.equal(calls.length, 3);
});

test('historical sessions without risk remain unassessed and unverified in task views', async () => {
  const b = browser(async url => {
    if (url.startsWith('/api/v1/metrics/')) return response({error:{message:'not implemented'}}, 501);
    if (url.startsWith('/api/v1/actions?')) return response({items:[]});
    if (url.includes('/decisions?')) return response({items:[],next_cursor:null,has_more:false});
    if (url.startsWith('/api/v1/trajectories/')) return response({segments:[],next_cursor:null,has_more:false});
    if (url === '/api/v1/sessions?limit=50') return response({items:[{session_id:'historical',agent_id:'agent',risk:null,risk_step_count:0,verification_status:null}]});
    if (url === '/api/v1/sessions?limit=1') return response({items:[]});
    return response({status:'ok'});
  });
  b.run(source('metrics.js')); b.run(source('riskmap.js')); await settle();
  b.run("App.emit('tab', 'metrics', ['tasks'])");
  assert.ok(b.get('#dBody').textContent.includes('UNAVAILABLE'));
  assert.ok(b.get('#dBody').textContent.includes('NOT VERIFIED'));
  assert.ok(!b.get('#dBody').textContent.includes('NaN'));
  b.run("App.emit('tab', 'metrics', ['tasks','historical'])"); await settle();
  assert.ok(b.get('#dBody').textContent.includes('Likelihood P—'));
  assert.ok(b.get('#dBody').textContent.includes('Expected loss—'));
});

test('saving a config does not replace the selected immutable policy until activation', async () => {
  let active = 'active-1';
  const calls = [], b = browser(async url => {
    calls.push(url);
    if (url === '/api/v1/configs') return response([{name:'standard',selected:true,revision:'saved-2',active_revision:active,requires_selection:active !== 'saved-2'}]);
    if (url === '/api/v1/configs/standard?view=active') return response({name:'standard',budget:{tokens:active === 'active-1' ? 1000 : 2000}});
    throw new Error(`saved policy fetched by active sync: ${url}`);
  });
  await b.run('App.syncActive()');
  assert.equal(b.run('App.config.budget.tokens'), 1000);
  await b.run('App.syncActive()');
  assert.equal(b.run('App.configRevision'), 'active-1');
  assert.equal(calls.filter(url => url.includes('?view=active')).length, 1);
  active = 'saved-2'; await b.run('App.syncActive()');
  assert.equal(b.run('App.config.budget.tokens'), 2000);
  assert.equal(b.run('App.configRevision'), 'saved-2');
});
