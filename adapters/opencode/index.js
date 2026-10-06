// No runtime import of @opencode/plugin: in 2.0.22 `Plugin.define(plugin)` returns `plugin` unchanged
// (dist/promise/plugin.js), and OpenCode does not install a local plugin's dependencies. Exporting the
// same { id, setup } object lets OpenCode load this directory without `npm install`.

const PLUGIN_ID = "hardcounter.intercept"
const LOOPBACK_ENDPOINT = /^(http:\/\/(?:127\.0\.0\.1|localhost|\[::1\]):(\d{1,5}))\/?$/
const PROMPT_MODES = new Set(["off", "observe", "enforce"])
const PART_TEXT_LIMIT = 16000

// Only a plain http://<loopback>:<port> origin; the control service must be local.
function loopbackEndpoint(value) {
  const match = typeof value === "string" ? LOOPBACK_ENDPOINT.exec(value) : null
  if (!match || Number(match[2]) < 1 || Number(match[2]) > 65535) return null
  return match[1]
}

function clip(text) {
  if (typeof text !== "string") return { text: "", truncated: false }
  return text.length > PART_TEXT_LIMIT
    ? { text: text.slice(0, PART_TEXT_LIMIT), truncated: true, length: text.length }
    : { text, truncated: false }
}

// OpenCode message part -> forwarded part. Reasoning is never forwarded (no private chain-of-thought);
// media bytes are never forwarded; tool results are labelled untrusted data.
function normalizePart(part) {
  if (typeof part === "string") return { type: "text", ...clip(part) }
  switch (part?.type) {
    case "text": return { type: "text", ...clip(part.text) }
    case "tool-call": return { type: "tool-call", id: part.id, name: part.name, input: part.input ?? null }
    case "tool-result": {
      const serialized = JSON.stringify(part.result ?? null)
      return {
        type: "tool-result", id: part.id, name: part.name, trust: "untrusted",
        result: serialized.length > PART_TEXT_LIMIT ? { type: "omitted", length: serialized.length } : part.result ?? null,
      }
    }
    case "reasoning": return null
    default: return { type: "omitted", original_type: String(part?.type ?? "unknown") }
  }
}

function normalizeMessage(message) {
  const parts = typeof message?.content === "string" ? [message.content] : Array.isArray(message?.content) ? message.content : []
  const content = parts.map(normalizePart).filter(Boolean)
  const omitted = parts.length - content.length
  return { role: String(message?.role ?? "unknown"), content, ...(omitted ? { reasoning_parts_omitted: omitted } : {}) }
}

// Necessary runtime shim only; all policy decisions belong to Python.
// Opt-in: deliberately not loaded into the repository's development agent.
export default /** @type {import("@opencode/plugin").Plugin.Plugin} */ ({
  id: PLUGIN_ID,
  async setup(ctx) {
    const endpoint = loopbackEndpoint(ctx.options.endpoint)
    if (!endpoint) throw new Error("Interception requires a loopback http endpoint (options.endpoint)")
    const token = process.env.INTERCEPT_TOKEN
    // Diagnostic handshake: tells the Python side the plugin loaded (or why it did not). Best effort.
    const announce = async (status, extra) => {
      if (ctx.options.announce === false) return
      globalThis.console?.error?.(`[${PLUGIN_ID}] ${status}${extra.reason ? `: ${extra.reason}` : ""}`)
      try {
        await fetch(`${endpoint}/v1/adapter/hello`, {
          method: "POST",
          headers: { "Content-Type": "application/json",
                     ...(status === "ready" ? { Authorization: `Bearer ${token}` } : {}) },
          body: JSON.stringify({ adapter: PLUGIN_ID, status, prompts: ctx.options.prompts ?? "off",
                                 directory: ctx.location?.directory ?? null, ...extra }),
          signal: AbortSignal.timeout(1000),
          redirect: "error",
        })
      } catch {}
    }
    try {
      if (!token || token.length < 32) {
        throw new Error("INTERCEPT_TOKEN (>= 32 chars) is not set in the environment of the OpenCode server")
      }
      const hooks = await install(ctx, endpoint, token)
      await announce("ready", { hooks })
    } catch (error) {
      await announce("error", { reason: String(error?.message ?? error).slice(0, 300) })
      throw error
    }
  },
})

