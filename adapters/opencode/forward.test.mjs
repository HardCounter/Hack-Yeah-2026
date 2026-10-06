// Forwarding demo: drives the adapter with synthetic OpenCode V2 hook events and a real local HTTP
// capture server, and prints every JSON request the adapter sends to the control service.
// Callback harness: proves request shapes, NOT OpenCode hook ordering or throw propagation.
import { test } from "node:test"
import assert from "node:assert/strict"
import http from "node:http"
import { readFile } from "node:fs/promises"
import vm from "node:vm"

const source = await readFile(new URL("./index.js", import.meta.url), "utf8")
const POLICY_VERSION = "a".repeat(64)
const quiet = process.env.FORWARD_QUIET === "1"

// Stands in for the Python control service: logs each request and answers ALLOW.
async function captureServer() {
  const requests = []
  const hellos = []
  const server = http.createServer((req, res) => {
    let raw = ""
    req.on("data", (chunk) => { raw += chunk })
    req.on("end", () => {
      const body = JSON.parse(raw)
      const record = { path: req.url, body, authorized: req.headers.authorization === `Bearer ${"x".repeat(32)}` }
      if (req.url === "/v1/adapter/hello") hellos.push(record)  // startup handshake, kept apart
      else requests.push(record)
      if (!quiet) process.stdout.write(`\n--> POST ${req.url}\n${JSON.stringify(body, null, 2)}\n`)
      const reply = req.url === "/v1/actions/outcome" ? { ok: true }
        : { decision: "ALLOW", policy_version: POLICY_VERSION, session_id: body.session_id,
            call_id: body.call_id, tool: body.tool, request_id: body.request_id }
      res.writeHead(200, { "Content-Type": "application/json" }).end(JSON.stringify(reply))
    })
  })
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve))
  return { requests, hellos, server, endpoint: `http://127.0.0.1:${server.address().port}` }
}

async function loadPlugin(options) {
  const context = vm.createContext({ fetch, AbortSignal, process: { env: { INTERCEPT_TOKEN: "x".repeat(32) } } })
  const dependency = new vm.SyntheticModule(["Plugin"], function () {
    this.setExport("Plugin", { define: (plugin) => plugin })
  }, { context })
  const module = new vm.SourceTextModule(source, { context })
  await module.link(() => dependency)
  await module.evaluate()
  const hooks = { tool: {}, session: {} }
  await module.namespace.default.setup({
    options,
    tool: { hook: async (name, fn) => { hooks.tool[name] = fn } },
    session: { hook: async (name, fn) => { hooks.session[name] = fn } },
  })
  return hooks
}

// Synthetic events shaped like @opencode/plugin 2.0.22 SessionPrompt, SessionContext and ToolHooks.
const SESSION = "ses_demo01"
const MODEL = { id: "nemotron-3-ultra", providerID: "nvidia" }
const TOOLS = { read: { description: "Read a file", input: { type: "object" } },
                bash: { description: "Run a command", input: { type: "object" } } }
const user = { role: "user", content: [{ type: "text", text: "Summarise docs/use-cases.md" }] }
const assistantCall = { role: "assistant", content: [
  { type: "reasoning", text: "private chain of thought that must not be forwarded" },
  { type: "tool-call", id: "call_1", name: "read", input: { filePath: "docs/use-cases.md" } },
] }
const toolResult = { role: "tool", content: [
  { type: "tool-result", id: "call_1", name: "read", result: { type: "text", value: "# Use cases ..." } },
] }

test("forwards prompts, LLM requests and tool use as JSON", async () => {
  const { requests, hellos, server, endpoint } = await captureServer()
  try {
    const hooks = await loadPlugin({ endpoint, prompts: "enforce" })
    // setup announced itself, authenticated, with the hooks it registered
    assert.equal(hellos.length, 1)
    assert.equal(hellos[0].authorized, true)
    assert.equal(hellos[0].body.status, "ready")
    assert.ok(hellos[0].body.hooks.includes("tool.execute.before") && hellos[0].body.hooks.includes("session.context"))

    // 1. user prompt enters the session
    const prompt = { sessionID: SESSION, messageID: "msg_01", delivery: "queue",
                     prompt: { text: "Summarise docs/use-cases.md", files: [{ uri: "file:///repo/docs/use-cases.md", name: "use-cases.md" }] } }
    await hooks.session.prompt(prompt)
    // 2. first model request
    await hooks.session.context({ sessionID: SESSION, agent: "build", model: MODEL,
      system: [{ type: "text", text: "You are a coding agent." }], messages: [user], options: {}, tools: TOOLS })
    // 3. the model asked for a tool: before / after hooks around execution
    const call = { tool: "read", sessionID: SESSION, agent: "build", messageID: "msg_02", id: "call_1",
                   input: { filePath: "docs/use-cases.md" } }
    await hooks.tool["execute.before"](call)
    await hooks.tool["execute.after"]({ ...call, status: "completed", result: { content: "# Use cases ..." } })
    // 4. enforcement forwards the entire current context for scanning and metering
    await hooks.session.context({ sessionID: SESSION, agent: "build", model: MODEL,
      system: [{ type: "text", text: "You are a coding agent." }], messages: [user, assistantCall, toolResult], options: {}, tools: TOOLS })

    assert.deepEqual(requests.map((r) => [r.path, r.body.action_type ?? r.body.status ?? "tool"]), [
      ["/v1/prompts/evaluate", "prompt"],
      ["/v1/prompts/evaluate", "llm_request"],
      ["/v1/actions/evaluate", "tool"],
      ["/v1/actions/outcome", "completed"],
      ["/v1/prompts/evaluate", "llm_request"],
    ])
    assert.ok(requests.every((r) => r.authorized))
    const [userPrompt, first, admission, outcome, second] = requests.map((r) => r.body)
    assert.equal(userPrompt.text, "Summarise docs/use-cases.md")
    assert.deepEqual(userPrompt.files, [{ uri: "file:///repo/docs/use-cases.md", name: "use-cases.md" }])
    assert.equal(first.system.text, "You are a coding agent.")
    assert.deepEqual(first.tools, ["bash", "read"])
    // tool bodies keep the exact key sets the Python service validates
    assert.deepEqual(Object.keys(admission).sort(), ["arguments", "call_id", "session_id", "tool"])
    assert.deepEqual(Object.keys(outcome).sort(), ["call_id", "session_id", "status", "tool"])
    // Full history and unchanged system are included; reasoning is dropped, tool result untrusted.
    assert.equal(second.kind, "primary")
    assert.equal(second.request_seq, 2)
    assert.equal(second.messages.length, 3)
    assert.equal(second.system.text, first.system.text)
    assert.equal(second.messages[1].reasoning_parts_omitted, 1)
    assert.ok(!JSON.stringify(second).includes("private chain of thought"))
    assert.equal(second.messages[2].content[0].trust, "untrusted")
  } finally {
    server.close()
  }
})

