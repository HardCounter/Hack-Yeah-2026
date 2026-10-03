// Callback harness, NOT evidence of OpenCode hook ordering or throw propagation.
import { test } from "node:test"
import assert from "node:assert/strict"
import { readFile } from "node:fs/promises"
import vm from "node:vm"

const source = await readFile(new URL("./index.js", import.meta.url), "utf8")

async function harness(fetch, options = { endpoint: "http://127.0.0.1:8080" }) {
  const context = vm.createContext({
    // the adapter's diagnostic /v1/adapter/hello handshake is answered here and not counted
    fetch: (url, options) => url.endsWith("/v1/adapter/hello") ? Promise.resolve({ ok: true, json: async () => ({}) }) : fetch(url, options),
    AbortSignal,
    process: { env: { INTERCEPT_TOKEN: "x".repeat(32), INTERCEPT_ADMIN_TOKEN: "y".repeat(32) } },
  })
  const dependency = new vm.SyntheticModule(["Plugin"], function () {
    this.setExport("Plugin", { define: (plugin) => plugin })
  }, { context })
  const module = new vm.SourceTextModule(source, { context })
  await module.link(() => dependency)
  await module.evaluate()
  const hooks = {}
  hooks.registered = new Map()
  hooks.removed = []
  hooks.commands = new Map()
  hooks.sessionMessages = []
  await module.namespace.default.setup({
    options,
    tool: {
      hook: async (name, fn) => { hooks[name] = fn },
      transform: async (callback) => callback({
        list: () => [{ id: "shell" }],
        remove: (id) => hooks.removed.push(id),
        add: (definition) => hooks.registered.set(definition.name, definition),
      }),
    },
    command: { transform: async (callback) => callback({ add: (definition) => hooks.commands.set(definition.name, definition) }) },
    session: {
      context: async () => hooks.sessionMessages,
      prompt: async (prompt) => { hooks.forwardedPrompt = prompt },
    },
  })
  return hooks
}

const event = { sessionID: "session-1", id: "call-1", tool: "write", input: { target: "assigned" } }
const allow = {
  decision: "ALLOW", session_id: event.sessionID, call_id: event.id,
  tool: event.tool, policy_version: "a".repeat(64),
}
const response = (value, ok = true) => ({ ok, json: async () => value })

test("allow awaits admission and sends V2 input/id", async () => {
  let request
  const hooks = await harness(async (url, options) => {
    request = { url, options }
    return response(allow)
  })
  let executed = false
  await hooks["execute.before"](event)
  executed = true // synthetic tool body entered only after hook resolves
  assert.equal(executed, true)
  assert.deepEqual(JSON.parse(request.options.body), {
    session_id: "session-1", call_id: "call-1", tool: "write", arguments: { target: "assigned" },
  })
  assert.equal(request.options.redirect, "error")
})

test("deny, malformed, mismatched, HTTP and transport failures prevent synthetic body", async () => {
  for (const fetch of [
    async () => response({ ...allow, decision: "BLOCK" }),
    async () => response({}),
    async () => response({ ...allow, call_id: "other-call" }),
    async () => response({ ...allow, policy_version: "invalid" }),
    async () => response({}, false),
    async () => { throw new Error("synthetic transport failure") },
  ]) {
    const hooks = await harness(fetch)
    let executed = false
    await assert.rejects(async () => {
      await hooks["execute.before"](event)
      executed = true
    })
    assert.equal(executed, false)
  }
})

test("outcomes omit raw results and errors", async () => {
  let sent
  const hooks = await harness(async (_url, options) => {
    sent = JSON.parse(options.body)
    return response(allow)
  })
  await hooks["execute.before"](event)
  await hooks["execute.after"]({ ...event, status: "error", error: { message: "synthetic-secret" } })
  assert.deepEqual(sent, { session_id: "session-1", call_id: "call-1", tool: "write", status: "error" })
  assert.ok(!JSON.stringify(sent).includes("synthetic-secret"))
})

test("failed post-action telemetry prevents later tool admission", async () => {
  const hooks = await harness(async (url) => url.endsWith("evaluate") ? response(allow) : response({}, false))
  await hooks["execute.before"](event)
  await hooks["execute.after"]({ ...event, status: "completed" })
  await assert.rejects(() => hooks["execute.before"](event), /Observation unavailable/)
})

test("unadmitted after-hook errors do not fabricate an execution observation", async () => {
  let calls = 0
  const hooks = await harness(async () => { calls++; return response(allow) })
  await hooks["execute.after"]({ ...event, status: "error" })
  assert.equal(calls, 0)
  await hooks["execute.before"](event)
  assert.equal(calls, 1)
})

test("validated transformation reaches synthetic tool input", async () => {
  const hooks = await harness(async () => response({ ...allow, modified_arguments: { target: "assigned", text: "[REDACTED]" } }))
  const proposed = { ...event, input: { target: "assigned", text: "DEMO_SECRET_123" } }
  await hooks["execute.before"](proposed)
  assert.equal(proposed.input.text, "[REDACTED]")
})

test("registered tools dispatch once through Python without duplicate admission", async () => {
  const requests = []
  const hooks = await harness(async (url, options) => {
    requests.push({ url, body: JSON.parse(options.body) })
    if (url.endsWith("catalog")) return response({ tools: [{ name: "write", description: "write", input: { type: "object" } }] })
    return response({ ...allow, tool_result: { status: "done" } })
  }, { endpoint: "http://127.0.0.1:8080", registerTools: true, contractId: "contract-1" })
  assert.deepEqual(hooks.removed, ["shell"])
  await hooks["execute.before"](event)
  const tool = hooks.registered.get("write")
  const result = await tool.execute(event.input, { sessionID: event.sessionID, id: event.id, signal: new AbortController().signal })
  await hooks["execute.after"]({ ...event, status: "completed" })
  assert.equal(result.metadata.verification, "NOT_VERIFIED")
  assert.deepEqual(requests.map(r => r.url.split("/").at(-1)), ["catalog", "execute"])
})

test("operator command binds actual session before forwarding prompt", async () => {
  const requests = []
  const hooks = await harness(async (url, options) => {
    requests.push({ url, body: JSON.parse(options.body) })
    if (url.endsWith("catalog")) return response({ tools: [{ name: "write", description: "write", input: { type: "object" } }] })
    return response({ session_id: "ses_real-session", contract_id: "contract-1", policy_version: "a".repeat(64) })
  }, { endpoint: "http://127.0.0.1:8080", registerTools: true, contractId: "contract-1" })
  const prompt = { text: "Continue the approved task", files: [] }
  await hooks.commands.get("intercept-run").execute({ sessionID: "ses_real-session", prompt, delivery: "steer" })
  assert.deepEqual(requests.at(-1).body, { session_id: "ses_real-session", contract_id: "contract-1" })
  assert.equal(hooks.forwardedPrompt.sessionID, "ses_real-session")
  assert.equal(hooks.forwardedPrompt.text, prompt.text)
})

test("existing session cannot be rebound after any history", async () => {
  const requests = []
  const hooks = await harness(async (url, options) => {
    requests.push(url)
    return response({
    tools: [{ name: "write", description: "write", input: { type: "object" } }],
    })
  }, { endpoint: "http://127.0.0.1:8080", registerTools: true, contractId: "contract-1" })
  hooks.sessionMessages.push({ role: "user", text: "old task" })
  await assert.rejects(() => hooks.commands.get("intercept-run").execute({
    sessionID: "ses_real-session", prompt: { text: "new" }, delivery: "steer",
  }), /new empty session/)
  assert.equal(requests.some(url => url.endsWith("/v1/runs/bind")), false)
})
