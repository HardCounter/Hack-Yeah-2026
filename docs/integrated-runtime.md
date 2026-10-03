# Integrated local KYC runtime

The executable composition is `simulation/governed.py`. `simulation.agent.Session`
uses it by default for both scripted and local LLM drivers. Lower-level registry
fixtures remain available explicitly to test corrupted business state.

```text
trusted runtime -> contracts.TaskContract -> persisted immutable contract
agent proposal -> contracts.ActionProposal -> intercept.GovernedGateway
                    -> canonical GatewayDecision
                    -> persistence.GovernedPersistence
                    -> committed Event Envelope v2.1 + session seq + outbox
                    -> PersistenceEventSource -> ConsumerManager
                    -> trajectory-risk / gateway-violations / outcome-verifier
                    -> Finding / metrics / PolicyAdjustmentSignal
                    -> trusted in-process feedback channel -> Layer 1 -> durable control event

persisted session ended -> read-only bank snapshot -> VerificationResult
```

## Run and test

```sh
uv run python data/generate.py
uv run python -m simulation.agent APP-0001 --driver scripted
uv run python -m simulation.agent APP-0003 --driver scripted --fault skip_step:screen_sanctions
uv run pytest
node --experimental-vm-modules --test adapters/opencode/test.mjs adapters/opencode/forward.test.mjs
```

Each demo uses an isolated bank copy. Its sibling evidence and consumer ledger
files contain the durable trajectory, delivery jobs, findings, feedback provenance
and verification result. `simulation/policy.json` is the central policy for this
composition: tools, model IDs, ceilings, signature scanning, risk thresholds and
feedback permissions. The separate consumer YAML is replay/runtime wiring,
not an additional policy source for governed runs.

Validated on 2026-10-03: `uv run pytest -q` passed 280 tests, with the live Ollama
test skipped because localhost Ollama was unavailable. The Node adapter harness
passed 16 tests. Standalone scripted CLI runs on fresh synthetic databases returned
`VERIFIED_SUCCESS` with one client for APP-0001, and `VERIFICATION_INCOMPLETE` with
zero clients and exit status 1 for APP-0003 when sanctions screening was omitted.
Both runs drained their consumer delivery jobs. Cross-layer tests additionally
exercise feedback, scope drift, budget exhaustion, redelivery, a real subprocess
delivery restart, concurrent client creation and interrupted evidence writes.

## Boundaries

`contracts.TaskContract` is the only contract definition. Consumer imports remain
aliases. The trusted runtime creates it, binds principal/agent/session/targets and
pins the full policy hash. Requests cannot select a database, change identity,
increase a ceiling or replace a contract. Related document/registry access is
resolved to the authoritative application target through protected linkage.

Layer 1 creates action/event IDs, timestamps, decisions and sanitized metadata.
Layer 2 alone assigns `seq` and run evidence indices. Internal uppercase persistence
enums and schema `2.0` remain the internal storage format; the sole consumer wire format is
`2.1`, produced by `persistence/adapters/consumer_v21.py`. Unsupported mappings
raise. The consumer decoder remains unchanged. `action_id` is an additional
top-level correlation field, stable across intent, result and receipt evidence.

High-impact dispatch awaits durable `PENDING` intent evidence. Intents have no
consumer `seq`, are never delivered as completed events and are excluded from
trajectory views. Final evidence and consumer jobs commit together. Durable
claims serialize outstanding deliveries within each consumer/session; ACK removes
the job, NACK retries, exhausted retries enter the existing DLQ. Completion ledgers,
finding IDs, signal IDs and business receipts make redelivery idempotent.

Local model requests reserve a conservative input bound plus the configured output
cap before dispatch. Failed or cancelled attempts retain that charge. Model calls
hold the same session fence as governed tools, feedback and finish; cancellation
waits for an outstanding backend before recording a failed result. Model telemetry
uses conservative token estimates rather than a claim of tokenizer-exact usage.

Feedback accepts only authenticated session-scoped tightening: `ALERT`,
`REQUIRE_APPROVAL_FOR`, `BLOCK_TOOLS`, `STRICT_MODE`, `HALT_SESSION`. The explicit
channel adapter translates legacy internal proposal keys into canonical `tools`.
Layer 1 revalidates scope, tool sets, TTL and trigger evidence, persists control
evidence, then applies restrictions. It never grants approval or expands authority.
`REQUIRE_APPROVAL` holds execution; no approval-granting UI is provided by this slice.

Generic events exclude identity bodies, prompts, OCR, completion bodies and raw
errors. Optional bodies use integrity-checked `ContentRef`s in Layer 2, with
`untrusted` as the default; plugin context enforces `needs_content`. Finding storage
keeps structured evidence and fixed codes rather than arbitrary free text.
Protected bank baselines and successful screening evidence serve verification;
they are separate from generic telemetry and stay inside the synthetic bank copy.

The lifecycle verifier uses a separate read-only consistent bank snapshot, checks
the existing deterministic `data/postconditions.py` plus baseline identity,
client/account links, transaction receipt and process provenance. It reports only
`VERIFIED_SUCCESS`, `FAILED_POSTCONDITIONS`, `VERIFICATION_INCOMPLETE`, with fixed
per-check details. A failure discovered after execution is not called a block.

## OpenCode controlled tool mode

`intercept.local` reuses the HTTP transport and the same governed runtime. The
operator selects the synthetic source bank, application and contract. An admin
binding creates one isolated run; model input cannot choose the bank path.

```sh
uv run python -m intercept.local --bank-db data/bank.db --application APP-0001 \
  --contract-id contract_APP0001_v1 --runs-dir data/runs
```

Supply `INTERCEPT_TOKEN` and a distinct `INTERCEPT_ADMIN_TOKEN` securely to the
service and OpenCode; never commit or print them. Use the existing plugin's
`registerTools: true`, `contractId: "contract_APP0001_v1"`, and loopback `endpoint`.
Its registered tools call `/v1/tools/execute`; Python normalizes `call_id` into a
stable `action_id` and injects trusted identity. `/v1/session/finish` persists the
lifecycle boundary and returns independent verification. The new HTTP integration
test exercises authentication, execution, denial, persistence, consumption and finish.

The merged `intercept.receiver` remains an observe-only diagnostic server. It is
not the integrated enforcement service. OpenCode prompt-hook forwarding and live
hook behavior are separate from the governed simulation's local model execution.

## Limits

This is a cooperative local synthetic demonstration, not an OS sandbox or a live
financial system. A same-user agent can bypass in-process Python unless deployment
permissions prevent it. The deterministic trajectory-risk model is heuristic;
no new semantic model or risk-model rewrite is introduced. Live Ollama/OpenCode
conversation behavior must be reported separately from mocked/offline tests.
Active tool sessions cannot resume after gateway restart; durable evidence and
outbox delivery do survive restart. No new broker, database or framework is needed.
