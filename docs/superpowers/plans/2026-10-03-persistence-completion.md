# Persistence Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. This request publishes the plan; it does not start its implementation.

**Goal:** Complete the persistence responsibilities for one governed synthetic KYC workflow: trusted trace binding, durable intents, atomic business receipts, recoverable analytics, bounded operations and security reporting.

**Architecture:** Keep the stdlib SQLite audit store and durable consumer outbox. The trusted runtime supplies authenticated bindings; the banking transaction owns business receipts and a source outbox, which replicates sanitized notices into the audit store idempotently. A separate read-only verifier checks banking state; telemetry never substitutes for that check.

**Tech Stack:** Python >=3.11, development Python 3.12, stdlib sqlite3/asyncio/dataclasses/json/hashlib, uv and existing pytest. No new runtime dependency or distributed queue is proposed.

**Spec:** [runtime architecture contract](../../architecture-contract.md), [project direction](../../project-direction.md), [current persistence guarantees](../../persistence.md), [technical Criteria](../../../GoldmanSachsCriteria.md), [formal Rules](../../../GoldmanSachsRules.md). Inspect source before executing; this plan is based on persistence commit `b10abdc`.

## Global Constraints

- "The MVP is **one KYC workflow**." AML and broad provider compatibility remain outside this plan.
- "The authenticated principal is bound server-side to the contract." The issuer is the trusted runtime; storage does not authenticate HTTP requests.
- "Hard denies, insufficient budgets and missing approvals take precedence over any semantic ALLOW, warning or redaction."
- "No transparent retry/fallback for side-effecting tools." Outbox replay replicates audit evidence, never calls `create_client` again.
- "Consumer retries deduplicate by event ID and cannot replay business effects."
- "Persist only allowlisted sanitized fields." Protected approved identity and screening subjects stay in the banking/verifier boundary.
- "Missing/corrupt trace cannot fabricate success for process invariants."
- "Use **uv** with `pyproject.toml` and committed `uv.lock`; Python 3.12 is the development pin, and project metadata allows Python >=3.11."
- Preserve unrelated work; never read credentials or real `.env` files. Use disposable synthetic databases. Check licenses if a dependency is actually introduced.
- Keep implemented, tested and demonstrated claims distinct. No automatic commits, pushes, deployments or live financial operations during future execution without applicable user authorization.

## Current state and scope

Already implemented and tested: sanitized immutable evidence; atomic audit-store/outbox commits; file-backed critical emit; transient volatile-batch retention; consumer leases, bounded retry and DLQ; conflict rejection; unique supplied run indices; explicit timeout and overflow reporting. Baseline command `uv run --locked --offline pytest` returned **84 passed, 1 skipped**; the skipped check required unavailable Ollama.

The following are genuinely missing or incomplete: schema migration/version checks, an enforced trusted binding writer, business receipt replication, worker/consumer lifecycle controls, scheduled retention and capacity telemetry, and bounded authorized export. Current `create_client` already writes `clients.application_id`, client/account/status changes share `registry.call()`'s transaction, and `data/postconditions.py` already exists. Reuse those pieces; do not claim the linkage or verifier needs to be invented from nothing. Application uniqueness, durable effect receipts and protected process evidence are still missing.

Persistence must **not** own model inference, semantic judgment, central policy authoring, approval authorization, budget decisions, or the full dashboard. Those components consume the interfaces below. Two storage files cannot be made one crash-atomic transaction by appending separately; the source banking outbox is required. Do not claim cross-file WAL atomicity.

## Review Focus

1. Reentrant analytics during shutdown must not wait on a lifecycle lock held by shutdown; test in Task 4.
2. A crash after the banking commit but before audit replication must converge without another client; test in Tasks 3 and 7.
3. A malformed legacy record must neither leak raw text nor jam all healthy deliveries forever; test in Tasks 1 and 4.
4. Retention/consumer retirement must preserve required active-run evidence and leave an auditable disposition; test in Tasks 4 and 5.
5. Equal timestamps, pruning and restore must not make export cursors miss records or expose another principal's run; test in Tasks 1 and 6.

## File and interface map

