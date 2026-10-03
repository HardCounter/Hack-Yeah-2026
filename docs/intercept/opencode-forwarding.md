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
| Agent-loop model request | `ctx.session.hook("context")`: `{sessionID, agent, model: {id, providerID}, system[], messages[], tools{}, options}` | `POST /v1/prompts/evaluate`, `action_type: "llm_request"` |
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
{"action_type": "llm_request", "source": "agent", "request_id": "ses_demo01:2", "session_id": "ses_demo01",
 "agent": "build", "model": {"id": "nemotron-3-ultra", "provider_id": "nvidia"},
 "request_seq": 2, "message_count": 3, "history_reset": false,
 "messages": [
   {"role": "assistant", "content": [{"type": "tool-call", "id": "call_1", "name": "read", "input": {"filePath": "docs/use-cases.md"}}],
    "reasoning_parts_omitted": 1},
   {"role": "tool", "content": [{"type": "tool-result", "id": "call_1", "name": "read", "trust": "untrusted",
    "result": {"type": "text", "value": "# Use cases ..."}}]}],
 "tools": ["bash", "read"]}
```

- **Only new messages are sent** for each request. `request_seq` and `message_count` let the
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
- **`compaction`, `generate`, and `title` model requests** use separate hooks and are not forwarded.
- **The Python side** still needs `/v1/prompts/evaluate`, a response contract for prompts, and a
  mapping from these bodies to the consume plane's envelope v2.1 (`action_type: "llm_call"`, bodies
  as content refs).
