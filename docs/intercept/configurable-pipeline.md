# Configurable auditor pipeline: implemented milestone and remaining roadmap

All control-plane code and tests are Python/asyncio/uv. JavaScript is restricted
to `adapters/opencode/`. Read `intercept/AGENTS.md` before contributing.

## Run this milestone

With `uv` available and a securely supplied `INTERCEPT_TOKEN`:

```sh
uv run --locked python -m intercept.service.server --policy config/intercept.demo.yaml --audit /tmp/opencode/intercept-demo.jsonl --trace
uv run --locked pytest tests/support/test_policy.py tests/support/test_server.py tests/support/test_auditors.py -v
node --experimental-vm-modules --test adapters/opencode/test.mjs
```

The last command tests the necessary adapter only. It is a mock callback harness,
not a real OpenCode terminal integration test. The YAML fixture is synthetic and
binds `synthetic-session`; a trusted operator must bind the actual OpenCode session
outside the governed agent's writable checkout. Do not enable this fixture for
the development agent and expect every tool to be allowed.

`--trace` prints metadata-only pipeline events as requests arrive. Each auditor
has an id, decision, fixed code, and latency; raw arguments and transformations
are excluded from both terminal output and persisted evidence. A changed payload
is returned to the adapter for execution, not logged.

## Configuration and actual controls

`config/intercept.demo.yaml` selects an ordered pipeline. Runtime validation is in
`intercept.policy.config` and `intercept.policy.auditors`; `config/intercept.schema.json` is the
editor/documentation schema. Duplicate YAML keys, unknown fields/types, duplicate
auditor ids, malformed configurations, and invalid webhook responses fail closed.
The legacy `{runs: ...}` JSON policy still works with an empty optional pipeline.

Built-ins:

- `tool_allowlist`: additional exact tool-name restriction.
- `pattern_scanner`: literal signatures over nested argument values; configurable
  BLOCK, REDACT or additive ALERT.
- `classified_scanner`: checks arguments against classified data sensitivity patterns.
- `domain_blocklist`: blocks requests attempting to communicate with untrusted network domains.
- `webhook`: optional Python/polyglot auditor using loopback HTTP, no proxy-env or
  redirects, bounded response and timeout. Response is exactly `{"decision": ...}`
  with ALLOW/BLOCK/ALERT/REQUIRE_APPROVAL. Webhook REDACT is not supported yet.

The initial built-ins are named implementations selected by config. In addition, typed in-process
pre-dispatch plugins are supported via `intercept.plugins.ActionAuditorPlugin` (e.g. `BudgetGuard`,
`PatternMatch`, `VelocityGuard` in `plugins/`). Custom external auditors can implement the webhook contract.

Mandatory run identity, tool authority, task scope, approval and budget checks run
before optional auditors and again against final arguments before admission. These
cannot be disabled by configuration of the optional chain. Hard denials skip the
optional pipeline; hard denial inside the pipeline skips later external work.
All configured transformations are ordered. ALERT never grants authority.

REQUIRE_APPROVAL remains an enforced block until the real operator approval API
exists. A config reload requires restarting; policy hashes now include auditor
configuration, but no atomic hot reload or overlay API exists yet.

## Full architecture completion checklist (not implemented by this milestone)

1. **Trusted lifecycle/contracts:** operator run creation/session binding, original
   objective/provenance, protected admin authentication, stable action fingerprints.
2. **Real OpenCode demo:** isolated V2 config/launcher, validate plugin loading,
   built-in/custom/MCP hook propagation, tool registry dispatch and independent
   SQLite KYC outcome verification. Prove deny never executes a tool body.
3. **Auditor extension/decisions:** typed in-process API, selective semantic review,
   full redaction policies/output filtering, exact-action approval pause/resume,
   invalid/stale/expired approvals and final-argument integrity tests.
4. **Protocol paths:** LLM request/response proxy and SSE; include auxiliary model
   requests or disable them; MCP tools/resources/prompts and HTTP egress mediation.
   Identify or restrict all unproxied routes; hooks do not mediate OS side effects.
5. **Routing/accounting:** model allowlists, token/cost/time quotas, usage reservation
   and reconciliation, safe provider retry/fallback. Never transparently retry a
   possibly executed side effect or switch providers after streaming output starts.
6. **Policy updates:** atomic validation/activation, bound run versions, explicit
   monotonic tightening overlays and approval invalidation, last-good rollback.
7. **Persistence:** versioned normalized events, durable admission/run accounting,
   reliable document persistence and consumer outbox, ACK/retry/jitter/DLQ,
   graceful shutdown and recovery. Define crash-loss windows honestly: non-blocking
   RAM ingestion cannot guarantee zero event loss on process crash.
8. **Supervision/reporting:** consumer metrics/costs/loops/trajectory/webhooks,
   authenticated bounded feedback, terminal/SSE/dashboard read APIs, test-status
   and scenario replay endpoints using real evidence rather than hardcoded counts.
9. **Handoff/deployment:** reproducible uv setup, pinned adapter dependencies,
   single documented suite, config reference, coverage inventory, local all-in-one
   demo first, then Compose/sidecar packaging without claiming isolation by default.

Remaining limitations also include in-memory replay/budgets, lossy JSONL ingestion,
no independent business verification in the intercept path, and the handcrafted
HTTP server's limited transport support. Replace that parser with a maintained
async Python HTTP primitive before expanding to SSE, proxies, or public-facing use.

No terminal OpenCode run catching **all** configured model/tool/MCP requests has
yet been demonstrated. That is the acceptance target, not the status of this slice.

## Validation of this milestone

Executed with isolated uv 0.9.0/Node 22.20.0 under `/tmp/opencode/intercept-tooling`:

- `uv run --locked pytest tests/support/test_policy.py tests/support/test_server.py tests/support/test_auditors.py -v`: tests pass.
- `node --experimental-vm-modules --test adapters/opencode/test.mjs`: **6 callback-harness tests pass**.
- `git diff --check`: passed.

During development, the mock webhook success test initially failed due to
case-sensitive parsing of `Content-Length` in its fixture. The fixture was fixed
and the complete suite rerun. Also fixed an admission-order hazard: transformations
must not turn an originally unauthorized task scope into an allowed one. Both the
original action and final payload now pass mandatory checks, with a regression test.