| File | Responsibility |
|---|---|
| `persistence/settings.py` (new) | Validate the persistence section supplied from the single central policy; no second policy file |
| `persistence/schema.py` (new) | Audit schema versions, transactional migrations and store metadata |
| `persistence/writer.py` (new) | Trusted run binding, immutable event identity and transactional event index allocation |
| `persistence/business.py` (new) | Banking transaction receipt/notice SQL and idempotent audit replication |
| `persistence/maintenance.py` (new) | Retention, capacity admission, consistent backup/restore validation |
| `persistence/reader.py` (new) | Scoped stable pagination, sanitized exports and reporting |
| `persistence/store.py` | Existing persistence transactions; call focused schema/settings helpers, keep connection serialization |
| `persistence/worker.py` | Engine state, cooperative lifecycle, consumer scheduling and ACK transactions |
| `persistence/models.py`, `privacy.py` | Structured DTOs and the existing allowlist boundary |
| `sim/tools/registry.py`, `kyc.py`; `data/generate.py` | Synthetic bank uniqueness, protected receipts/process evidence and trusted runtime context |
| `data/postconditions.py` | Reuse outcome checks; add persisted receipt/process evidence support without relying on raw public telemetry |
| `tests/test_persistence_{schema,writer,business,lifecycle,maintenance,reader,integration}.py` (new) | Each task's positive/negative boundary tests |
| `docs/persistence.md`, `README.md`, `docs/local-changes-review.md` | Update actual guarantees and executable evidence after each task |

### Task 1 — Versioned schema and centralized persistence settings

**Priority/dependencies:** P0 foundation; no new runtime prerequisite.

**Files:** Create `persistence/settings.py`, `persistence/schema.py`, `tests/test_persistence_schema.py`; modify `store.py`, `worker.py`, `docs/persistence.md`.

**Interfaces:** `PersistenceSettings.from_policy(section: dict) -> PersistenceSettings`; `migrate_audit_schema(conn: sqlite3.Connection, settings: PersistenceSettings) -> None`. Engine accepts a validated settings snapshot; it does not read a new standalone policy file. Persist the active store-wide capacity configuration and reject incompatible simultaneous writer settings. Each delivery stores its own retry/timeout snapshot so a reload cannot silently change an already queued job.

- [ ] **Step 1: Add failing migration/settings tests.** Copy the established `async_test` decorator or use `asyncio.run` directly; do not add an async test dependency. Pin a future schema and verify initialization refuses it without modifying data:

```python
def test_future_schema_is_rejected(tmp_path):
    import asyncio, sqlite3, pytest
    from persistence import EventStore
    path = tmp_path / "future.db"
    with sqlite3.connect(path) as con:
        con.execute("PRAGMA user_version=999")
    with pytest.raises(ValueError, match="schema"):
        asyncio.run(EventStore(path).initialize())
    with sqlite3.connect(path) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 999
```

Also test two processes racing initialization; rollback at each migration boundary; corrupted JSON sanitized on reads or rejected with a bounded structured error; unknown settings keys; booleans/NaN/negative limits; conflicting simultaneous settings; and monotonic offsets after pruning the latest event.

- [ ] **Step 2: Run red checks.** `uv run --locked --offline pytest tests/test_persistence_schema.py -q`. Require the unsupported-schema test to fail because current initialization accepts it, not because a fixture is broken.
- [ ] **Step 3: Implement transactional versions and config.** Set audit schema version 2 via `PRAGMA user_version`; adopt version-0 tables only after checking their column layouts. For synthetic legacy stores preserve old bytes privately and disclose that sanitizing projections do not erase them. Unknown/corrupt schemas fail closed. Use `BEGIN IMMEDIATE` and roll back failed migrations. Add the following metadata and offset contract:

```sql
CREATE TABLE store_metadata (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    epoch TEXT NOT NULL,
    next_offset INTEGER NOT NULL CHECK (next_offset >= 1),
    settings_json TEXT NOT NULL
);
-- Add events.ingest_offset INTEGER, backfill by existing rowid, then:
CREATE UNIQUE INDEX idx_events_ingest_offset ON events(ingest_offset);
```

Allocate `ingest_offset` from `next_offset` in the event transaction; never use reusable rowid as a durable cursor. Validate integer capacities, finite timeouts and bounded retries using the existing strict checks. Initial values: pending jobs 10,000; retries 3; callback timeout 5 seconds; lease 35 seconds; batch 50; poll 0.05 seconds. Reserve maintenance settings defined in Task 5. Close the new connection on initialization failure.

