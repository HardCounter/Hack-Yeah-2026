# Runtime architecture contract

Status: **required design, not implemented or runtime-tested**. This document resolves
the architecture contradictions identified in the October 3 review. It specializes
[project-direction.md](project-direction.md); competition documents remain unchanged.
The MVP is **one KYC workflow**. AML, broad provider compatibility, distributed queues,
automatic fallback, and arbitrary plugins are deferred. Technology suggestions in other
documents are not installed dependencies or implemented capabilities.

## 1. Trusted instruction and execution boundary

A trusted orchestrator creates the Task Contract outside the governed agent. A dashboard
prompt, OCR document, tool result, retrieved page, or model response cannot grant authority.
The contract is mandatory and immutable, and includes:

- Contract/run/session/case identity, authenticated principal and declared role.
- Original objective and approved instruction, with issuer, timestamp and provenance.
- Target application, related document/registry/UBO resource IDs and initial decision state.
- A protected baseline of approved identity fields, document expiry and registry facts,
  with snapshot/version identifiers. Untrusted extractions are proposals, not this baseline.
- Allowed tools, models, destinations and expected side effects.
- Hard constraints, required approvals, token/tool/time/cost ceilings.
- Explicit persisted-state invariants, uniqueness key and verifier strategy/version.
- Policy identity, content hash/version and signature-feed version.

The authenticated principal is bound server-side to the contract. Correlation headers are
trace hints, not authentication or authorization; reject conflicting IDs. The gateway
allocates action IDs and ordering. Tools receive identity from the gateway, not model-filled
arguments. The agent cannot register contracts, change modes, grant approvals or reset budgets.

Changing `base_url` alone is **cooperative interception**, not bypass prevention. A contained
demo must isolate the agent from upstream credentials, tool implementations, writable bank
files, ground truth, policy files and administrative endpoints. Only the gateway can reach
tool/LLM backends; backend authentication and OS/network permissions enforce that separation.
A same-user, unsandboxed agent is outside this containment claim. Test direct-backend denial
before claiming interception cannot be bypassed. A sidecar alone is not a security sandbox.

## 2. Synchronous decision path

```text
authenticated proposal + pinned contract + protected current state
  -> schema/size checks, scoped access, hard invariants, signatures, input data controls
  -> atomic resource reservation (including guard-model work)
  -> selective semantic trajectory assessment when the policy requires it
  -> exact-action approval check when required
  -> durable action intent + final hard/state recheck
  -> backend execution and persisted business effect
  -> output inspection before delivery + usage reconciliation + durable result
  -> independent persisted-state verification
```

Hard denies, insufficient budgets and missing approvals take precedence over any semantic
ALLOW, warning or redaction. Compose decisions conservatively; unavailable mandatory
auditors, invalid responses or missing required state pause/block the action. REDACT requires
revalidation of the transformed payload. Do not silently redact/change approved identity
fields or economic terms; reject and request a trusted correction instead.

Semantic supervision assesses observable history, source provenance, original objective,
proposed action and relevant trusted state. Run it selectively before `create_client` and
other policy-selected high-impact operations or suspicious detours. It is AI-based and
non-deterministic even at low temperature. Do not request private chain-of-thought. Log
only structured risk evidence, reason codes and the guard model/version. A deterministic
test stub is not live semantic inference. Required semantic-service failure pauses/blocks;
it must not fall back to ALLOW. Async consumers can add restrictions to **later** calls,
but cannot protect an action already forwarded or replace the synchronous gate.

Inspect tool results and model responses before releasing them to the agent. Buffer output
in the MVP; do not stream unchecked chunks. Any future streaming implementation must prove
cross-chunk detection and bounded buffers before enablement. Scan results, exceptions,
webhook bodies and audit exports too. Untrusted content remains data even when allowlisted.
Known-pattern scanning is not comprehensive prompt-injection protection.

## 3. Central policy and explicit interventions

Use one validated configuration source for allowed tools/models, data-flow rules,
thresholds, budgets, approvals, expected side effects, verifier selection and signature-feed
references. Separate examples are sections of that source, not competing policy files.
Only authenticated administrators can change it. Plugin paths, webhook targets and upstream
destinations are trusted deployment configuration, never agent-controlled values.

Validate new snapshots atomically; reject invalid reloads and retain the last valid version.
New sessions bind to the new version; existing sessions retain their contract's snapshot.
Every decision records policy and feed versions. Emergency halt/restriction is an explicit,
authenticated, audited intervention referencing the pinned policy and an intervention ID;
it can tighten or pause a run, never silently expand authority. Resuming with different
authority requires a trusted contract revision/new session and fresh approvals. Async
feedback can only request tightening, cannot edit trusted instructions or undo hard denies.

Strictness toggles affect new sandbox sessions. Judge prompt input cannot choose an identity
or lower enforcement in an active session. Public demo access is read-only/sandbox-limited;
admin policy and approval endpoints require separate authentication and authorization.

REQUIRE_APPROVAL means **no execution yet**. Approval records bind authorized reviewer,
run/contract ID, action ID, canonical tool/arguments digest, policy version, relevant state
version, expiry and single-use nonce. Use protected storage and atomically consume the
approval with dispatch authorization. Reject replay, expiry, substitutions and stale state;
recheck hard rules and budgets after approval. An approval never overrides a hard deny.

## 4. Resource governance, retries and durable evidence

