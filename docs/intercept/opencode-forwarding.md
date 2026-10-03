# OpenCode Adapter: Forwarded Requests

**Status (2026-10-03):** implemented in `adapters/opencode/index.js` and tested with synthetic
hook events (`adapters/opencode/forward.test.mjs`, run with `scripts/test_opencode_adapter.sh`).
It is **not yet validated against a live OpenCode session**, and the Python service has **no
`/v1/prompts/evaluate` endpoint yet**.

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
  "prompts": "observe"                   // off (default) | observe | enforce
}}]
```

`INTERCEPT_TOKEN` (32 or more characters) must be set in the environment. Every request carries
`Authorization: Bearer <token>`.

- `off`: prompts are not forwarded. Tool behaviour is unchanged. This is the default, because
  Python has no prompt endpoint yet.
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

- **Only new messages are sent** for each request, tracked separately per `kind`. `request_seq` and `message_count` let the
  receiver rebuild the full history. If the history shrinks (compaction), everything is resent
  with `history_reset: true`.
- **`system`** (`{text, truncated}`) is sent on the first request and again whenever it changes.
- **Reasoning parts are never forwarded**, in line with the AGENTS.md rule against relying on
  private chain-of-thought. Only their count is reported. Media parts are reduced to `{type}`.
- **Text parts and the system prompt are capped at 16,000 characters** (`truncated: true`,
  `length`). A tool result larger than that becomes `{type: "omitted", length}`. Python decides
  whether truncated input is acceptable. Its request body limit is 64 KiB.
- **Tool results carry `trust: "untrusted"`.** They are data, not instructions.

### Tool requests (unchanged)

`{session_id, call_id, tool, arguments}` and `{session_id, call_id, tool, status}`, with exactly
these keys, because `intercept/policy.py` validates exact key sets. The hook's `agent` and
`messageID` are therefore **not** forwarded for tools yet.

## Not verified / open

- **Whether a throw in `session.hook("prompt")` or `("context")` stops the prompt or model
  request in the real runtime.** The harness proves only the adapter's own behaviour. Verify
  against a live `opencode v2.0.22` before claiming prompt enforcement.
- **The model's final answer** (an assistant message with no tool call) is not a request. It shows up
  only in the next request's message delta, so the last answer of a session is not forwarded.
  Capturing it would need `http.response` (provider-specific streams) or session event subscription.
- **The Python side** still needs `/v1/prompts/evaluate`, a response contract for prompts, and a
  mapping from these bodies to the consume plane's envelope v2.1 (`action_type: "llm_call"`, bodies
  as content refs).

## Manual validation with a live OpenCode session

```bash
scripts/check_opencode_pipeline.sh      # automated: real OpenCode loads the adapter -> PASS/FAIL (no prompt sent)
scripts/run_intercept_receiver.sh       # terminal 1: observe-only Python receiver (logs + ALLOWs everything)
scripts/run_opencode_intercepted.sh     # terminal 2: OpenCode wired to that receiver
```

How OpenCode 2.0.22 loads the adapter. This was read from the shipped binary and confirmed by the
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
