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
  const server = http.createServer((req, res) => {
    let raw = ""
    req.on("data", (chunk) => { raw += chunk })
    req.on("end", () => {
      const body = JSON.parse(raw)
      requests.push({ path: req.url, body, authorized: req.headers.authorization === `Bearer ${"x".repeat(32)}` })
      if (!quiet) process.stdout.write(`\n--> POST ${req.url}\n${JSON.stringify(body, null, 2)}\n`)
      const reply = req.url === "/v1/actions/outcome" ? { ok: true }
        : { decision: "ALLOW", policy_version: POLICY_VERSION, session_id: body.session_id,
            call_id: body.call_id, tool: body.tool, request_id: body.request_id }
      res.writeHead(200, { "Content-Type": "application/json" }).end(JSON.stringify(reply))
    })
  })
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve))
  return { requests, server, endpoint: `http://127.0.0.1:${server.address().port}` }
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
  const { requests, server, endpoint } = await captureServer()
  try {
    const hooks = await loadPlugin({ endpoint, prompts: "enforce" })

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
    // 4. second model request carries only the new assistant + tool messages
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
    // delta only, system unchanged so not resent, reasoning dropped, tool result marked untrusted
    assert.equal(second.request_seq, 2)
    assert.equal(second.messages.length, 2)
    assert.equal(Object.hasOwn(second, "system"), false)
    assert.equal(second.messages[0].reasoning_parts_omitted, 1)
    assert.ok(!JSON.stringify(second).includes("private chain of thought"))
    assert.equal(second.messages[1].content[0].trust, "untrusted")
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

test("prompt forwarding is off by default and non-loopback endpoints are refused", async () => {
  const hooks = await loadPlugin({ endpoint: "http://127.0.0.1:8080" })
  assert.deepEqual(Object.keys(hooks.session), [])
  await assert.rejects(() => loadPlugin({ endpoint: "http://example.com:8080" }), /loopback/)
  await assert.rejects(() => loadPlugin({ endpoint: "http://127.0.0.1:8080", prompts: "maybe" }), /off, observe or enforce/)
})
