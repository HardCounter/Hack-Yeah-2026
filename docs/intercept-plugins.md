# Layer 1 interception plugins

`plugins/` contains trusted **pre-dispatch** auditors, not consume-plane processors. The
implemented interface is `intercept.plugins.ActionAuditorPlugin`: synchronous `setup(config)`
and `evaluate(AuditContext) -> AuditDecision`, matching application-documentation §5.2.
The gateway provides the bound contract, agent/session/action identity and policy version;
raw payloads remain transient. No private chain-of-thought is requested.

## Registration and execution

`plugins/__init__.py:PLUGINS` is the fixed registration list, read by
`intercept/plugins.py:registered_plugins()`. Adding a plugin requires a code-reviewed import in
that list; API configuration cannot choose executable modules or paths. Both plugin files also
expose their individual `PLUGINS` lists for explicit developer enumeration.

The auditor pipeline runs registered deterministic plugins before its existing scanners and
webhooks. A hard `BLOCK` cannot be overridden by approval or semantic results; subsequent
webhook work is skipped. Plugin failures/invalid decisions fail closed with
`INTERCEPT_PLUGIN_FAILED`. Existing task scope, tool permissions, approvals and budgets still
apply. This covers only proposals entering the gateway, not all OpenCode provider/OS traffic.

Input/output phases are explicit. These two plugins operate on **input only**; existing output
scanners remain in place. Blocking an output after execution cannot be called prevention.
PromptGateway uses the same pattern plugin for mediated model inputs, but velocity counts only
tool/MCP proposals, not model calls or result inspection.

## Configuration

The named JSON presets contain:

```json
{
  "intercept": {
    "pattern_match": {
      "enabled": true,
      "patterns": ["(?i)ignore\\s+previous\\s+instructions", "(?i)system\\s+override"],
      "fields": ["tool", "arguments"],
      "action": "BLOCK"
    },
    "velocity_guard": {"enabled": true, "window_s": 10, "max_calls": 8}
  }
}
```

The example is a section, not a complete PUT body. Full defaults live in
`config/presets/{lenient,standard,strict}.json`. The legacy YAML loader also supports an
`intercept` plugin section; see `config/intercept.demo.yaml` and `config/intercept.schema.json`.

Save/select via the [config REST API](rest.md#418-configuration-reads-and-put-configsname).
New sessions pin the selected snapshot; existing sessions are unchanged. Old snapshots lacking
`pattern_match` keep regex matching disabled, and missing `velocity_guard.enabled` means enabled.
Optional fields are not injected into stored JSON, so old revisions remain valid. Existing
state is not reseeded: to enable regex in an older saved preset, explicitly save/select it.
VelocityGuard's previously stored thresholds now enforce Layer 1 velocity on new sessions.

### PatternMatch (`pattern-match`, version 1.0.0)

- At most 64 regexes, each 1–256 characters. Empty list means no blacklist.
- RE2 **search** semantics, case-sensitive unless a regex includes an inline flag such as `(?i)`.
- Fields: `tool` (action name) and/or `arguments` (nested string values **and dictionary keys**).
  Match strings individually; do not concatenate unrelated fields or stringify arbitrary values.
- Any match returns `BLOCK` for that invocation. It does not permanently blacklist the tool.
- Invalid regexes, unsupported backreferences/lookaround, duplicate/unknown fields and non-BLOCK
  actions are rejected before activation. Disabled configs must also be valid.
- RE2 avoids exponential backtracking. Compilation is capped at 1 MiB per regex; evaluation
  examines at most 65,536 UTF-8 bytes, 4,096 nodes and depth 32. Oversized/invalid inputs block
  rather than being truncated or allowed (`REGEX_INPUT_LIMIT`, `REGEX_INPUT_INVALID`).
- Evidence: fixed `REGEX_PATTERN_MATCH` reason plus `regex.pattern.<index>` rule ID, never the
  regex text or matching payload. Indices refer to the pinned ordered config.

Dependency: `google-re2`, locked in `uv.lock`. RE2 and its Python wrapper use the **BSD-3-Clause**
license; checked at https://github.com/google/re2/blob/main/LICENSE and
https://github.com/google/re2/blob/main/python/LICENSE (the latter refers to the former).
No upstream code is vendored; retain the dependency's license notices when distributing it.

### VelocityGuard (`velocity-guard`, version 2.0.0)

- `window_s`: finite number in `(0, 3600]`; `max_calls`: integer in `[1, 10000]`.
- Uses a server monotonic clock and session-scoped history, not agent timestamps.
- Counts distinct tool proposals reaching this pipeline; a proposal may later be denied by
  another control. Proposals rejected before the pipeline are not counted.
- Once the window is full, the next new proposal returns `REQUIRE_APPROVAL` before dispatch
  (`VELOCITY_EXCEEDED`). No real human approval/resume workflow is added here; the current
  gateway withholds execution. Rejected rate-limit attempts do not extend the window forever.
- Reinspection of an already-counted action does not count twice. Outputs and LLM requests do
  not count. Each session retains at most `max_calls` entries; at most 128 active sessions per
  instance, with a hard `VELOCITY_STATE_LIMIT` on overflow. Histories expire at window boundaries.
- State is in-process, not restart-durable; this is a per-session call-rate gate, not a global
  persisted budget or exactly-once mechanism. Existing budget and receipt controls remain separate.

### BudgetGuard (`budget-guard`, version 1.0.0)

- It receives the existing **top-level** preset `budget` object. The preset schema is unchanged;
  no budget fields are moved under `intercept` or into a plugin list.
- One instance is shared by tool and prompt pipelines for one governed runtime/session. It keeps
  bounded in-memory token and tool-call reservations and exposes the used/limit counters as
  sanitized numeric auditor evidence. Its state is discarded when that runtime closes.
- Prompt tokens use the current conservative reservation: serialized input byte count plus
  `max_output_tokens`. The reservation is committed after input auditors allow and before durable
  intent/provider dispatch. Failed or ambiguous calls are not refunded. PromptGateway's existing
  persisted-usage check remains an independent hard backstop.
- Tool calls are charged once after auditor/policy admission and before any effect dispatch. The
  existing `Policy.tool_call_budget` remains an independent hard backstop. Calls denied by an
  earlier scanner or approval gate are not charged by the plugin.
- Repeated reservation for the same action ID is idempotent; conflicting reuse is blocked.
  Reservations are capped at 4,096 IDs per session; overflow fails closed.
- `cost_usd: null` means no configured dollar ceiling. A positive dollar ceiling is not
  enforceable yet: there is no trusted price table or integrated trusted provider usage. It
  remains fail-closed for model dispatch with `LOCAL_MODEL_COST_BUDGET_UNSUPPORTED`; the plugin
  does not invent spend measurements. Zero-cost local inference is permitted.

## Consume-plane separation and tests

`consume_plane.yaml` no longer discovers `plugins/` or registers VelocityGuard. Layer 3's
trajectory risk, gateway violation analytics and independent outcome verifier remain separate.
The consumer SDK example is now a **test fixture**, `tests/consume_plane/fixtures/plugins/velocity_observer.py`;
its after-event feedback never claims to prevent its triggering call.

```sh
uv sync --locked
uv run --locked pytest -q tests/test_intercept_plugins.py tests/test_configuration_api.py tests/consume_plane
```

Tests cover nested/key/name matches, invalid regexes, bounded adversarial inputs, disabled
plugins, fail-closed errors, velocity concurrency/session isolation, duplicate inspection,
old snapshot hashes, actual tool non-execution and persisted sanitized denial evidence.
