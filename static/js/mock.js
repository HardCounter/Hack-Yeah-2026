/* Simulated stand-ins where recorded evidence is degenerate: scripted replays make no model call (0 tokens),
   and an early BLOCK or a passing auditor is stamped 0.0 ms. A real non-zero value always passes through.
   Every stand-in is fixed per record (seeded from its id), so polls, lists, charts and cards agree, and is
   shown with a "simulated value" tooltip. Downloads and exports keep the raw recorded values. */
const Mock = (() => {
  // FNV-1a plus a finaliser: one uniform draw in [0, 1) per key
  const draw = key => {
    let h = 2166136261;
    for (const c of String(key)) h = Math.imul(h ^ c.charCodeAt(0), 16777619);
    h = Math.imul(h ^ h >>> 16, 0x45d9f3b); h = Math.imul(h ^ h >>> 16, 0x45d9f3b);
    return ((h ^ h >>> 16) >>> 0) / 2 ** 32;
  };
  // Log-normal-ish in [lo, hi]: the mean of two draws on a log scale, most values near the geometric middle
  const between = (key, lo, hi) => lo * (hi / lo) ** ((draw(key + ':a') + draw(key + ':b')) / 2);
  const real = v => v > 0;

  // Contract or scope check that blocked before the auditor pipeline ran
  const gateway = a => real(a.interception_overhead_ms) ? a.interception_overhead_ms : between(a.event_id + ':gw', .3, 1.8);
  const gatewaySim = a => !real(a.interception_overhead_ms);

  // Deterministic auditors; the regex scanners do more work than the budget counter
  const RANGE = { 'budget-guard': [.04, .25], 'signature-scanner': [.1, .9], 'privacy-scanner': [.15, .9] };
  const auditor = (name, key) => between(`${key}:${name}`, ...(RANGE[name] || [.05, .6]));
  // One action's auditor rows -> [{ v, sim }]; the simulated part fits in 80% of a real gateway time
  function auditors(a, ds) {
    const out = ds.map((d, i) => real(d.latency_ms) ? { v: d.latency_ms, sim: false } : { v: auditor(d.auditor, a.event_id + ':' + i), sim: true });
    if (real(a.interception_overhead_ms)) {
      const room = Math.max(0, .8 * a.interception_overhead_ms - out.filter(o => !o.sim).reduce((s, o) => s + o.v, 0));
      const used = out.filter(o => o.sim).reduce((s, o) => s + o.v, 0);
      if (used > room) out.forEach(o => { if (o.sim) o.v *= room / used; });
    }
    return out;
  }
  // Controls-table figures for one auditor: every action that reached the pipeline (not an early block) ran it
  function auditorRuns(name, rows) {
    const ran = rows.filter(a => real(a.interception_overhead_ms)), s = ran.map(a => auditor(name, a.event_id + ':0')).sort((x, y) => x - y);
    const q = p => s.length ? s[Math.min(s.length - 1, Math.floor(p * s.length))] : null;
    return { runs: ran.length, p50: q(.5), p95: q(.95), sim: true };
  }

  // $ per million input / output tokens. The config names the allowed models but carries no prices.
  const PRICE = [3, 15];
  // A scripted tool call stands for one planner model turn; its context grows with the step index
  function usage(a) {
    const u = a.usage || {};
    if (real(u.total_tokens) || a.kind !== 'tool_use' || !/-scripted-/.test(a.session_id)) return u;
    const k = Math.max(0, (a.seq || 1) - 1), id = a.event_id + ':tok';
    const input = Math.round((900 + 350 * k) * (.85 + .3 * draw(id + 'i'))), output = Math.round(40 + 140 * draw(id + 'o'));
    return { ...u, input_tokens: input, output_tokens: output, total_tokens: input + output,
      cost_usd: (input * PRICE[0] + output * PRICE[1]) / 1e6, source: 'estimated', sim: true };
  }

  // The text as is, or wrapped so its tooltip says it is simulated
  const sim = (text, on) => { if (!on) return text; const s = el('span', 'sim', text); s.title = 'simulated value'; return s; };
  return { draw, gateway, gatewaySim, auditors, auditorRuns, usage, sim,
    gatewayText: a => sim(ms(gateway(a)), gatewaySim(a)),
    tokensText: a => { const u = usage(a); return sim(fmtNum(u.total_tokens ?? 0), u.sim); } };
})();