Later tasks add numbered migrations to this same registry, not independent schema initialization code. Persist per-job `max_retries`, `timeout_seconds` and `lease_seconds`; protect them against accidental in-flight rebinding. Include export defaults `max_export_rows=10000`, `max_export_bytes=8388608`, `export_hold_seconds=300` in the validated central-policy section.

- [ ] **Step 4: Run green and regressions.** `uv run --locked --offline pytest tests/test_persistence_schema.py tests/test_persistence.py tests/test_persistence_failures.py -q`; require every new schema/settings boundary to pass.
- [ ] **Step 5: Review checkpoint.** Verify settings come from one policy section, migrations are atomic, old data is not silently declared safe, and no change weakens immutable record semantics. Record actual schema/version behavior in `docs/persistence.md`.

### Task 2 — Trusted run writer and durable dispatch evidence

**Priority/dependencies:** P0; depends on Task 1. Requires the runtime owner to supply authenticated server-side bindings. A synthetic trusted orchestrator is sufficient for component tests, not for an authentication claim.

**Files:** Create `persistence/writer.py`, `tests/test_persistence_writer.py`; modify `store.py`, `models.py`, `privacy.py`, `docs/persistence.md`.

**Interfaces:** Define frozen `RunBinding(run_id, contract_id, session_id, principal_id, agent_id, policy_version, policy_hash, feed_version)` with string fields; `BoundAuditWriter.bind_run(binding: RunBinding) -> None`; `BoundAuditWriter.append(run_id: str, event_id: str, action_id: str, details: ActionDetails, metadata: InterceptionMetadata, status: ActionStatus, reason_code: str) -> ActionEventEnvelope`. `append` reads identity/version from the protected run table, assigns the next event index atomically, and returns the committed sanitized envelope. `append_effect(run_id: str, event_id: str, action_id: str, receipt_id: str, application_id: str, client_id: str) -> ActionEventEnvelope` imports a committed banking notice; it records an executed effect, **not** a verified outcome.

- [ ] **Step 1: Add failing identity/order/gate tests.** Cover unknown run, conflicting rebind, forged policy/principal fields, concurrent allocation, exact replay, and replay with changed structured content. Use the following dispatch fixture to pin the required ordering:

```python
async def test_audit_failure_prevents_backend_dispatch(writer, monkeypatch):
    import pytest
    reached = []
    async def unavailable(*args, **kwargs):
        raise OSError("synthetic storage outage")
    monkeypatch.setattr(writer, "append", unavailable)
    with pytest.raises(OSError):
        await writer.append("run-1", "event-1", "action-1", details, metadata,
                            ActionStatus.PENDING, "PRE_DISPATCH")
        reached.append("backend")
    assert reached == []
```

Define `details = ActionDetails(name="create_client", parameters={"application_id": "APP-0001"})` and `metadata = InterceptionMetadata(AuditorVerdict.ALLOWED)` in this module; fixtures bind synthetic `run-1`. This fixture is a dispatch-sequencing test, not proof of a production gateway.

- [ ] **Step 2: Run red checks.** `uv run --locked --offline pytest tests/test_persistence_writer.py -q`; verify tests fail for missing writer/binding behavior.
- [ ] **Step 3: Implement protected binding and append.** Store a canonical immutable binding and a next-index counter:

```sql
CREATE TABLE audit_runs (
    run_id TEXT PRIMARY KEY,
    binding_json TEXT NOT NULL,
    next_index INTEGER NOT NULL DEFAULT 0,
    lifecycle TEXT NOT NULL CHECK (lifecycle IN ('ACTIVE','SEALED','EXPIRED'))
);
UPDATE audit_runs SET next_index = next_index + 1
WHERE run_id = ? AND lifecycle = 'ACTIVE';
```

In one transaction: verify immutable binding, look up an existing event ID before allocating, compare replay content against its stored assigned index, allocate a fresh index, build context from the binding, append sanitized event plus analytics jobs, commit. Reject conflicting context supplied in metadata; callers cannot override policy identity. Require the full binding for this governed path while retaining explicit unbound telemetry for legacy component use. Opaque IDs are generated by trusted orchestration; do not use names or hash short personal identifiers as anonymization. The gateway performs final hard/state/approval checks immediately before dispatch; this writer cannot authorize actions itself.

