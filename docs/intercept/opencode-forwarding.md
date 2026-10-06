# OpenCode Adapter: Forwarded Requests

**Status (2026-10-06):** implemented in `adapters/opencode/index.js` and tested with synthetic
hook events (`adapters/opencode/forward.test.mjs`, run with `scripts/test_opencode_adapter.sh`).
**Real OpenCode v2.0.22 loads the adapter and its startup handshake reaches Python**
(`scripts/check_opencode_pipeline.sh`). The enforcing service (`intercept/service/local.py` / `server.py`)
implements the `POST /v1/prompts/evaluate` endpoint via `PromptGateway`. The observe-only receiver
(`intercept/service/receiver.py`) logs and allows prompts for manual inspection.

## Changes to the adapter (2026-10-03)

| Change | Why |
|---|---|
| **Prompt forwarding added**: `session.hook` for `prompt`, `context`, `compaction`, `generate`, `title`; opt-in through `options.prompts` | The adapter forwarded only tool use. Prompts and model requests are agent actions too |
| **Tool hooks unchanged** (`execute.before` / `execute.after`, always registered); tool bodies keep their exact key sets | `intercept/policy/runs.py` validates exact keys; adding `agent` / `messageID` would break it |
| **Endpoint check relaxed** from exactly `http://127.0.0.1:8080` to any `http://<127.0.0.1\|localhost\|[::1]>:<port>` | Tests and the receiver need other ports. Non-loopback origins are still refused |
| **No runtime `import { Plugin } from "@opencode/plugin"`**; the module exports the `{id, setup}` object directly | In 2.0.22 `Plugin.define` returns its argument unchanged, and OpenCode does **not** install a local plugin's dependencies, so the import made loading fail without `npm install` |
| **`setup` split** into a thin `setup` and an `install(ctx, endpoint, token)` function that registers the hooks and returns their names | So setup can report what it registered, or why it failed |
| **Startup handshake** `POST /v1/adapter/hello`: `{adapter, status: "ready", hooks[], prompts, directory}` with the token, or `{status: "error", reason}` without it. Also logged to stderr as `[hardcounter.intercept] …`; disable with `options.announce: false` | A plugin whose setup throws (for example, missing token) previously failed silently, so the Python side saw nothing |
| **Reasoning parts are never forwarded**, and tool results are labelled `trust: "untrusted"` | AGENTS.md: no private chain-of-thought; retrieved content is data, not authority |

Tests: `adapters/opencode/forward.test.mjs` (7 tests) covers the request shapes, hook
registration, the auxiliary request kinds, fail-closed behaviour, and the handshake on success and
on failure. The original `adapters/opencode/test.mjs` (9 tests) is unchanged except that its fetch
stub answers the handshake, so call counts stay as before.

## Source of the hook shapes

The adapter targets `@opencode/plugin` **2.0.22**, the version of the installed `opencode v2.0.22`.
The hook payloads below come from that package's published type definitions
(`dist/promise/tool.d.ts`, `dist/promise/session.d.ts`) and from `@opencode/ai` / `@opencode/schema`
2.0.22. The older `@opencode-ai/plugin` (1.x) API, with `tool.execute.before(input, output)` and
`callID`, does **not** apply.

| Agent action | OpenCode V2 hook | Adapter request |
|---|---|---|
| User prompt enters a session | `ctx.session.hook("prompt")`: `{sessionID, messageID, prompt: {text, files?, agents?, ...}, delivery}` | `POST /v1/prompts/evaluate`, `action_type: "prompt"` |
| Agent-loop model request | `ctx.session.hook("context")`: `{sessionID, agent, model: {id, providerID}, system[], messages[], tools{}, options}` | `POST /v1/prompts/evaluate`, `action_type: "llm_request"`, `kind: "primary"` |
| History compaction request | `ctx.session.hook("compaction")`: same as `context` (+ optional `result`) | same, `kind: "compaction"` |
| `session.generate()` request | `ctx.session.hook("generate")`: same as `context` | same, `kind: "generate"` |
| Session title request | `ctx.session.hook("title")`: `{sessionID, model, system[], messages[], options}` (no agent, no tools) | same, `kind: "title"` |
| Tool call, before execution | `ctx.tool.hook("execute.before")`: `{tool, sessionID, agent, messageID, id, input}` | `POST /v1/actions/evaluate` (unchanged) |
| Tool call, after execution | `ctx.tool.hook("execute.after")`: same + `status`, `result` / `error` | `POST /v1/actions/outcome` (unchanged) |

