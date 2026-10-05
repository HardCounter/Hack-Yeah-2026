/* Formatting helpers for recorded evidence. Unknown measurements stay unknown; zero is valid. */
const Mock = (() => {
  // Stable visual jitter only; this never supplies metrics or risk scores.
  const draw = key => {
    let h = 2166136261;
    for (const c of String(key)) h = Math.imul(h ^ c.charCodeAt(0), 16777619);
    return (h >>> 0) / 2 ** 32;
  };
  const gateway = a => a.interception_overhead_ms ?? null;
  const usage = a => a.usage || {};
  const auditors = (a, ds) => ds.map(d => ({ v: d.latency_ms ?? null, sim: false }));
  const sim = text => text;
  return { draw, gateway, gatewaySim: () => false, auditors, usage, sim,
    gatewayText: a => ms(gateway(a)),
    tokensText: a => fmtNum(usage(a).total_tokens) };
})();
