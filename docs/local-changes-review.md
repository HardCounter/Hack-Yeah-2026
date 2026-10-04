# Local changes review — October 3, 2026

## Scope and conclusion

Reviewed the local-only documentation commits `d4650b8`, `75bf5da`, `ddbd255`,
the conflict-cleanup commit `5d00a2a`, `pyproject.toml`, and the originally
untracked `persistence/` and `tests/` additions against `GoldmanSachsRules.md`,
`GoldmanSachsCriteria.md`, `project-direction.md`, the architecture contract,
application architecture and adapter plan. Two GPT-6 Luna reviewers analyzed
correctness and requirement alignment read-only; the builder made and tested
the fixes. This review used available remote-tracking references, not a fetch.

During work, the original persistence package was committed as `c408d0e` and
the branch/upstream references advanced to `persistance` / `origin/persistance`.
Those repository mutations were not performed by this review. The corrective
work described here is left uncommitted. Unrelated changes were preserved.

The documentation direction fits the challenge's external hybrid control layer
and KYC MVP. The initial persistence implementation did **not** meet its stated
durability/privacy guarantees. The corrections make the standalone persistence
component usable with explicit limits; **the full project is not submission-ready**.
No implemented gateway currently invokes this component before business dispatch.

## Findings corrected

| Initial finding | Correction | Evidence |
|---|---|---|
| Memory enqueue counted as durable acceptance; a failed write discarded the dequeued batch | Critical emits atomically commit file-backed evidence and consumer jobs; volatile batches remain in flight until commit | Abrupt-process-exit recovery, transient write failure and explicit audit-failure tests |
| `flush()` observed an empty queue before the write finished; shutdown raced/cancelled work | Acknowledge only after commit; drain waits for ingestion completion and attached consumers; timeout raises without closing active storage | Stalled-write flush/shutdown test |
| Cancellation of `to_thread()` released the connection lock while SQLite could still be running | Shield the thread operation and retain serialization until it finishes | Cancelled write vs concurrent close test |
| Raw parameters, results, errors, modifications, reasons and DLQ payloads could leak | Central allowlist projection before persistence/fanout and on reads; structured error messages; owned subscriber snapshots | Synthetic leakage tests across events/alerts/audit/DLQ/live feeds and legacy reads |
| Duplicate IDs overwrote evidence through `INSERT OR REPLACE` | Exact sanitized retries are idempotent; conflicting reuse rejects and rolls back the whole batch | Duplicate/retry/atomic rollback tests |
| Queue 2 dropped jobs and retry exhaustion was manually routed by tests | Persistent registered consumers, leased jobs, ACK, jittered bounded retries and atomic DLQ transfer | Restart/resume, exhausted retry, stale ACK/lease expiry and abandoned-claim tests |
| Critical API could silently use an ephemeral database | Engine requires a file path; durable lower-level outbox also rejects memory-only/temporary databases | Ephemeral-store rejection tests |
| Unknown/missing verdicts became `ALLOWED`; policy version was invented | Invalid verdicts reject; absent binding/version stays absent | Malformed verdict and missing binding tests |
| No event context or authoritative-order query | Explicit context references; unique gateway-supplied run event index; `get_run_events()` reads index order | Context roundtrip, out-of-order arrival and conflicting-index tests |
| Async failures/overflow lacked usable reporting | Expose total backlog, rejected volatile events, live-feed drops/filter errors and worker error class | Telemetry/overflow checks |
| Adapter plan excluded all persistence from critical dispatch | Require durable critical evidence confirmation before dispatch while analytics remains async | Adapter documentation corrected to match normative contract |

`docs/persistence.md` specifies the actual API, trust boundary and limitations.
README and application diagrams now distinguish implemented SQLite primitives
from the planned gateway and separate durable analytics from volatile live feeds.

## Challenge and direction traceability