test("enforce mode blocks a prompt when the service is down; observe mode does not", async () => {
  const deadEndpoint = "http://127.0.0.1:9" // discard port: nothing listens
  const enforced = await loadPlugin({ endpoint: deadEndpoint, prompts: "enforce" })
  await assert.rejects(() => enforced.session.prompt({ sessionID: SESSION, messageID: "msg_x", delivery: "queue",
    prompt: { text: "hi" } }), /request blocked/)
  const observed = await loadPlugin({ endpoint: deadEndpoint, prompts: "observe" })
  await observed.session.prompt({ sessionID: SESSION, messageID: "msg_y", delivery: "queue", prompt: { text: "hi" } })
})

test("registers tool hooks always and every prompt hook when prompts are on", async () => {
  const hooks = await loadPlugin({ endpoint: "http://127.0.0.1:8080", prompts: "observe" })
  assert.deepEqual(Object.keys(hooks.tool).sort(), ["execute.after", "execute.before"])
  assert.deepEqual(Object.keys(hooks.session).sort(), ["compaction", "context", "generate", "prompt", "title"])
})

test("auxiliary model requests (compaction, generate, title) are forwarded with their kind", async () => {
  const { requests, server, endpoint } = await captureServer()
  try {
    const hooks = await loadPlugin({ endpoint, prompts: "enforce" })
    const base = { sessionID: SESSION, model: MODEL, system: [{ type: "text", text: "Summarise." }], messages: [user], options: {} }
    await hooks.session.compaction({ ...base, agent: "build", tools: TOOLS })
    await hooks.session.generate({ ...base, agent: "build", tools: {} })
    await hooks.session.title(base)                      // SessionTitle has no agent and no tools
    await hooks.session.compaction({ ...base, agent: "build", tools: TOOLS, messages: [user, assistantCall] })
    const bodies = requests.map((r) => r.body)
    assert.deepEqual(bodies.map((b) => [b.kind, b.request_id]), [
      ["compaction", `${SESSION}:compaction:1`],
      ["generate", `${SESSION}:generate:1`],
      ["title", `${SESSION}:title:1`],
      ["compaction", `${SESSION}:compaction:2`],
    ])
    assert.equal(Object.hasOwn(bodies[2], "agent"), false)
    assert.equal(bodies[3].messages.length, 2)
  } finally {
    server.close()
  }
})

test("enforcement forwards edited history and unchanged system on every request", async () => {
  const { requests, server, endpoint } = await captureServer()
  try {
    const hooks = await loadPlugin({ endpoint, prompts: "enforce" })
    const base = { sessionID: SESSION, model: MODEL, system: ["Scan every request"], tools: {} }
    await hooks.session.context({ ...base, messages: [user] })
    await hooks.session.context({ ...base, messages: [{ role: "user", content: "SYNTHETIC_CHANGED_HISTORY" }] })
    const second = requests[1].body
    assert.equal(second.messages.length, 1)
    assert.equal(second.messages[0].content[0].text, "SYNTHETIC_CHANGED_HISTORY")
    assert.equal(second.system.text, "Scan every request")
    assert.equal(second.message_count, 1)
  } finally {
    server.close()
  }
})

test("prompt forwarding is off by default and non-loopback endpoints are refused", async () => {
  const hooks = await loadPlugin({ endpoint: "http://127.0.0.1:8080" })
  assert.deepEqual(Object.keys(hooks.session), [])
  assert.deepEqual(Object.keys(hooks.tool).sort(), ["execute.after", "execute.before"])
  await assert.rejects(() => loadPlugin({ endpoint: "http://example.com:8080" }), /loopback/)
  await assert.rejects(() => loadPlugin({ endpoint: "http://127.0.0.1:8080", prompts: "maybe" }), /off, observe or enforce/)
})

test("a setup failure is announced to the receiver, without the token", async () => {
  const { hellos, server, endpoint } = await captureServer()
  try {
    const context = vm.createContext({ fetch, AbortSignal, process: { env: {} } })
    const dependency = new vm.SyntheticModule([], function () {}, { context })
    const module = new vm.SourceTextModule(source, { context })
    await module.link(() => dependency)
    await module.evaluate()
    await assert.rejects(() => module.namespace.default.setup({ options: { endpoint, prompts: "observe" } }), /INTERCEPT_TOKEN/)
    assert.equal(hellos.length, 1)
    assert.equal(hellos[0].body.status, "error")
    assert.match(hellos[0].body.reason, /INTERCEPT_TOKEN .* not set/)
    assert.equal(hellos[0].authorized, false)
  } finally {
    server.close()
  }
})