async function install(ctx, endpoint, token) {
    const promptMode = ctx.options.prompts ?? "off"
    if (!PROMPT_MODES.has(promptMode)) throw new Error("options.prompts must be off, observe or enforce")
    let observationsHealthy = true
    const admitted = new Set()
    const gatewayTools = new Set()
    const callKey = (event) => JSON.stringify([event.sessionID, event.id])
    if (ctx.options.registerTools === true) {
      const contractId = ctx.options.contractId
      const adminToken = process.env.INTERCEPT_ADMIN_TOKEN
      if (typeof contractId !== "string" || !/^[A-Za-z0-9_.:-]{1,160}$/.test(contractId) ||
          !adminToken || adminToken.length < 32) {
        throw new Error("Gateway tool mode requires configured contractId and INTERCEPT_ADMIN_TOKEN")
      }
      const catalogResponse = await fetch(`${endpoint}/v1/tools/catalog`, {
        method: "POST", headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
        body: "{}", redirect: "error", signal: AbortSignal.timeout(3000),
      })
      if (!catalogResponse.ok) throw new Error("Gateway tool catalog unavailable")
      const catalog = await catalogResponse.json()
      if (!Array.isArray(catalog.tools) || catalog.tools.length === 0 || catalog.tools.length > 32) {
        throw new Error("Invalid gateway tool catalog")
      }
      for (const tool of catalog.tools) {
        if (!/^[a-z][a-z0-9_]{0,63}$/.test(tool.name) || gatewayTools.has(tool.name) ||
            typeof tool.description !== "string" || tool.input?.type !== "object") {
          throw new Error("Invalid gateway tool definition")
        }
        gatewayTools.add(tool.name)
      }
      await ctx.tool.transform((editor) => {
        // Opt-in controlled mode: expose gateway-backed tools, not local shell/filesystem tools.
        for (const tool of editor.list()) editor.remove(tool.id)
        for (const definition of catalog.tools) {
          editor.add({
            name: definition.name, description: definition.description, input: definition.input,
            options: { codemode: false },
            execute: async (input, context) => {
              const response = await fetch(`${endpoint}/v1/tools/execute`, {
                method: "POST", headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
                body: JSON.stringify({ session_id: context.sessionID, call_id: context.id,
                  tool: definition.name, arguments: input }),
                redirect: "error", signal: AbortSignal.any([context.signal, AbortSignal.timeout(3000)]),
              })
              if (!response.ok) throw new Error("Gateway execution unavailable; do not retry an ambiguous write")
              const result = await response.json()
              if (!["ALLOW", "REDACT", "ALERT"].includes(result.decision) || result.session_id !== context.sessionID ||
                  result.call_id !== context.id || result.tool !== definition.name || !result.tool_result ||
                  typeof result.tool_result !== "object" || Array.isArray(result.tool_result)) {
                throw new Error("Gateway execution denied or invalid")
              }
              if (Object.hasOwn(result.tool_result, "error")) throw new Error("Gateway tool returned an error")
              return { content: JSON.stringify(result.tool_result), metadata: { verification: "NOT_VERIFIED" } }
            },
          })
        }
      })
      await ctx.command.transform((editor) => {
        editor.add({
          name: "intercept-run",
          description: "Start a governed run in this new, empty OpenCode session using the operator-configured Task Contract.",
          execute: async ({ sessionID, prompt, delivery }) => {
            const history = await ctx.session.context({ sessionID })
            if (history.length !== 0) throw new Error("Start a new empty session before /intercept-run")
            const response = await fetch(`${endpoint}/v1/runs/bind`, {
              method: "POST",
              headers: { "Content-Type": "application/json", Authorization: `Bearer ${adminToken}` },
              body: JSON.stringify({ session_id: sessionID, contract_id: contractId }),
              redirect: "error", signal: AbortSignal.timeout(3000),
            })
            if (!response.ok) throw new Error("Trusted Task Contract binding failed")
            const binding = await response.json()
            if (binding.session_id !== sessionID || binding.contract_id !== contractId ||
                typeof binding.policy_version !== "string") {
              throw new Error("Invalid Task Contract binding response")
            }
            await ctx.session.prompt({ ...prompt, sessionID, delivery })
          },
        })
      })
    }
    await ctx.tool.hook("execute.before", async (event) => {
      if (!observationsHealthy) throw new Error("Observation unavailable; further tools blocked")
      // Dispatch enforces the pipeline immediately before execution; do not reserve twice.
      if (gatewayTools.has(event.tool)) return
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
      if (gatewayTools.has(event.tool)) return // Python dispatch already records outcome status.
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

    const registered = ["tool.execute.before", "tool.execute.after"]
    if (gatewayTools.size) registered.push("tool.transform(gateway tools)", "command:intercept-run")
    // Tool use is always intercepted (execute.before / execute.after above). Prompts are opt-in.
    if (promptMode === "off") return registered
    const enforce = promptMode === "enforce"
    const forwarded = new Map() // `${sessionID}|${kind}` -> { count, system, seq }

    // observe: best effort, never blocks. enforce: fail closed unless Python answers ALLOW for this request.
    const submitPrompt = async (body) => {
      let decision
      try {
        const response = await fetch(`${endpoint}/v1/prompts/evaluate`, {
          method: "POST",
          headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
          body: JSON.stringify(body),
          signal: AbortSignal.timeout(3000),
          redirect: "error",
        })
        if (!response.ok) throw new Error("status")
        decision = await response.json()
      } catch {
        if (enforce) throw new Error("Prompt interception unavailable; request blocked")
        return null
      }
      if (!enforce) return null
      if (decision?.decision !== "ALLOW" || decision.session_id !== body.session_id ||
          decision.request_id !== body.request_id || typeof decision.policy_version !== "string" ||
          !/^[a-f0-9]{64}$/.test(decision.policy_version)) {
        throw new Error("Prompt interception denied or invalid; request blocked")
      }
      return decision
    }

    // A user (or command) prompt entering the session, before the agent loop sees it.
    await ctx.session.hook("prompt", async (event) => {
      const files = Array.isArray(event.prompt?.files) ? event.prompt.files : []
      const decision = await submitPrompt({
        action_type: "prompt",
        source: "user",
        request_id: String(event.messageID),
        session_id: event.sessionID,
        message_id: event.messageID,
        delivery: event.delivery,
        ...clip(event.prompt?.text),
        files: files.map((f) => ({ uri: f.uri, ...(f.name ? { name: f.name } : {}) })),
        agents: (event.prompt?.agents ?? []).map((a) => a.name),
      })
      if (decision && Object.hasOwn(decision, "modified_text")) {
        if (typeof decision.modified_text !== "string") throw new Error("Invalid prompt transformation; request blocked")
        event.prompt.text = decision.modified_text
      }
    })

    // Model requests. OpenCode V2 runs one hook per request kind (SessionRequestKind):
    //   context    -> "primary": every step of the agent loop
    //   compaction -> history summarisation, generate -> session.generate(), title -> session title
    // Enforcement scans and meters the full current request on every call, including edited history
    // and unchanged system instructions. Diagnostic observation may forward append-only deltas.
    const forwardModelRequest = async (kind, event) => {
      const messages = Array.isArray(event.messages) ? event.messages : []
      const key = `${event.sessionID}|${kind}`
      const state = forwarded.get(key) ?? { count: 0, system: null, seq: 0 }
      const historyReset = messages.length < state.count
      const start = promptMode === "enforce" || historyReset ? 0 : state.count
      const system = (event.system ?? []).map((part) => (typeof part === "string" ? part : part?.text ?? "")).join("\n")
      const seq = state.seq + 1
      await submitPrompt({
        action_type: "llm_request",
        source: "agent",
        kind,
        request_id: `${event.sessionID}:${kind}:${seq}`,
        session_id: event.sessionID,
        ...(event.agent ? { agent: event.agent } : {}), // title requests carry no agent
        model: { id: event.model?.id, provider_id: event.model?.providerID },
        request_seq: seq,
        message_count: messages.length,
        history_reset: historyReset,
        ...(promptMode === "enforce" || system !== state.system ? { system: clip(system) } : {}),
        messages: messages.slice(start).map(normalizeMessage),
        tools: Object.keys(event.tools ?? {}).sort(),
      })
      forwarded.set(key, { count: messages.length, system, seq })
    }
    await ctx.session.hook("context", (event) => forwardModelRequest("primary", event))
    await ctx.session.hook("compaction", (event) => forwardModelRequest("compaction", event))
    await ctx.session.hook("generate", (event) => forwardModelRequest("generate", event))
    await ctx.session.hook("title", (event) => forwardModelRequest("title", event))
    return [...registered, ...["prompt", "context", "compaction", "generate", "title"].map((h) => `session.${h}`)]
}
