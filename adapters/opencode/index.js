import { Plugin } from "@opencode/plugin"

// Necessary runtime shim only; all policy decisions belong to Python.
// Opt-in: deliberately not loaded into the repository's development agent.
export default Plugin.define({
  id: "hardcounter.intercept",
  async setup(ctx) {
    const endpoint = ctx.options.endpoint
    const token = process.env.INTERCEPT_TOKEN
    if (endpoint !== "http://127.0.0.1:8080" || !token || token.length < 32) {
      throw new Error("Interception requires the loopback service and INTERCEPT_TOKEN")
    }
    let observationsHealthy = true
    const admitted = new Set()
    const callKey = (event) => JSON.stringify([event.sessionID, event.id])
    await ctx.tool.hook("execute.before", async (event) => {
      if (!observationsHealthy) throw new Error("Observation unavailable; further tools blocked")
      // @opencode/plugin 2.0.22 uses event.id, not V1's callID.
      const response = await fetch(`${endpoint}/v1/actions/evaluate`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
        body: JSON.stringify({
          session_id: event.sessionID,
          call_id: event.id,
          tool: event.tool,
          arguments: event.input,
        }),
        signal: AbortSignal.timeout(3000),
        redirect: "error",
      })
      if (!response.ok) throw new Error("Interception unavailable; tool blocked")
      const decision = await response.json()
      if (decision.decision !== "ALLOW" ||
          decision.session_id !== event.sessionID ||
          decision.call_id !== event.id ||
          decision.tool !== event.tool ||
          typeof decision.policy_version !== "string" ||
          !/^[a-f0-9]{64}$/.test(decision.policy_version)) {
        throw new Error("Interception denied or invalid; tool blocked")
      }
      if (Object.hasOwn(decision, "modified_arguments")) {
        if (!decision.modified_arguments || typeof decision.modified_arguments !== "object" || Array.isArray(decision.modified_arguments)) {
          throw new Error("Invalid interception transformation; tool blocked")
        }
        event.input = decision.modified_arguments
      }
      admitted.add(callKey(event))
    })
    await ctx.tool.hook("execute.after", async (event) => {
      // Some runtimes may report a before-hook rejection as an after-hook error.
      // Denied actions have no open admission and must not fabricate an outcome.
      if (!admitted.delete(callKey(event))) return
      try {
        const response = await fetch(`${endpoint}/v1/actions/outcome`, {
          method: "POST",
          headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
          body: JSON.stringify({
            session_id: event.sessionID,
            call_id: event.id,
            tool: event.tool,
            status: event.status,
          }),
          signal: AbortSignal.timeout(3000),
          redirect: "error",
        })
        if (!response.ok) observationsHealthy = false
      } catch {
        observationsHealthy = false
      }
      // Never claim a post-action telemetry failure prevented/rolled back execution.
    })
  },
})
