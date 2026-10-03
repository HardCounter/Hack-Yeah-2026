import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { cp, mkdir, mkdtemp, realpath, rm } from "node:fs/promises";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

// Keep real user config, credentials, and project data out of the CLI fixture.
const root = fileURLToPath(new URL("../../", import.meta.url));
const modelID = "nvidia/nemotron-3-ultra-550b-a55b";
const model = `nvidia/${modelID}`;
const smallModel = "nvidia/nvidia/nemotron-3.5-lightning-30b-a3b";
const agents = {
  "control-builder": "primary",
  "control-architect": "all",
  "rules-auditor": "subagent",
  "direction-auditor": "subagent",
  "security-auditor": "subagent",
  "verification-auditor": "subagent",
};
const commands = {
  "review-project": "control-architect",
  "review-rules": "rules-auditor",
  "review-direction": "direction-auditor",
  "review-security": "security-auditor",
  "review-outcomes": "verification-auditor",
};
const signatures = {
  "control-builder": "You are the project's implementation lead.",
  "control-architect": "You are the read-only architect for",
  "rules-auditor": "You are a read-only competition-compliance reviewer",
  "direction-auditor": "You are the read-only steward of",
  "security-auditor": "You are an independent read-only security reviewer",
  "verification-auditor": "You are the read-only outcome-verification and test-design reviewer",
};
const auditors = Object.keys(agents).filter((name) => agents[name] === "subagent");
const binary = process.env.OPENCODE_BIN || spawnSync("which", ["opencode"], { encoding: "utf8" }).stdout?.trim();
assert(binary, "Install OpenCode or set OPENCODE_BIN to its executable path");
assert(!existsSync("/Library/Managed Preferences"), "Managed macOS preferences need additional isolation");
const temp = await realpath(await mkdtemp(path.join(os.tmpdir(), "opencode-agents-smoke-")));
const fixture = path.join(temp, "project");
let server;
let scenario;

