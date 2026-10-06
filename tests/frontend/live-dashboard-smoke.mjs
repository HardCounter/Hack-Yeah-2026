/* Run against an isolated synthetic dashboard server:
   node tests/frontend/live-dashboard-smoke.mjs http://127.0.0.1:18800
   The server must use CONFIG_ADMIN_TOKEN=synthetic-smoke-admin-token.
*/
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { browser, source, settle } from './dashboard-harness.mjs';
const base = new URL(process.argv[2] || 'http://127.0.0.1:18800');
assert.ok(['127.0.0.1', 'localhost'].includes(base.hostname), 'Smoke runner only accepts a local synthetic server');
const calls = [];
const b = browser(async (path, options) => {
  const url = new URL(path, base);
  calls.push(url.pathname + url.search);
  return fetch(url, { ...options, signal: AbortSignal.timeout(10000) });
});
const waitFor = async predicate => {
  for (let i = 0; i < 200; i++) {
    if (predicate()) return;
    await new Promise(resolve => setTimeout(resolve, 25));
  }
  throw new Error('Dashboard data did not finish loading');
};
await b.run('health()');
assert.equal(b.get('#gateway').textContent, 'Gateway online');
b.run(source('riskmap.js'));
await waitFor(() => b.run('App.sessions.length') > 0);
assert.equal(calls.filter(p => p.startsWith('/api/v1/sessions?')).length, 1);
assert.equal(calls.filter(p => p.startsWith('/api/v1/trajectories')).length, 0);
const firstCard = b.run('App.sessionCard(App.sessions[0])');
await waitFor(() => !firstCard.textContent.includes('Loading steps'));
assert.ok(firstCard.textContent.includes('recorded tool and egress steps'));
assert.ok(!firstCard.textContent.includes('Steps unavailable'));
assert.equal(calls.filter(p => p.startsWith('/api/v1/trajectories')).length, 1);
b.run(source('metrics.js'));
await waitFor(() => b.get('#ctlTable')?.textContent.includes('trajectory-risk'));
assert.ok(!b.get('#kpis').textContent.includes('NaN'));
assert.ok(b.get('#kpis').textContent.includes('pricing unavailable'));
b.run("App.emit('tab','metrics',['tasks'])");
assert.ok(b.get('#dBody').textContent.includes('VERIFIED SUCCESS'));
b.run(source('tests.js'));
b.run("App.emit('tab','tests',[])");
await waitFor(() => b.get('#sWord')?.textContent && !b.get('#sWord').textContent.includes('Loading'));
assert.ok(['VERIFIED SUCCESS', 'VERIFICATION INCOMPLETE', 'FAILED POSTCONDITIONS'].includes(b.get('#sWord').textContent));
assert.ok(b.get('#evidence').textContent.length > 0);
await b.run('App.syncActive()');
const configName = b.run('App.configName');
const config = JSON.parse(b.run('JSON.stringify(App.config)'));
config.budget.tokens += 1;
const activeTokens = b.run('App.config.budget.tokens');
b.context.smokeConfig = config; b.context.smokeName = configName;
b.run("App.setAdminToken('synthetic-smoke-admin-token')");
const saved = await b.run("App.api('/'+smokeName,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(smokeConfig)})");
await b.run('App.syncActive()');
assert.equal(b.run('App.config.budget.tokens'), activeTokens, 'Saved edits must stay inactive');
b.context.smokeRevision = saved.revision;
await b.run("App.request('/api/v1/config-selection',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:smokeName,revision:smokeRevision})})");
await b.run('App.syncActive()');
assert.equal(b.run('App.config.budget.tokens'), config.budget.tokens);
const exported = await fetch(new URL('/api/v1/export/actions?kinds=prompt,tool_use,egress', base));
assert.equal(exported.status, 200);
const lines = (await exported.text()).trimEnd().split('\n'), footer = JSON.parse(lines.pop());
assert.equal(footer.rows, lines.length);
assert.equal(footer.sha256, createHash('sha256').update(lines.join('\n') + '\n').digest('hex'));
assert.ok(footer.rows > 0);
console.log(`Live dashboard smoke passed: ${b.run('App.sessions.length')} persisted sessions; lazy trajectory, metrics, verification, authenticated config round trip, ${footer.rows} streamed audit rows.`);