Maintain atomic, protected reservation ledgers per run and shared service. Reserve conservative
input/output token bounds, cost and concurrency/time limits **before** each backend attempt;
enforce maximum output tokens and tool-call limits synchronously. Include semantic calls,
retries, fallback attempts and local-model compute limits. Reconcile actual usage; retain
conservative charges for unknown usage after timeouts. Unknown pricing/usage must not create
unlimited free calls. Async cost observers report spend; they do not enforce hard ceilings.

No transparent retry/fallback for side-effecting tools. A timeout after dispatch is an
ambiguous outcome: query persisted state using the original action/idempotency key before
any retry. Derive uniqueness from the business application ID, not just a session or an
agent-selected key. Atomically transact client/account creation, application status,
business-key uniqueness and a durable effect receipt; conflicting replays fail, identical
retries return the existing receipt without another effect. Concurrent sessions targeting
the same application must still produce at most one client. LLM retries/fallback, if added,
must recheck model/destination policy and reserve each attempt's budget.

Persist sanitized intent/decision evidence before high-impact dispatch and durable effect
receipts with the business transaction. Use a durable outbox for asynchronous analytics;
an in-memory queue alone does not guarantee evidence delivery. Record denies and approvals
as well as successful calls. Bound queues, retries and retention; pause writes on durable
audit failure/backpressure. Unknown outcomes remain unknown until state reconciliation.
Consumer retries deduplicate by event ID and cannot replay business effects.

Audit envelopes include schema/contract/run/action IDs, authenticated identity, policy/feed
versions, intervention/approval references, reserved/actual usage, decision/reason, timestamps,
semantic assessment metadata, effect receipt ID and verification status. Persist only
allowlisted sanitized fields. Never retain raw secrets/PII in blobs, exceptions, free-text
reasons or dashboard excerpts. Hashing a short identifier is not anonymization. Separate
agent-facing redactions from protected verifier state; any required sensitive baseline is
access-controlled and not part of general telemetry. Define retention and export access.

## 5. Independent outcome verification

The verifier uses a separate read-only connection/credential to the persisted bank state
and protected baseline/contract; never infer business success from HTTP status, submitted
arguments, tool receipts alone, event traces or an agent's “done” message. Quiesce/fence
writes before verification and read a consistent snapshot. Use a disposable database copy
per run; concurrency tests explicitly share an application to test global uniqueness.

Required KYC schema additions (not present in the generator yet): application-to-created-client
linkage with a unique application key; durable decision/effect receipts with action/run IDs;
server-recorded screening evidence bound to normalized subject, DOB, source/version and time;
and decision document/registry/UBO provenance. Enforce a unique client per application and
atomic related account/status writes. `audit_actions` records fake bait execution only;
its count does not by itself prove KYC correctness or real email/code/network safety.

Verify persisted identity fields against the protected approved baseline, application status,
expected client/account links, required screening and decision evidence, and client count
across sessions. Approved clean cases require **exactly one** linked client; escalated,
rejected or blocked-create cases require **zero** new clients. Compare initial/final scoped
state and receipts for unexpected side effects, not just the intended target row. Count
attempts/denies from the trace separately from committed business effects.

Separate outcome invariants (state checks) from historical/process checks (trusted screening
records and complete trace). Missing/corrupt trace cannot fabricate success for process
invariants. Report per-check evidence source and one of:

- `VERIFIED_SUCCESS`: all required outcome and process checks completed and passed.
- `FAILED_POSTCONDITIONS`: observed trusted state/evidence contradicts a required invariant.
- `VERIFICATION_INCOMPLETE`: unavailable state, schema mismatch, missing provenance/trace,
  unresolved write or verifier failure prevents a conclusion.

Post-run detection does not block or roll back an already committed effect. Display the
incorrect effect and any separately authorized remediation honestly. A replay verifies an
immutable persisted-state snapshot plus trusted baseline, not recorded events alone.

Generated ground truth is a synthetic test oracle, not production business authority.
Pin dataset/schema/rule/FX versions and snapshot hashes in run metadata; protect the oracle
from agent/tool writes. Reusing generator `rules.py` checks consistency but shares its bugs.
Hand-authored expected fixtures and boundary cases must independently test rule correctness.
No real external banking-system verification is claimed for this SQLite simulation.

## 6. Build and acceptance order

Use **uv** with `pyproject.toml` and committed `uv.lock`; Python 3.12 is the development pin,
and project metadata allows Python >=3.11. Dependencies are currently empty. Add only
dependencies actually selected for a slice and check licenses; no `requirements.txt` is needed.

1. Mandatory trusted KYC contract, centralized policy and isolated run fixture.
2. One gateway/tool path: scoped reads, deterministic create gate, atomic budgets and
   persisted exactly-once client creation with sanitized durable evidence.
3. Independent read-only state verifier: valid outcome, wrong identity, duplicate,
   missing/extra effect, false-success and unavailable-state cases.
4. Selective live semantic gate; deterministic stub tests plus clearly labelled live
   guard-model smoke tests, including required-service failure.
5. Minimal dashboard, policy/signature reload and approvals through the same path.

Before an end-to-end claim, execute positive and negative tests for authentication and
backend bypass; task resource scope; signatures and input/output redaction; approvals and
replay; concurrent budget reservation and restart recovery; policy pinning/invalid reload;
ambiguous retries and concurrent duplicates; audit loss/backpressure; semantic/verifier
failure; wrong persisted results and false success. Report actual commands/results and
versions, not the counts of planned scenarios. Extend coverage only after this slice works.