try {
  for (const directory of ["project", "home", "config", "data", "cache", "state", "tmp", "managed"]) {
    await mkdir(path.join(temp, directory));
  }
  await mkdir(path.join(fixture, "docs"));
  await mkdir(path.join(fixture, ".opencode"));
  for (const relative of [
    "opencode.json", "AGENTS.md", "GoldmanSachsRules.md", "GoldmanSachsCriteria.md",
    "docs/project-direction.md", ".opencode/agents", ".opencode/commands",
  ]) {
    await cp(path.join(root, relative), path.join(fixture, relative), { recursive: true, errorOnExist: true });
  }
  const env = {
    PATH: process.env.PATH,
    HOME: path.join(temp, "home"),
    OPENCODE_TEST_HOME: path.join(temp, "home"),
    XDG_CONFIG_HOME: path.join(temp, "config"),
    XDG_DATA_HOME: path.join(temp, "data"),
    XDG_CACHE_HOME: path.join(temp, "cache"),
    XDG_STATE_HOME: path.join(temp, "state"),
    TMPDIR: path.join(temp, "tmp"),
    OPENCODE_TEST_MANAGED_CONFIG_DIR: path.join(temp, "managed"),
    OPENCODE_PURE: "1",
    OPENCODE_DISABLE_DEFAULT_PLUGINS: "1",
    OPENCODE_DISABLE_EXTERNAL_SKILLS: "1",
    OPENCODE_DISABLE_CLAUDE_CODE: "1",
    OPENCODE_DISABLE_MODELS_FETCH: "1",
    OPENCODE_DISABLE_AUTOUPDATE: "1",
    OPENCODE_DISABLE_LSP_DOWNLOAD: "1",
    npm_config_userconfig: "/dev/null",
    npm_config_globalconfig: "/dev/null",
    npm_config_cache: path.join(temp, "npm-cache"),
    npm_config_offline: "true",
    GIT_CONFIG_NOSYSTEM: "1",
    GIT_CONFIG_GLOBAL: "/dev/null",
  };
  const git = spawnSync("git", ["init", "--quiet", fixture], { env, encoding: "utf8" });
  assert.equal(git.status, 0, git.stderr);
  const operational = { username: "smoke", autoupdate: false, snapshot: false, formatter: false, lsp: false };
  env.OPENCODE_CONFIG_CONTENT = JSON.stringify(operational);

  async function cli(args) {
    return await new Promise((resolve, reject) => {
      const child = spawn(binary, args, { cwd: fixture, env, stdio: ["ignore", "pipe", "pipe"] });
      let stdout = "";
      let stderr = "";
      const timer = setTimeout(() => { child.kill("SIGKILL"); }, 90000);
      child.stdout.on("data", (chunk) => { stdout += chunk; });
      child.stderr.on("data", (chunk) => { stderr += chunk; });
      child.on("error", (error) => { clearTimeout(timer); reject(error); });
      child.on("close", (code) => {
        clearTimeout(timer);
        if (code !== 0) reject(new Error(`${args.join(" ")} exited ${code}: ${stderr}\n${stdout}`));
        else resolve(stdout);
      });
    });
  }

  // Match OpenCode's ordered wildcard rules, including broad tool permissions.
  function permission(agent, tool, input = "*") {
    const matches = (pattern, value) => new RegExp(`^${pattern.replace(/[.+^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*").replace(/\?/g, ".")}$`, "s").test(value);
    return agent.permission.filter((rule) => matches(rule.permission, tool) && matches(rule.pattern, input)).at(-1)?.action;
  }

  console.log(`OpenCode ${(await cli(["--version"])).trim()}`);
  const config = JSON.parse(await cli(["debug", "config"]));
  assert.equal(config.default_agent, "control-builder");
  assert.equal(config.model, model);
  assert.equal(config.small_model, smallModel);
  assert.equal(config.share, "disabled");
  assert(config.instructions.includes("docs/project-direction.md"));
  for (const [name, target] of Object.entries(commands)) {
    assert.equal(config.command[name].agent, target);
    assert(config.command[name].template.includes("$ARGUMENTS"));
    if (name !== "review-project") assert.equal(config.command[name].subtask, false);
  }
  for (const [name, mode] of Object.entries(agents)) {
    const agent = JSON.parse(await cli(["debug", "agent", name]));
    assert.equal(agent.mode, mode);
    assert.deepEqual(agent.model, { providerID: "nvidia", modelID });
    assert.equal(permission(agent, "read", path.join(fixture, "AGENTS.md")), "allow");
    for (const secret of [".env", ".env.local", "private.pem", "private.key", "auth.json"]) {
      assert.equal(permission(agent, "read", path.join(fixture, secret)), "deny", `${name}: ${secret}`);
    }
    assert.equal(permission(agent, "read", path.join(fixture, ".env.example")), "allow");
    assert.equal(permission(agent, "edit"), name === "control-builder" ? "allow" : "deny");
    assert.equal(permission(agent, "bash", "git status"), name === "control-builder" ? "ask" : "deny");
    for (const target of ["general", "explore", "unknown-agent"]) {
      assert.equal(permission(agent, "task", target), "deny");
    }
    for (const target of auditors) {
      assert.equal(permission(agent, "task", target), mode === "subagent" ? "deny" : "allow");
    }
    console.log(`PASS loaded ${name}: mode, model, permissions`);
  }
  console.log("PASS defaults and five command definitions");

  server = http.createServer(async (request, response) => {
    try {
      assert.equal(request.url, "/v1/chat/completions");
      let body = "";
      for await (const chunk of request) body += chunk;
      const input = JSON.parse(body);
      assert.equal(input.model, modelID);
      assert.equal(input.stream, true);
      const system = input.messages.filter((message) => message.role === "system").map((message) => message.content).join("\n");
      const name = Object.keys(signatures).find((agent) => system.includes(signatures[agent]));
      assert(name, "Request must include a known agent prompt");
      if (scenario.direct) assert.equal(name, scenario.parent, "Specialist commands must not invoke a parent model");
      assert(system.includes("# Project Instructions"), "AGENTS.md must be loaded");
      assert(system.includes("# AI Control Layer: Project Direction"), "Configured direction must be loaded");
      const session = request.headers["x-opencode-session-id"];
      assert.equal(typeof session, "string", "Request must include an OpenCode session ID");
      assert(session.length > 0);
      scenario.seen.add(name);
      scenario.sessions.add(session);
      const agentSessions = scenario.sessionsByAgent.get(name) || new Set();
      agentSessions.add(session);
      scenario.sessionsByAgent.set(name, agentSessions);
      scenario.requests++;
      const available = (input.tools || []).map((tool) => tool.function.name);
      if (agents[name] !== "primary") {
        assert(!available.includes("edit") && !available.includes("write") && !available.includes("bash"));
      }
      if (agents[name] === "subagent") assert(!available.includes("task"));
      const results = input.messages.filter((message) => message.role === "tool");
      let calls;
      if (scenario.direct && !scenario.called.has(session)) {
        assert(available.includes("read"));
        scenario.called.add(session);
        calls = [{
          index: 0,
          id: "call_read",
          type: "function",
          function: { name: "read", arguments: JSON.stringify({ filePath: path.join(fixture, "AGENTS.md"), limit: 1 }) },
        }];
      } else if (scenario.direct) {
        assert.equal(results.length, 1);
        assert(JSON.stringify(results[0].content).includes("# Project Instructions"), "Auditor must receive the completed read result");
      } else if (scenario.delegate && name === scenario.parent && !scenario.called.has(session)) {
        assert(available.includes("task"), "Parent must have Task available");
        scenario.called.add(session);
        calls = scenario.delegate.map((target, index) => ({
          index,
          id: `call_${index}`,
          type: "function",
          function: {
            name: "task",
            arguments: JSON.stringify({ description: `Smoke ${target}`, subagent_type: target, prompt: `SMOKE_CHILD ${target}: return a synthetic harness marker only.` }),
          },
        }));
      } else if (scenario.delegate && name === scenario.parent) {
        for (const target of scenario.delegate) {
          assert(results.some((result) => JSON.stringify(result.content).includes(`SMOKE_OK ${target}`)), `Missing completed child result: ${target}`);
        }
        scenario.completed = true;
      }
      const id = `chatcmpl_${name}`;
      response.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" });
      const send = (delta, finish_reason = null) => response.write(`data: ${JSON.stringify({ id, object: "chat.completion.chunk", created: 1, model: modelID, choices: [{ index: 0, delta, finish_reason }] })}\n\n`);
      if (calls) {
        send({ role: "assistant", tool_calls: calls });
        send({}, "tool_calls");
      } else {
        send({ role: "assistant", content: `SMOKE_OK ${name}` });
        send({}, "stop");
      }
      response.end("data: [DONE]\n\n");
    } catch (error) {
      scenario.errors.push(error);
      response.writeHead(500, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ error: { message: error.message, type: "smoke_error" } }));
    }
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  env.OPENCODE_CONFIG_CONTENT = JSON.stringify({
    ...operational,
    compaction: { auto: false },
    enabled_providers: ["nvidia"],
    provider: {
      nvidia: {
        npm: "@ai-sdk/openai-compatible",
        env: [],
        whitelist: [modelID],
        options: { baseURL: `http://127.0.0.1:${server.address().port}/v1`, apiKey: "synthetic-smoke-only", timeout: 10000 },
        models: { [modelID]: { name: "Scripted Local Harness", tool_call: true, reasoning: false, interleaved: false, limit: { context: 131072, output: 4096 } } },
      },
    },
  });

  async function run(label, args, parent, delegate, direct = false) {
    scenario = { parent, delegate, direct, requests: 0, seen: new Set(), sessions: new Set(), sessionsByAgent: new Map(), called: new Set(), errors: [], completed: false };
    const output = await cli(["run", "--format", "json", "--title", "Synthetic agent harness", ...args]);
    assert.equal(scenario.errors.length, 0, scenario.errors.map((error) => error.message).join("\n"));
    assert(scenario.seen.has(parent), `${label}: wrong parent`);
    const events = output.split("\n").filter((line) => line.startsWith("{")).map((line) => JSON.parse(line));
    assert(!events.some((event) => event.type === "error"), `${label}: CLI emitted an error`);
    assert(!events.some((event) => event.type === "tool_use" && event.part.state.status === "error"), `${label}: tool execution failed`);
    assert(events.some((event) => event.type === "text" && event.part.time?.end && event.part.text === `SMOKE_OK ${parent}`), `${label}: no completed parent response`);
    const parentSessions = scenario.sessionsByAgent.get(parent);
    assert.equal(parentSessions.size, 1, `${label}: expected a single parent session`);
    const [parentSession] = parentSessions;
    const childSessions = new Set();
    for (const target of delegate || []) {
      const task = events.find((event) => event.type === "tool_use" && event.part.tool === "task" && event.part.state.input.subagent_type === target);
      assert(task, `${label}: missing Task event for ${target}`);
      assert.equal(task.sessionID, parentSession);
      assert.equal(task.part.state.status, "completed");
      assert(task.part.state.output.includes(`SMOKE_OK ${target}`));
      const child = task.part.state.metadata.sessionId;
      assert(scenario.sessionsByAgent.get(target)?.has(child), `${label}: Task metadata must match the observed child`);
      assert.notEqual(child, parentSession, `${label}: child reused the parent session`);
      assert(!childSessions.has(child), `${label}: siblings reused a session`);
      childSessions.add(child);
    }
    if (delegate) {
      assert(scenario.completed, `${label}: parent did not receive child results`);
      for (const target of delegate) assert(scenario.seen.has(target), `${label}: ${target} not invoked`);
      assert(scenario.sessions.size >= delegate.length + 1, `${label}: child sessions not isolated`);
    }
    if (direct) {
      assert.deepEqual([...scenario.seen], [parent], `${label}: duplicate parent work`);
      assert.equal(scenario.sessions.size, 1, `${label}: must use one auditor session`);
      assert.equal(scenario.requests, 2, `${label}: expected read followed by final response, with no extra inference`);
      assert(!events.some((event) => event.type === "tool_use" && event.part.tool === "task"), `${label}: unexpected child Task`);
      assert.equal(events.filter((event) => event.type === "tool_use" && event.part.tool === "read" && event.part.state.status === "completed").length, 1);
    }
    console.log(`PASS ${label}: ${[...scenario.seen].join(", ")}`);
  }

  await run("default builder", ["SMOKE_DEFAULT"], "control-builder");
  await run("builder delegation", ["SMOKE_DELEGATE"], "control-builder", ["control-architect", ...auditors]);
  for (const [command, target] of Object.entries(commands)) {
    if (command === "review-project") {
      await run(command, ["--command", command, "SMOKE_SCOPE"], target, auditors);
    } else {
      for (const caller of ["control-builder", "control-architect"]) {
        await run(`${command} from ${caller}`, ["--agent", caller, "--command", command, "SMOKE_SCOPE"], target, undefined, true);
      }
    }
  }
  console.log("PASS isolated local harness; no authenticated NVIDIA inference performed");
} finally {
  if (server) await new Promise((resolve) => server.close(resolve));
  await rm(temp, { recursive: true, force: true });
}