- [ ] **Step 4: Run green checks.** `uv run --locked --offline pytest tests/test_persistence_writer.py tests/test_persistence_failures.py -q`. Confirm concurrent appends have unique contiguous committed indices; rolled-back appends do not advance the counter.
- [ ] **Step 5: Review checkpoint.** Confirm governed writes have complete context and the real adapter must await the receipt. Agent text, headers and tool results cannot call `bind_run`; enforce that deployment boundary in the separate gateway workstream.

### Task 3 — Atomic banking receipts and source-outbox replication

**Priority/dependencies:** P0; depends on Tasks 1–2. Coordinate with the KYC/backend owner; this is the shared persistence/business boundary.

**Files:** Create `persistence/business.py`, `tests/test_persistence_business.py`; modify `sim/tools/registry.py`, `sim/tools/kyc.py`, `data/generate.py`, `data/postconditions.py`, `docs/persistence.md`.

**Interfaces:** `record_effect(con: sqlite3.Connection, receipt: EffectReceipt) -> None` runs inside the existing registry transaction, never begins or commits it. Frozen `EffectReceipt` fields: receipt_id, source_event_id, application_id, run_id, action_id, command_digest, client_id, account_id, policy_version, policy_hash, occurred_at (all strings). `replicate_effects(bank_path: Path, writer: BoundAuditWriter, limit: int = 50) -> int` reads source notices, invokes `append_effect`, and acknowledges the source notice only after the audit commit. `command_digest` is computed from the protected approved command in the trusted backend; it is not public telemetry or authority supplied by the agent.

- [ ] **Step 1: Add failing transaction/recovery tests.** Submit the same approved application concurrently from two sessions; require exactly one client and one related account, an approved status and one receipt. Identical retry returns that receipt; changed command raises. Crash after bank commit before audit replication; restart the replicator; require no second client and one imported event. Include failure between client insert and receipt insert and require rollback of both. Hand-author expected identity values instead of deriving expectations only from generator rules.

```python
def assert_exactly_one_booking(con, app_id):
    assert con.execute("SELECT COUNT(*) FROM clients WHERE application_id=?", (app_id,)).fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM effect_receipts WHERE application_id=?", (app_id,)).fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM accounts a JOIN clients c USING(client_id) WHERE c.application_id=?", (app_id,)).fetchone()[0] == 1
```

- [ ] **Step 2: Run red checks.** `uv run --locked --offline pytest tests/test_persistence_business.py -q`; the existing simulator permits duplicate applications, so the independent uniqueness assertion must initially fail.
- [ ] **Step 3: Implement the source transaction and replication.** Reuse `registry.call()`'s `BEGIN IMMEDIATE`. Add:

```sql
CREATE UNIQUE INDEX uq_created_client_application
ON clients(application_id) WHERE application_id IS NOT NULL;
CREATE TABLE effect_receipts (
    receipt_id TEXT PRIMARY KEY,
    source_event_id TEXT NOT NULL UNIQUE,
    application_id TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    command_digest TEXT NOT NULL,
    client_id TEXT NOT NULL UNIQUE REFERENCES clients(client_id),
    account_id TEXT NOT NULL UNIQUE REFERENCES accounts(account_id),
    policy_version TEXT NOT NULL,
    policy_hash TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);
CREATE TABLE business_audit_outbox (
    source_event_id TEXT PRIMARY KEY REFERENCES effect_receipts(source_event_id),
    notice_json TEXT NOT NULL,
    acknowledged_at TEXT
);
```

Before creating rows, look up the application receipt and compare the protected command digest. Never invoke the tool during replication. Insert client/account/status/receipt/notice in the same banking transaction; the public notice includes opaque linkage/version references only. Read source notices by receipt time plus source ID; only mark acknowledged after `append_effect` committed. A crash after import but before acknowledgment repeats the same source event ID, which the writer handles idempotently. Do not pass the audit store as the simulator's bank database: both have different `audit_actions` schemas.

Add protected screening/decision provenance records to the banking schema: subject identity/DOB, document/registry source versions, time and approved-baseline version. These are necessary verifier inputs, not general telemetry. Extend the verifier to read them and a consistent read-only snapshot. Keep legacy raw `audit_actions` fixtures synthetic and private; the governed runtime must emit sanitized public evidence and use protected structured process records instead of exposing raw arguments/results.