| Required capability | Observed state after this review | Work still required |
|---|---|---|
| Functional external intermediary | Tools and standalone persistence primitives exist | Implement and contain one authenticated KYC gateway/tool path; prove before-hook rejection prevents execution |
| Central policy with strictness/models/budgets/feed reload | Specifications and examples exist | Validated loadable config, policy pinning/reload, permissions, signatures and atomic resource reservation ledger |
| Deterministic enforcement | Synthetic dataset rules and tool logic exist | Runtime authentication/scope/identity/input-output/signature/approval checks with hard-deny precedence |
| AI-based trajectory supervision | Planned; optional LLM tool-calling check is not a semantic supervisor | Live selective pre-action guard and mandatory-service-failure behavior; keep deterministic stub evidence separate |
| Trusted Task Contract | Designed; persistence can carry references | Trusted creation/binding/provenance/approved baseline and externally authorized revisions |
| Exactly-once persisted KYC effects | Current tools and synthetic verifier are partial building blocks | Atomic business uniqueness/linkage/account/status/effect receipt and screened decision provenance across sessions |
| Independent outcome verification | `data/postconditions.py` checks the synthetic state | Integrate protected baseline and fenced read-only snapshot; prove wrong/missing/extra effects, false success and unavailable-state handling |
| Security reporting and interactive dashboard | Aggregate storage telemetry and query APIs exist | Interactive UI, authenticated exports, policy/security/cost metrics and drill-down traces through the actual enforcement path |
| Positive/negative executable self-tests | Existing tools/verifier tests plus persistence failure tests execute | End-to-end enforcement cases covering bypass, policy reload, approvals, budgets, injection and persisted outcomes |
| Reproducible demonstration and scalability | Component uses stdlib SQLite; a local append benchmark executes | One integrated launch/demo command, isolation evidence, bounded retention scheduler and concurrency/resource telemetry |
| Formal submission package | README has title/team information; documents outline the task | Confirm 1–6 members, eligibility/timing; description and ≤10-slide PDF; submit to HackTribe in English/Polish |

Rules allocate self-testing/scalability **20%/10%**, while Criteria allocate
**15%/15%**. Preserve both documents and request clarification from the organizer;
use formal Rules provisionally for submission scoring and meet Criteria's
technical deliverables. This review did not contact organizers or establish
eligibility, timing, submission or publication.

## Validation and limits

The original baseline was **51 passed, 1 skipped**. Corrected component tests
exercise synthetic data in disposable databases, including process exit after
commit, concurrent independent connections, retry/restart/cancellation boundaries,
explicit backpressure and telemetry privacy. No runtime dependencies were added.
SQLite and asyncio are standard library components; pytest remains development-only.

Executed checks and final results are recorded below. Local benchmark numbers
measure serialized durable-append throughput, not per-request latency or gateway
performance, and are not a cross-platform SLA.

| Command/check | Observed result |
|---|---|
| `uv run --locked --offline pytest` | **84 passed, 1 skipped**, 85 collected, 4.59 seconds; Python 3.12.11 / pytest 9.1.1 on macOS |
| `uv lock --check --offline` | Exit 0; resolved 7 locked packages |
| `git diff --check` | Exit 0 |
| Inline Python local-link and code-fence check | Changed Markdown links resolve and fences balance |
| `uv run --locked --offline pytest tests/support/test_persistence.py::TestPersistenceBenchmark::test_concurrency_smoke_benchmark_500_events -q -s` | One measured run during review: 500 committed events in 260.29 ms (0.5206 ms/event average throughput), test passed |

The skipped check was `sim/tools/test_ollama.py`: Ollama was unreachable at
`http://localhost:11434`. Linux/Windows CI was not executed locally. No private
data was sent to external services, and no commit/push/deployment/live financial
operation was performed by this review.

The persisted business result is still unverified by an integrated runtime.
Identity/policy/approval references are trusted caller assertions at this private
storage boundary, not authenticated evidence by themselves. Opaque identifier
issuance, deployment access restrictions, consumer idempotency and authorized
retention/export remain integration obligations. At-least-once analytics does
not provide exactly-once banking effects. Process-exit tests do not prove power
loss tolerance or production readiness. The unavailable Ollama service means
this review establishes no live semantic inference.