## Plugin options

```jsonc
// opencode.json (V2 config key is "plugins"; entries are a spec string or {package, options})
"plugins": [{ "package": "./adapters/opencode", "options": {
  "endpoint": "http://127.0.0.1:8080",   // any http loopback origin: 127.0.0.1, localhost or [::1]
  "prompts": "observe",                  // off (default) | observe | enforce
  "announce": true                       // default true: POST /v1/adapter/hello on startup
}}]
```

`INTERCEPT_TOKEN` (32 or more characters) must be set in the environment. Every request carries
`Authorization: Bearer <token>`.

- `off`: prompts are not forwarded. Tool behaviour is unchanged.
- `observe`: prompts are forwarded, and failures and responses are ignored. It never blocks.
- `enforce`: each prompt or model request waits for Python. It throws (fail closed) unless the
  reply is `{decision: "ALLOW", session_id, request_id, policy_version: <64 hex>}`. A user prompt
  may be rewritten through `modified_text`. Model requests cannot be modified.

## Request bodies

### `action_type: "prompt"`

```json
{"action_type": "prompt", "source": "user", "request_id": "msg_01", "session_id": "ses_demo01",
 "message_id": "msg_01", "delivery": "queue", "text": "Summarise docs/use-cases.md", "truncated": false,
 "files": [{"uri": "file:///repo/docs/use-cases.md", "name": "use-cases.md"}], "agents": []}
```

### `action_type: "llm_request"`

```json
{"action_type": "llm_request", "source": "agent", "kind": "primary", "request_id": "ses_demo01:primary:2", "session_id": "ses_demo01",
 "agent": "build", "model": {"id": "nemotron-3-ultra", "provider_id": "nvidia"},
 "request_seq": 2, "message_count": 3, "history_reset": false,
 "messages": [
   {"role": "assistant", "content": [{"type": "tool-call", "id": "call_1", "name": "read", "input": {"filePath": "docs/use-cases.md"}}],
    "reasoning_parts_omitted": 1},
   {"role": "tool", "content": [{"type": "tool-result", "id": "call_1", "name": "read", "trust": "untrusted",
    "result": {"type": "text", "value": "# Use cases ..."}}]}],
 "tools": ["bash", "read"]}
```

- **Enforcement sends the full normalized history on every request**, including edits that do not
  change message count. Observe mode sends new-message deltas per `kind`; shrinking history resets
  that observation stream. `request_seq`, `message_count` and `history_reset` remain diagnostic metadata.
- **`system`** (`{text, truncated}`) is sent on every enforcement request. Observe mode sends it on
  the first request and whenever it changes. Python includes system text in admission and metering.
- **Reasoning parts are never forwarded**, in line with the AGENTS.md rule against relying on
  private chain-of-thought. Only their count is reported. Unsupported media becomes an explicit
  omitted-content marker and therefore fails closed in enforcement mode.
- **Text parts and the system prompt are capped at 16,000 characters** (`truncated: true`,
  `length`). A tool result larger than that becomes `{type: "omitted", length}`. Python decides
  denies truncated or omitted content before dispatch. Its request body limit is 64 KiB.
- **Tool results carry `trust: "untrusted"`.** They are data, not instructions.

### Tool requests (unchanged)

`{session_id, call_id, tool, arguments}` and `{session_id, call_id, tool, status}`, with exactly
these keys, because `intercept/policy/runs.py` validates exact key sets. The hook's `agent` and
`messageID` are therefore **not** forwarded for tools yet.

## Not verified / open

- **Whether a throw in `session.hook("prompt")` or `("context")` stops the prompt or model
  request in the real runtime.** The harness proves only the adapter's own behaviour. Verify
  against a live `opencode v2.0.22` before claiming prompt enforcement.