- [ ] **Step 4: Run green and simulator regressions.** `uv run --locked --offline pytest tests/test_persistence_business.py sim/tools/test_tools.py data/test_postconditions.py -q`. Update simulator tests that deliberately exercise duplicate effects so duplicates are created only by an explicit corrupted-state fixture, not the normal backend path.
- [ ] **Step 5: Review checkpoint.** Exactly-once is enforced by application across sessions; identical action-ID retry alone is insufficient. Persisted receipt existence is not independent identity verification. The runtime verifier must still detect a deliberately wrong persisted identity and classify unavailable provenance as `VERIFICATION_INCOMPLETE`.

### Task 4 — Consumer lifecycle, atomic derived evidence and worker fairness

**Priority/dependencies:** P0 lifecycle correction, P1 administration; depends on Task 1.

**Files:** Modify `persistence/worker.py`, `store.py`, `models.py`; create `tests/test_persistence_lifecycle.py`; update `docs/persistence.md`.

**Interfaces:** `ConsumerResult(alerts: tuple[AlertEvent, ...] = ())`; registered callbacks may return `ConsumerResult` or legacy `None`. `EventStore.commit_consumer_result(event_id: str, consumer_name: str, lease_id: str, result: ConsumerResult) -> bool` stores derived alerts, the `(consumer_name,event_id)` completion marker and ACK in one transaction. `PersistenceEngine.pause_consumer(name: str)`, `resume_consumer(name: str)`, `retire_consumer(name: str, intervention_id: str)` are trusted administrative methods, never model tools. Retirement atomically transfers pending jobs to a reason-coded DLQ; active leases require quiescence or waiting for expiry.

- [ ] **Step 1: Add failing shutdown/fairness tests.** A callback returns a derived alert while shutdown starts; require normal completion, one alert and no deadlock. A stalled consumer must not block an unrelated healthy consumer or required audit appends. A poison optional telemetry batch must not prevent healthy durable outbox jobs from progressing. Resume a paused consumer after restart; retirement must leave a durable disposition rather than silently dropping jobs.

```python
async def derived_alert(ev):
    return ConsumerResult(alerts=(AlertEvent(
        severity=Severity.HIGH, rule="RULE-1", agent_id=ev.agent_id,
        session_id=ev.session_id, action_taken="LOGGED",
        alert_id="derived-" + ev.event_id,
    ),))
```

Also test accidental callback use of `engine.emit_alert()` while quiescing: reject immediately with a documented exception rather than waiting behind shutdown's lock. Required derived evidence uses the returned result transaction, not recursive engine emission.

- [ ] **Step 2: Run red checks.** `uv run --locked --offline pytest tests/test_persistence_lifecycle.py -q`.
- [ ] **Step 3: Implement lifecycle and scheduling.** Use this explicit state vocabulary:

```python
from enum import Enum
class EngineState(str, Enum):
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    QUIESCING = "QUIESCING"
    FAILED = "FAILED"
```

Change state under the lifecycle lock, then release that lock before awaiting drain; gate new emit calls on RUNNING before and after acquiring it. Keep connections open until in-flight thread work completes. Separate optional ingestion and durable delivery scheduling so permanent optional-record errors cannot starve analytics. Use a bounded worker pool (initially 2) with one active job per consumer for the demo; require callbacks to cooperate with cancellation. Persist consumer active/paused/retired status and per-job delivery settings. Preserve at-least-once semantics and stale-lease protection. Use a completion-marker primary key `(consumer_name,event_id)` and the existing immutable alert insertion within the ACK transaction. Arbitrary external callback I/O remains outside exactly-once claims.

- [ ] **Step 4: Run green and existing boundary tests.** `uv run --locked --offline pytest tests/test_persistence_lifecycle.py tests/test_persistence_failures.py -q`; prove no new emits enter after quiescence and shutdown timeout still leaves recoverable state.
- [ ] **Step 5: Review checkpoint.** Administrative calls require authorization at the eventual API boundary. Consumer controls only affect analytics; they never grant approvals, relax policy or replay banking effects.

### Task 5 — Retention, capacity and backup/restore operations

**Priority/dependencies:** P1, required before claiming bounded operation; depends on Tasks 1–4.

**Files:** Create `persistence/maintenance.py`, `tests/test_persistence_maintenance.py`; modify `settings.py`, `store.py`, `worker.py`, `docs/persistence.md`.

