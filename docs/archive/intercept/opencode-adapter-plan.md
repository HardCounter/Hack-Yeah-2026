# OpenCode Interception Adapter: Implementation Plan

## Scope and status

This is an implementation plan, not evidence that an adapter exists. Implement one runtime adapter: an OpenCode plugin that intercepts OpenCode-managed tool execution and connects it to the shared control layer. Keep policy, Task Contract, trajectory supervision, audit persistence, and outcome verification outside the runtime-specific adapter. Do not claim integration with other agent runtimes.

The available development environment reports OpenCode `v2.0.22`. The [OpenCode plugin API](https://opencode.ai/docs/plugins/) documents the relevant hooks, but that does not establish that this adapter has been tested. Pin the version selected for the demo and validate hook payloads and enforcement behavior against it before claiming compatibility.

## Repository workflow

- Development takes place on `main`. Preserve existing history; do not reset, replace, merge, or cherry-pick branches without direction from the team.
- Use the repository's `uv` project and lockfile for Python environment, application, and test commands. Do not substitute raw `pip` or an ad-hoc virtual environment. The OpenCode plugin itself follows the runtime's supported JavaScript/TypeScript mechanism; `uv` does not replace that toolchain.
- Use synthetic fixtures only. Never read or log real credentials, private keys, or unnecessary PII.

## Vertical implementation slices

### Initial coverage inventory (pending runtime validation)

| Action path | Planned mediation | Current status |
|---|---|---|
| OpenCode-managed built-in/custom tool invocation | OpenCode before/after hooks | Planned; verify exact coverage and pre-execution blocking on the pinned version. |
| MCP tool exposed through OpenCode | OpenCode hook, if the tool call follows the hooked execution path | Unverified; do not count as covered until a synthetic call proves interception before the upstream side effect. |
| Direct LLM/provider request | Separate LLM gateway | Uncovered by the OpenCode tool adapter. |
| Direct MCP connection or JSON-RPC request | MCP proxy with no direct route around it | Uncovered unless routed through that proxy. |
| Shell command and its subprocess/filesystem/network effects | Hook may observe the OpenCode tool invocation; OS-level controls constrain effects | Not completely mediated by inspecting the command string. |
| Alternate plugin or execution path that bypasses the hook | Separate runtime/OS boundary | Uncovered by this adapter. |

Keep this inventory current as hooks are tested. Report per-tool evidence rather than treating a generic hook registration as proof that every tool is intercepted.

### 1. Verify the OpenCode interception point

- Build a minimal plugin against the pinned runtime and verify `tool.execute.before` / `tool.execute.after` names, input/output shapes, ordering, and error behavior.
- Use a synthetic test tool with an observable invocation marker. Verify a before-hook rejection prevents the tool body from running.
- Verify which arguments the before hook can safely modify and whether the after hook can alter results before they return to the model. Treat unknown or observation-only result behavior as unsupported for redaction.
- Record which built-in, custom, and MCP-backed tools pass through these hooks. Do not infer coverage for direct LLM traffic, network effects, subprocesses, or other plugins.

### 2. Define the adapter-to-control contract

- Normalize each proposed action into a shared request containing a stable action/call identifier, run/session identity, adapter/runtime identity, tool name, arguments, trusted Task Contract reference/context, budget/accounting state, and the policy version bound to the run.
- Keep agent-provided text and tool arguments as untrusted data. The agent cannot create or change its own contract, approval state, or policy identity.
- Define a response contract for `ALLOW`, `BLOCK`, `MODIFY`/`REDACT`, and `REQUIRE_APPROVAL`, including a reason/violation code and policy-version traceability.
- Resolve decisions as `BLOCK` (including exhausted budget) > `REQUIRE_APPROVAL` > `ALLOW`; `ALERT` is additive evidence, never authorization. Validate approval against the finalized action, run, base policy version, and active overlay version; any applicable change invalidates approval and requires a new decision. Approval must be an actual wait/hold, never an allow-by-default.
- Apply configured argument transformations in order, re-run deterministic hard checks against the final arguments, and record every auditor decision (including `ALERT`) in sanitized evidence.
- Bind each run to an immutable policy snapshot. New validated policy versions apply to new runs; emergency tightening of an active run must be an explicit scoped overlay, not a silent Task Contract rewrite.
- Reject malformed/partial policy reloads and retain the last valid snapshot; emit old/new policy-version traceability for successful reloads and a sanitized failure event for rejected candidates.

### 3. Enforce synchronously before tool execution

- Have the plugin submit each hooked action to the local control layer and await its decision before allowing execution.
- Apply deterministic hard constraints first. A semantic assessment may add context but cannot override a deny, missing approval, or exhausted budget.
- Define and test policy-service timeout/unavailability behavior. Governed actions must not proceed if an authoritative required check cannot be completed.
- Ordinary telemetry may be asynchronous. Before high-impact dispatch, wait for the control layer to confirm that sanitized intent/decision evidence committed durably; storage failure or backpressure pauses dispatch. The adapter must not use volatile `emit_action_nowait` for critical evidence. Analytics delivery stays outside the dispatch path.

### 4. Observe results and keep verification independent

- Capture tool success, failure, or rejection through the validated after-hook/event path and associate it with the original action and policy version.
- Use an explicit event-field allowlist; exclude raw arguments/results, credentials/authorization headers, and unbounded error text by default. Scrub secrets/PII and bound any necessary summaries before enqueueing. Test that synthetic secrets are absent from persisted events while correlation and policy evidence remain.
- Never treat submitted arguments, tool output, an HTTP status, or the agent's success message as proof of a persisted business outcome.
- For workflows with side effects, add an independent verifier that reads trusted external state and checks explicit postconditions, including uniqueness where relevant. Report verified, failed, and unable-to-verify distinctly.

### 5. Document coverage boundaries

- OpenCode hooks mediate only actions routed through the validated hook path; a plugin can be disabled or bypassed by alternate configuration.
- Treat shell command text as an observed request, not complete control over every OS/network effect. Use OS isolation and restricted credentials/network routes when required.
- Provider requests need a separately configured LLM proxy. MCP protocol enforcement requires an MCP proxy and no direct route around it. Neither is implied by this runtime adapter.
- Maintain the coverage inventory above and label paths not enforced by the adapter as uncovered.

## Acceptance tests

Use deterministic synthetic tools and fixtures to verify:

1. An allowed tool call executes once and its policy version/decision are recorded.
2. A denied call never enters the tool body or causes its synthetic side effect.
3. A policy-approved argument modification is exactly what the tool receives; unapproved modifications are rejected.
4. Missing, rejected, expired, or mismatched approval does not execute the action; valid approval is bound to the exact action and policy version.
5. Tool-call budgets are accounted centrally; the call that would exceed the configured limit is denied before execution, and usage is traceable per run.
6. Tool result and error observations are correlated to the initiating action, sanitized, and delivered asynchronously.
7. Policy-service timeout/unavailability follows the configured fail-closed behavior for governed actions.
8. Hook behavior is checked for the pinned OpenCode version, including built-in, custom, and MCP-backed tool coverage that the demo relies on.
9. Tests explicitly distinguish what the plugin sees from unmediated shell/network/provider paths; do not count observation-only evidence as enforcement.
10. An independent verifier detects wrong persisted state, duplicate side effects, false success, and verifier-unavailable status for the selected end-to-end workflow.
11. Conflicting auditor outputs prove deny/approval precedence, additive alert recording, ordered transformations, and hard-check revalidation of the final arguments.
12. Concurrent policy reload tests prove an in-flight run remains on its bound policy snapshot, emergency overlays are explicit, and malformed candidates do not replace the last valid policy.

Run Python setup and tests with the repository's `uv` workflow. Use OpenCode's supported runtime/tooling for plugin-level tests. Report exact commands and results; mocks validate adapter behavior but do not prove enforcement against bypasses or verify a live external business system.

Prerequisite: make the `uv` executable available before running Python project tests; do not silently substitute `pip`.

## Completion criteria

- The plugin blocks at least one synthetic action before execution and allows one legitimate action through the same policy path.
- The adapter translates runtime events and decisions without becoming the policy authority.
- Policy version, action, decision, approval/intervention, cost where available, and sanitized outcome are traceable.
- An independent state-based verifier checks the demonstrable side effect.
- Coverage boundaries, test evidence, and unresolved bypass risks are reported accurately.
