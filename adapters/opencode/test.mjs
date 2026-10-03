// Callback harness, NOT evidence of OpenCode hook ordering or throw propagation.
import { test } from "node:test"
import assert from "node:assert/strict"
import { readFile } from "node:fs/promises"
import vm from "node:vm"

const source = await readFile(new URL("./index.js", import.meta.url), "utf8")

async function harness(fetch) {
  const context = vm.createContext({
    fetch, AbortSignal,
    process: { env: { INTERCEPT_TOKEN: "x".repeat(32) } },
  })
  const dependency = new vm.SyntheticModule(["Plugin"], function () {
    this.setExport("Plugin", { define: (plugin) => plugin })
  }, { context })
  const module = new vm.SourceTextModule(source, { context })
  await module.link(() => dependency)
  await module.evaluate()
  const hooks = {}
  await module.namespace.default.setup({
    options: { endpoint: "http://127.0.0.1:8080" },
    tool: { hook: async (name, fn) => { hooks[name] = fn } },
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