**Interfaces:** `MaintenanceReport(events_pruned: int, pending_jobs: int, logical_bytes: int, database_bytes: int, wal_bytes: int, reason_code: str)`; `maintain(store: EventStore, now: datetime) -> MaintenanceReport`; `backup_store(store: EventStore, destination: Path) -> None`; `restore_store(source: Path, destination: Path) -> None`. Paths are trusted deployment configuration and allowlisted by the operator; they never come from agent input. Backup/restore operates on synthetic audit stores, not credentials or real banking systems.

- [ ] **Step 1: Add failing retention/capacity tests.** Expired terminal runs prune; active runs, pending jobs and unreplicated receipts remain protected. A forced admission failure must prevent a critical emit. Missing terminal evidence must not be relabeled successful. Restore a consistent backup and require fresh cursor epoch, retained jobs, immutable IDs and explicit verifier-incomplete status until the independent bank snapshot is available.

```sql
SELECT e.event_id FROM events e
JOIN audit_runs r ON r.run_id = json_extract(e.payload_json, '$.context.run_id')
WHERE r.lifecycle = 'SEALED' AND e.ts < ?
AND NOT EXISTS (SELECT 1 FROM outbox o WHERE o.event_id = e.event_id);
```

- [ ] **Step 2: Run red checks.** `uv run --locked --offline pytest tests/test_persistence_maintenance.py -q`.
- [ ] **Step 3: Implement deterministic maintenance.** Validate central-policy persistence settings: terminal retention 24 hours for the synthetic demo, maintenance interval 60 seconds, maximum retained logical payload 64 MiB, minimum filesystem free space 8 MiB. These are demo defaults, not organizer rules. Pin active/sealed run transitions to trusted orchestrator/verifier signals; analytics cannot seal a run. Schedule pruning of terminal event/alert/audit/DLQ data while protecting active runs, outstanding deliveries, receipt imports and explicit retention holds. Before pruning, retain a minimal terminal run summary identifying expiry and final verification classification; reject writes/replays into expired runs. This makes retention an explicit evidence horizon, not silent proof deletion.

Track logical payload accounting atomically and enforce that limit before critical append. Report actual database/WAL sizes and free-space pressure; filesystem checks are preflight evidence, not a hard guarantee against another process filling the disk. SQLite write failures remain fail-closed. Use SQLite's backup API under serialized access, validate schema and `PRAGMA quick_check` on restore, rotate the cursor epoch, fsync the prepared destination and atomically replace only an explicitly authorized target. Do not claim deletion/pruning securely erases old WAL or backup bytes.

- [ ] **Step 4: Run green checks.** `uv run --locked --offline pytest tests/test_persistence_maintenance.py tests/test_persistence_business.py -q`; use injected time/free-space observations, not clock-dependent sleeps or a deliberately filled developer disk.
- [ ] **Step 5: Review checkpoint.** Demonstrate retention/capacity telemetry and recovery from an injected disk error. Document permissions for the store directory and backup destinations. Encryption/key management and remote disaster recovery are separate deployment projects; do not invent them for this synthetic hackathon slice.

### Task 6 — Scoped bounded read/export APIs and reporting

**Priority/dependencies:** P1, required for security-team reporting; depends on Tasks 1–2 and 5.

**Files:** Create `persistence/reader.py`, `tests/test_persistence_reader.py`; modify `store.py`, `schema.py`, `settings.py`, `privacy.py`, `docs/persistence.md`; add the persistence interface reference to `docs/dashboard-ui.md`.

**Interfaces:** Frozen `ReadScope(principal_id: str, delegated_run_ids: frozenset[str])` comes only from authenticated server code; it is never deserialized from a dashboard body. `AuditCursor(epoch: str, after_offset: int, high_watermark: int, hold_id: str)`; `AuditPage(events: tuple[ActionEventEnvelope, ...], cursor: AuditCursor, has_more: bool)`; `AuditReader.page(scope: ReadScope, run_id: str, cursor: AuditCursor | None = None, limit: int = 100) -> AuditPage`; `AuditReader.export_jsonl(scope: ReadScope, run_id: str, destination: Path) -> int`. Export has a bounded row/byte quota from the central policy, and the runtime authorizes the destination.