- **The model's final answer** (an assistant message with no tool call) is not a request. It shows up
  only in the next request's history, so the last answer of a session is not forwarded.
  Capturing it would need `http.response` (provider-specific streams) or session event subscription.
- **The Python side** evaluates prompts via `LocalService._evaluate_prompt` and `PromptGateway`,
  mapping interaction metadata into the durable evidence store and consume plane envelope v2.1.

## Manual validation with a live OpenCode session

```bash
scripts/check_opencode_pipeline.sh      # automated: real OpenCode loads the adapter -> PASS/FAIL (no prompt sent)
scripts/run_intercept_receiver.sh       # terminal 1: observe-only Python receiver (logs + ALLOWs everything)
scripts/run_opencode_intercepted.sh     # terminal 2: OpenCode wired to that receiver
```

### Observe-only receiver (`intercept/service/receiver.py`)

This is a diagnostic server, separate from the enforcing gateway. It **logs and ALLOWS every request
and enforces nothing**:
- **Routes it accepts:** `/v1/actions/evaluate`, `/v1/actions/outcome`, and `/v1/prompts/evaluate`
  are logged with time, path, session, a one-line summary, and the full JSON (`--compact` puts each
  on one line).
- **Shape checks:** each tool request is checked against what the gateway accepts
  (`Policy.validate_action`, the exact outcome keys). A mismatch gets HTTP 400 and a yellow
  `shape: INVALID …` line. Bodies over the gateway's 64 KiB limit are accepted with a warning.
- **Unsupported routes:** `/v1/runs/bind` and `/v1/tools/*` (gateway-tools mode) return 501.
- **Startup handshake:** `/v1/adapter/hello` prints `ADAPTER CONNECTED …` or
  `ADAPTER FAILED TO START … reason=…`.
- **Bad tokens:** a request with a wrong or missing token gets 401 and its body is not printed.
  The one exception is a failed handshake, which shows only its status and reason, capped at 300
  characters.
- **Data warning:** it prints raw prompt text and tool arguments, so use synthetic data only.

Tests: `tests/support/test_receiver.py`.

`scripts/run_intercept_receiver.sh` creates a fresh token and writes it, with the port and the
demo directory, to `var/intercept.env` (mode 600, git-ignored). It also writes
`var/opencode-demo/opencode.json`, which loads the adapter. `scripts/run_opencode_intercepted.sh`
reads that file, checks that the receiver is reachable and the config loads the adapter, and runs
`opencode --standalone` there. `scripts/check_opencode_pipeline.sh` does the same automatically in
a temporary directory: it passes when the handshake arrives and fails with the reason (or the
OpenCode log tail) otherwise.

### How OpenCode 2.0.22 loads the adapter

This was read from the shipped binary and confirmed by the
check script on 2026-10-03.
- **A `plugins` entry** in the `opencode.json` of the directory OpenCode starts in, with an absolute or
  `./` path to `adapters/opencode`. OpenCode resolves it to `index.js` and logs
  `msg="loading plugin" … entrypoint=…/adapters/opencode/index.js`. OpenCode also auto-loads
  anything under `.opencode/plugins/`, but this repository does not use that.
- **The plugin runs in OpenCode's server process.** `INTERCEPT_TOKEN` must be in *that* process's
  environment. `opencode --standalone` starts a private server that inherits the shell's
  environment. The shared background service (`opencode service status`) does not, so the
  adapter's `setup` fails there.
- **The adapter announces itself** at startup on `POST /v1/adapter/hello`. The receiver prints
  `ADAPTER CONNECTED …` with the registered hooks, or `ADAPTER FAILED TO START … reason=…`. A failed
  setup sends no token and only its status and reason are shown. Inside OpenCode, `/plugins` lists
  plugin status and errors.

Verified on 2026-10-03 with real `opencode v2.0.22` (`--standalone`, pseudo-terminal, no prompt sent):
- **The plugin loads and registers all seven hooks**, and the handshake reaches Python.
- **Without `INTERCEPT_TOKEN`**, the receiver shows `ADAPTER FAILED TO START … INTERCEPT_TOKEN … is not set`.

Not verified: **that the hooks fire during a real conversation.** That needs a prompt sent to a
model provider, so it was left for the manual run.
