# Initial interception slice: status and API clarification

> Current integration: [integrated-runtime.md](../integrated-runtime.md). The canonical consumer
> wire contract is Event Envelope v2.1 and decisions are ALLOW/BLOCK/REDACT/
> REQUIRE_APPROVAL/ALERT. Older v1/v2.0 examples, uppercase storage enums and
> standalone/unwired status notes below are historical design or internal formats;
> they do not define additional supported external contracts.

> **Update 2026-10-03.** The sections below are the original slice record. These later changes
> supersede parts of it. Details are in [opencode-forwarding.md](opencode-forwarding.md).
>
> - **Live loading is now verified.** Real `opencode v2.0.22` (`--standalone`) loads
>   `adapters/opencode` from a `plugins` entry, and the adapter's startup handshake reaches Python
>   (`scripts/check_opencode_pipeline.sh`). Tool-block propagation and MCP/built-in tool coverage
>   in a real conversation are still untested.
> - **No package install is needed.** The adapter no longer imports `@opencode/plugin` at runtime.
>   `Plugin.define` is an identity function in 2.0.22, and OpenCode does not install a local
>   plugin's dependencies, so the earlier "must be installed by OpenCode's plugin loader" note no
>   longer applies.
> - **`INTERCEPT_TOKEN` must reach OpenCode's server process.** Use `opencode --standalone`; the
>   shared background service does not inherit the shell's environment. Without the token the
>   adapter's setup fails, and it now reports this through `POST /v1/adapter/hello`.
> - **`options.endpoint`** may be any `http://` loopback origin and port, not only `127.0.0.1:8080`.
> - **New options:** `options.prompts` (`off` default, `observe`, `enforce`) adds prompt and
>   model-request forwarding to `/v1/prompts/evaluate`. `options.announce` (default `true`) controls
>   the handshake. Tool request bodies are unchanged.
> - **New `intercept/receiver.py`:** an observe-only diagnostic server for manual OpenCode runs.
>   It logs every request and ALLOWs it, and checks the request shapes against this gateway.
>   `intercept/server.py` itself was not changed and still has no prompt endpoint.
> - **Current test counts:** `intercept/` 35 Python tests; `adapters/opencode` 9 (`test.mjs`) + 6
>   (`forward.test.mjs`) Node tests (`scripts/test_opencode_adapter.sh`).

Work now takes place on `main`, per the team's latest instruction; the older plan's
`intercept` branch assumption is historical. Stack: [Python, asyncio, uv](../stack.md).

## Implemented source (not yet runtime-demonstrated)

- `intercept.policy`: validated trusted JSON run configuration, fixed policy hash,
  exact tool allowlist, argument equality constraints, atomic tool-call admission
  budget, and replay rejection. A run is pre-bound to an OpenCode session by the
  operator's config. Unknown sessions are denied; the model cannot register runs.
- `intercept.server`: authenticated loopback-only asyncio HTTP endpoint with body,
  header, and timeout limits. Admissions enqueue metadata-only JSONL evidence.
  Queue overflow or known disk-writer failure returns 503 (adapter denies).
- `adapters/opencode`: **JavaScript**, opt-in V2 plugin. Await the Python decision
  before execution; any HTTP/network/timeout/malformed/deny response fails closed.
  Plugin package is pinned to `@opencode/plugin` 2.0.22 (MIT).
- The V2 after hook submits only correlated completion/error status, never raw
  tool results. Python marks observations `NOT_VERIFIED`. Unknown/mismatched or
  duplicate observations are rejected. After-hook transmission failure blocks
  future calls in that plugin instance, but cannot undo the already executed tool.

The older architecture's `tool.execute.before` / `output.args` refers to **V1**.
V2 registers `ctx.tool.hook("execute.before", callback)` in `Plugin.define.setup`.
Published 2.0.22 declarations expose `event.sessionID`, `event.id`, `event.tool`,
and `event.input`; `callID` is not a V2 event field. This source uses V2 only.
Declarations were inspected, but real execution/throw propagation is not yet proven.

## Run (synthetic/local only)

Provide `uv` and securely supply a locally generated `INTERCEPT_TOKEN` (at least 32
ASCII characters) to both the service and the OpenCode process; do not put it in
policy or plugin options, logs, or repository files.

```sh
uv run --locked python -m intercept.server --policy intercept/example-policy.json --audit /tmp/opencode/intercept-audit.jsonl
uv run --locked python -m unittest intercept.test_policy intercept.test_server
node --experimental-vm-modules --test adapters/opencode/test.mjs
```

The example binds `synthetic-session`; replace it with the actual governed session
ID in a trusted policy outside the agent's writable checkout. Restart the service
to load it. Opt in to the plugin only in an isolated demo config (V2):

```json
{"plugins": [{"package": "./adapters/opencode", "options": {"endpoint": "http://127.0.0.1:8080"}}]}
```

Do not auto-enable in the development agent: unknown sessions/tools are denied.
The package must be installed by OpenCode's supported plugin loader. Test loading,
blocking, call correlation, and all demo tool paths against the pinned runtime.

## Deliberately incomplete / next slices

- Approval-required calls are **BLOCK**, not allow and not a working approval UI.
  Redaction/modification, semantic supervision, policy reloads, result-content capture,
  consumer retry/DLQ, independent business verification, dashboard, and LLM/MCP
  proxies remain unimplemented in this slice.
- Budgets and replay reservations are in memory; a restart resets them. Do not
  resume a governed run after restarting this service. An admission consumes quota
  even if the tool never executes or audit enqueue fails. Retries are denied, not
  exactly-once external execution guarantees.
- The ingestion queue can lose events on crash/shutdown. Writer failure halts
  future admissions but cannot undo already executed actions. Audit is sanitized
  admission evidence, not proof of persisted business outcomes.
- Loopback bearer authentication is not OS isolation. Keep policy/credentials
  outside the agent's access; constrain other plugins, direct paths, and OS/network
  permissions. Later hooks can change inputs after this hook: do not claim final
  argument integrity without runtime-order tests and a protected execution boundary.
- System PATH initially lacked `uv`, Node, and Bun. With approval, validation-only
  uv 0.9.0 and Node 22.20.0 were installed under `/tmp/opencode/intercept-tooling`;
  no system-wide tools were changed. Production/demo setup still needs its own tools.

## Validation evidence

- `uv run --locked python -m unittest intercept.test_policy intercept.test_server -v`:
  **10 tests passed**, using an isolated uv project environment/cache under
  `/tmp/opencode/intercept-tooling`. Exercises atomic budgets, task scope, replay,
  unavailable audit, auth, correlation, and independently read JSONL evidence.
- `node --experimental-vm-modules --test adapters/opencode/test.mjs`:
  **5 tests passed**. This is a mocked Plugin.define/context/fetch callback harness,
  including synthetic tool-body prevention. It is **not** real OpenCode hook coverage.
- `node --check adapters/opencode/index.js` and Python AST/JSON parsing passed.
- OpenCode v2.0.22 package declarations were inspected for the V2 hook payload and
  plugin lifecycle. Live plugin loading, tool-block propagation, MCP/built-in tool
  coverage, and external business-state verification remain untested.