Add `AuditReader.grant_read(run_id: str, principal_id: str, intervention_id: str) -> None` and `AuditReader.revoke_read(run_id: str, principal_id: str, intervention_id: str) -> None` for trusted administration, and `EvidenceExpiredError(RuntimeError)` for pruned/expired evidence. Grant/revoke operations record authenticated administrator/intervention references in durable sanitized administrative evidence. A claimed delegated ID in `ReadScope` must match a persisted grant; it cannot grant itself authority. Every export holds its run against retention for at most 300 seconds; expired holds/cursors require an explicit restart or incomplete-evidence result.

- [ ] **Step 1: Add failing scope/cursor/export tests.** A principal cannot read another principal's run unless it has a protected delegated grant. Unknown filters fail rather than widening scope. Equal timestamps and concurrent inserts do not duplicate/miss page records; capture a high watermark on the first page. Changed/restored epoch rejects an old cursor. Privacy markers in legacy errors or reasons never appear in JSONL.

```python
async def test_foreign_run_is_denied(reader):
    import pytest
    with pytest.raises(PermissionError):
        await reader.page(ReadScope("principal-other", frozenset()), "run-1")
```

- [ ] **Step 2: Run red checks.** `uv run --locked --offline pytest tests/test_persistence_reader.py -q`.
- [ ] **Step 3: Implement keyset reads and sanitized exports.** Match the protected `audit_runs` binding to the server principal or a persisted explicit delegation; deny unknown/unbound runs on governed reader endpoints. Use the stable offset from Task 1:

```sql
SELECT payload_json, ingest_offset FROM events
WHERE ingest_offset > ? AND ingest_offset <= ?
  AND json_extract(payload_json, '$.context.run_id') = ?
ORDER BY ingest_offset LIMIT ?;
```

Validate limits 1–1,000, cursor epoch, offsets and immutable scope; never accept an arbitrary SQL/filter field. Snapshot the high watermark once, report gaps/expiry explicitly, and require consumers to restart a view after restore. Apply the allowlist again before export. Iterate pages instead of loading whole case/session/run histories; keep old unbounded helpers private to bounded test fixtures or deprecate them. Report policy/feed identity, interventions/approvals, verdict/reason counts, reserved/actual usage when available, final verification status, per-consumer backlog/oldest age/retries/DLQ, disk/retention health and dropped live feeds. Absent usage stays unknown, never zero-cost evidence. Count verified outcomes only from trusted verifier classifications.

Add the grants/holds in the next numbered audit migration:

```sql
CREATE TABLE audit_read_grants (
    run_id TEXT NOT NULL REFERENCES audit_runs(run_id),
    principal_id TEXT NOT NULL,
    intervention_id TEXT NOT NULL,
    PRIMARY KEY (run_id, principal_id)
);
CREATE TABLE audit_export_holds (
    hold_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES audit_runs(run_id),
    principal_id TEXT NOT NULL,
    high_watermark INTEGER NOT NULL,
    expires_at TEXT NOT NULL
);
```

Task 5's pruning query must exclude unexpired holds. A hold is created and scoped only after the reader permission check, released in a `finally` block after export, and expires after a crashed export. Test retention racing an export, expired holds, revoked grants and byte/row quota exhaustion; incomplete exports are not presented as complete audit evidence.

- [ ] **Step 4: Run green checks.** `uv run --locked --offline pytest tests/test_persistence_reader.py tests/test_persistence_maintenance.py -q`; verify exports use only scoped sanitized store data and pagination stays bounded under a large synthetic fixture.
- [ ] **Step 5: Review checkpoint.** The future gateway owns actual authentication and role enforcement. These storage checks do not protect an unsandboxed agent with direct file access. The dashboard remains a separate UI workstream consuming these read APIs.

### Task 7 — Reproducible crash matrix and integrated KYC persistence demonstration

**Priority/dependencies:** P0 acceptance; depends on Tasks 1–6. Wire to the actual runtime path once that separate gateway workstream exists; keep component fixtures explicitly labeled until then.

**Files:** Create `tests/test_persistence_integration.py`, `tests/persistence_crash_child.py`, `persistence/demo.py`; modify `README.md`, `docs/persistence.md`, `docs/local-changes-review.md`; reuse `data/postconditions.py` read-only checks.

**Interfaces:** `python -m persistence.demo --scenario <clean|audit-unavailable|crash-after-bank|wrong-state> --workspace <disposable-directory>` is a synthetic controlled-orchestrator demonstration; it never contacts real banks or an external model. Child test points: `before_bank_commit`, `after_bank_commit`, `after_audit_import`, `after_consumer_result`. A trusted fault hook exits with code 70 at exactly the chosen point. `tests/persistence_crash_child.py` accepts only a disposable directory and one enumerated point; no arbitrary code or shell command input.

- [ ] **Step 1: Add failing subprocess assertions.** Use the actual child Python executable, bounded waits and structured artifacts. Assertions read persisted state through a new read-only connection:

```python
import sqlite3, subprocess, sys
from pathlib import Path

def launch_crash(directory: Path, point: str):
    result = subprocess.run(
        [sys.executable, "tests/persistence_crash_child.py", str(directory), point],
        capture_output=True, timeout=15,
    )
    assert result.returncode == 70, result.stderr.decode()

def persisted_client_count(directory: Path):
    uri = (directory / "bank.db").resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as con:
        return con.execute("SELECT COUNT(*) FROM clients WHERE application_id='APP-0001'").fetchone()[0]
```

For each crash point, restart trusted orchestration/replication and assert the correct client count, a single receipt, no business re-execution, complete sanitized audit linkage and an idempotent consumer result. Include wrong persisted identity, duplicate corrupted fixture, missing receipt, absent bank snapshot and deliberately false agent success; check the verifier's three explicit classifications. Also test two independent processes competing for one business application and one consumer lease.

- [ ] **Step 2: Run red checks.** `uv run --locked --offline pytest tests/test_persistence_integration.py -q`; inspect that failures expose absent integration, not subprocess import problems.
- [ ] **Step 3: Implement the demo and fault points.** Use fresh per-run synthetic databases and protected baseline/provenance fixtures. The orchestrator binds a run, checks a configured deterministic decision, awaits intent commit, invokes the registry once, replicates the banking notice, fences writes and calls the read-only verifier. Use static structured reason codes; print only scoped sanitized report data. Inject failure at explicit transaction boundaries with:

```python
import os
def crash_if(selected: str, reached: str) -> None:
    if selected == reached:
        os._exit(70)
```

Keep this hook in test fixtures, not production-default execution. `wrong-state` deliberately mutates a synthetic persisted row via trusted fixture setup and displays detection, not prevention/rollback. The audit-unavailable scenario leaves the backend invocation marker absent. Record measured durable append latency, backlog/drain time and file sizes; do not impose hardware-specific sub-millisecond assertions.

- [ ] **Step 4: Run acceptance checks.** Execute:

```sh
uv lock --check --offline
uv run --locked --offline pytest
uv run --locked --offline python -m persistence.demo --scenario clean --workspace /private/tmp/kyc-persistence-clean
uv run --locked --offline python -m persistence.demo --scenario audit-unavailable --workspace /private/tmp/kyc-persistence-audit-failure
git diff --check
```

For portable execution, replace the demo workspace paths with fresh writable temporary directories on Linux/Windows; never reuse or overwrite a nonempty directory. Run CI on Python 3.11 for the existing Linux/Windows matrix. Ollama availability is relevant to the separate semantic-guard workstream; a skipped live model test cannot establish hybrid defense.

- [ ] **Step 5: Final review checkpoint.** Report commands, actual results, schema/policy versions, observed persisted state and any unverified crash boundary. Update documentation only for implemented behavior. Publish/commit the implementation only when authorized for that execution.

## Acceptance and handoff

Persistence is complete for the synthetic KYC slice only when: governed writes carry protected bindings; no critical effect dispatches without durable intent; business effects and source receipts/outbox are atomic and globally unique by application; restart converges across both stores without repeating effects; derived analytics/ACK are idempotent; active evidence and scope survive retention/export; health/capacity/shutdown are bounded and observable; and the crash matrix and independent state checks run successfully.

Recommended implementation order: Task 1 → Task 2 → Task 3 → Task 4 → Task 5 → Task 6 → Task 7. Deliver Tasks 1–4 and the early Task 7 crash checks before expanding the demo; Tasks 5–6 remain required before claiming complete persistence/reporting.

Gateway authentication/isolation, deterministic controls, approval and budget ledgers, live semantic supervision, central-policy reload and the interactive dashboard still require their own implementation. Completion of this plan supplies their persistence interfaces; it does not establish those product capabilities.

Deferred until justified by measured needs: MongoDB/Kafka/Redis, distributed multi-region storage, arbitrary plugin execution, remote backup services, encryption key infrastructure and cryptographic audit notarization. None is required to demonstrate this bounded local workflow.
