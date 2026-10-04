# Dashboard REST API and configuration management

**Status (2026-10-04): evidence-read stub implemented, store queries not.** `persistence/http_api/` serves the
evidence-read endpoints below, with this validation, error envelope and pagination, but those handlers return
fixed example data from `persistence/http_api/examples.py`. Tests: `tests/test_http_api_stub.py`.
**Configuration management is implemented, not a stub:** the shared `configuration/` router
provides authenticated updates and backend selection, backed by durable JSON state. Tests:
`tests/test_configuration_api.py`, `tests/test_configuration_intercept.py`, `tests/test_web.py`.
This document is the contract the implementation and the dashboard build against. Where the persistence layer cannot yet supply a
field, [section 8](#8-required-persistence-changes) lists the change. Until that change lands, the
field is returned as `null`.

## 1. Purpose and placement

Dashboards need to read what the three planes recorded:

- **Trajectories:** the ordered actions an agent took within a scope (session, run, case or agent).
- **Single actions:** one persisted agent action with its gateway verdict and related evidence.
- **Detection events:** every recorded sign of suspicious behaviour, as `name`, `ts` and `reason`.
- **Usage metrics:** tokens, cost, tool calls, latency and budget use per session, plus aggregates.
- **Security posture, interventions, verification results and store health.**

```text
Layer 1 interception ──► Layer 2 persistence (SQLite, WAL) ◄── Layer 3 consume plane
                                   │ read-only connection
                                   ▼
                         Read API  (this document)  ──► dashboard / exports
```

The read API is the **read side of Layer 2**. It runs as its own process, opens the evidence store
**read-only** (`file:<path>?mode=ro`, so WAL readers do not block the writer), and never imports the
write path (`PersistenceEngine`, `GovernedPersistence.append`, consumer registration). It cannot
change evidence or approvals. The separate configuration-management router changes policy for
**new sessions only**; it never writes evidence SQL or mutates existing Task Contracts.
The write API in [persistence.md](persistence.md) is
still not exposed over HTTP.

Module: `persistence/http_api/`, built on **FastAPI** with `async def` handlers, served by
**uvicorn**. This matches the asyncio-based codebase: the store methods are already coroutines, so a
handler awaits them directly. These are the only runtime dependencies the API adds (FastAPI is MIT
licensed, uvicorn is BSD). `httpx2` is a dev dependency, used by the test client.

| File | Content |
|---|---|
| `app.py` | `create_app(cors_origins, evidence_dir)`: routes, error handlers, read-only and CORS middleware, pagination |
| `examples.py` | Example payloads for every model in §5. Delete this file when the handlers call `ReadQueries`. |
| `__main__.py` | CLI: binds `127.0.0.1` by default and refuses a non-loopback bind without `--allow-remote` |
| `configuration/` (repository root) | Typed management models, shared router, and cross-process `ConfigService`; also mounted by `web/main.py` |

```sh
# started automatically next to the gateway by the live pipeline
scripts/run_live_pipeline.sh APP-0001 [--api-port 8790] [--cors-origin http://localhost:5173] [--no-api]
# on its own, for dashboard development against the stub
scripts/run_rest_api.sh --cors-origin http://localhost:5173
# directly
uv run python -m persistence.http_api --evidence-dir <bank-runs dir> --config-dir <operator-config-dir> --port 8790
```

**One evidence store per session.** The governed runtime writes
`<runs-dir>/<session_id>.evidence.db` when a session binds, so the API takes the directory
(`--evidence-dir`), not one database file. Each read opens the matching store read-only.
Cross-session endpoints (`/sessions`, `/detections`, `/metrics/*`) merge over every
`*.evidence.db` in the directory. The live pipeline passes its `bank-runs/` directory. See
[scripts/README.md](../scripts/README.md) for the process layout and the env files.

The OpenAPI schema is served at `/api/v1/openapi.json`, with an interactive UI at `/api/v1/docs`.
`--no-docs` turns them off.

The gateway endpoints in [application-documentation.md §5.4](application-documentation.md#54-gateway-dashboard-api-contract-for-judge-ui)
(`/api/v1/inspect`, `/api/v1/events/stream`, `/api/v1/policy`, ...) belong to Layer 1 and are not
part of this API. Both use the `/api/v1` prefix and do not share a path. The dashboard reads live
decisions from the gateway's SSE stream and history from this API.

## 2. Conventions

| Topic | Rule |
|---|---|
| Base path | `/api/v1`. Evidence routes remain `GET` only (plus `HEAD`). Two management exceptions permit `PUT /configs/{name}` and `PUT /config-selection`. Other writes return `405`. Allowed-origin CORS preflight (`OPTIONS`) carries no data. |
| Format | `application/json; charset=utf-8`. Exports use `application/x-ndjson`. |
| Auth | Evidence/config reads remain unauthenticated. Management PUTs require `Authorization: Bearer <CONFIG_ADMIN_TOKEN>`; absent server credential disables writes (`401`). Default bind is loopback; `--allow-remote` is not authorization. See §7. |
| Binding | Default `127.0.0.1`. Binding elsewhere requires `--allow-remote`. This port is never reachable from the agent's tool path. |
| CORS | Allowed only for origins listed in `--cors-origin`. No wildcard. |
| Timestamps | ISO 8601 UTC with `Z`, for example `2026-10-03T15:42:10.500Z`. Query parameters accept any ISO 8601 with a timezone. |
| Time windows | `since` is inclusive and `until` is exclusive. If neither is given, the window is unbounded. |
| IDs | Opaque strings, as stored. Clients must not parse them. |
| Enum casing | The wire vocabulary of [Event Envelope v2.1](consumer-plane-event-envelope.md): `kind`, `status` and `severity` are lowercase, and gateway decisions are uppercase (`ALLOW`, `BLOCK`, `REDACT`, `REQUIRE_APPROVAL`, `ALERT`). Uppercase storage enums are mapped through `persistence/vocabulary.py` and never leak. |
| Unknown fields | Clients must ignore fields they do not know. New fields are additive within `v1`. |
| Consistency | Each request reads inside one SQLite read transaction, so one response is a consistent snapshot. |
| Privacy | Only data that already passed `persistence/privacy.py` is returned. Bodies (prompts, completions, tool results) are never returned, only `ContentRef` metadata. Free text such as finding summaries, auditor reasons and errors is never stored, so it cannot be returned. Human-readable reasons come from the static [detection catalog](#57-detection-catalog). The contract `objective` is not returned. |

### 2.1 Pagination

List endpoints use keyset pagination:

| Parameter | Default | Rule |
|---|---|---|
| `limit` | `100` | `1..1000` |
| `cursor` | – | The opaque `next_cursor` from the previous page. Do not combine it with changed filters. |

```json
{ "items": [ ... ], "next_cursor": "eyJ0cyI6Ii4uLiJ9", "has_more": true }
```

A cursor encodes the sort key of the last item, so inserts during paging do not cause duplicates
or gaps. After retention pruning, an outdated cursor returns `400 invalid_cursor`.

### 2.2 Errors

Every error uses the same body:

```json
{ "error": { "code": "not_found", "message": "session not found", "details": {"session_id": "sess_x"} } }
```

| HTTP | `code` | When |
|---|---|---|
| 400 | `bad_request`, `invalid_cursor`, `invalid_filter` | Malformed parameter, unknown enum value, `limit` out of range |
| 404 | `not_found` | Unknown session, run, action or detection |
| 405 | `method_not_allowed` | Any method other than `GET` |
| 410 | `evidence_expired` | The run was sealed and then pruned (`audit_runs.lifecycle = EXPIRED`) |
| 413 | `export_quota_exceeded` | The export exceeds `max_export_rows` or `max_export_bytes` from `PersistenceSettings` |
| 503 | `store_unavailable` | The database is missing, locked past the busy timeout, or has the wrong schema version |

Configuration-specific errors (same envelope; fixed messages, no submitted values or exception text):

| HTTP | `code` | Fixed `message` | When |
|---|---|---|---|
| 400 | `bad_request` | `malformed configuration request` | Malformed JSON, duplicate keys, non-finite JSON constants, invalid name |
| 401 | `unauthorized` | `management authentication required` | Missing/invalid bearer credential or management disabled; includes `WWW-Authenticate: Bearer` |
| 404 | `config_not_found` | `configuration not found` | Unknown config name; PUT never creates configs |
| 405 | `method_not_allowed` | `method not allowed` | Unsupported management method |
| 409 | `config_revision_conflict` | `configuration revision changed` | Requested selection revision differs from saved revision |
| 413 | `config_too_large` | `configuration request exceeds size limit` | Request body exceeds 64,000 bytes |
| 415 | `unsupported_media_type` | `application/json required` | Missing/non-JSON Content-Type |
| 422 | `invalid_config` | `invalid configuration` | Invalid model, unknown fields, invalid constraints, path/body name mismatch |
| 503 | `config_unavailable` | `configuration storage unavailable` | Missing/corrupt defaults, corrupt persisted state, lock timeout (5 s), or storage failure |

Management errors have `details: {}` or `details: {"fields": ["budget.tokens"]}`.
Only bounded, sanitized model paths are returned. Authentication precedes body processing.
The current frontend's browser-local “Make active” action does not call the selection endpoint;
frontend integration must use these authenticated PUTs (preferably through its trusted proxy).

`message` is a fixed string per code. Exception text is never returned.

## 3. Endpoint summary

| # | Method and path | Returns |
|---|---|---|
| 1 | `GET /api/v1/health` | `Health` |
| 2 | `GET /api/v1/sessions` | `Page<SessionSummary>` |
| 3 | `GET /api/v1/sessions/{session_id}` | `SessionDetail` |
| 4 | `GET /api/v1/trajectories/{scope}/{id}` | `Trajectory`, where `scope` is `session`, `run`, `case` or `agent` |
| 5 | `GET /api/v1/actions` | `Page<ActionSummary>` |
| 6 | `GET /api/v1/actions/{event_id}` | `Action` |
| 7 | `GET /api/v1/detections` | `Page<DetectionEvent>` |
| 8 | `GET /api/v1/detections/{detection_id}` | `DetectionEvent` |
| 9 | `GET /api/v1/catalog/detections` | `DetectionCatalogEntry[]` |
| 10 | `GET /api/v1/sessions/{session_id}/usage` | `SessionUsage` |
| 11 | `GET /api/v1/metrics/usage` | `UsageReport` |
| 12 | `GET /api/v1/metrics/security` | `SecurityOverview` |
| 13 | `GET /api/v1/metrics/timeseries` | `TimeSeries` |
| 14 | `GET /api/v1/interventions` | `Page<Intervention>` |
| 15 | `GET /api/v1/sessions/{session_id}/verification` | `Verification` |
| 16 | `GET /api/v1/system/stats` | `StoreStats` |
| 17 | `GET /api/v1/export/sessions/{session_id}` | NDJSON audit export |
| 18 | `GET /api/v1/configs` | `ConfigSummary[]` (real backend state) |
| 19 | `GET /api/v1/configs/{name}` | `PolicyConfig`; quoted revision in `ETag` |
| 20 | `PUT /api/v1/configs/{name}` | `ConfigUpdateResult` |
| 21 | `PUT /api/v1/config-selection` | `ConfigSelectionResult` |
| 22 | `GET /api/v1/metrics/performance` | `PerformanceOverview` |

Priority for the hackathon build: **P0** is 1, 2, 4, 6, 7, 10 and 12. **P1** is the rest. Endpoint 17
covers the "exportable audit logs" deliverable, so build it as soon as P0 works.

## 4. Endpoints

### 4.1 `GET /health`

Liveness only. It returns no counts and no IDs.

```json
{ "status": "ok", "schema_version": 3, "read_only": true, "now": "2026-10-04T09:12:00.000Z" }
```

`status` is `ok`, or `degraded` when the store cannot be opened. In that case the response is `503`.
`read_only` describes the **evidence** interface; it does not disable the authenticated config router.

### 4.2 `GET /sessions`

Lists sessions, newest `started_at` first.

| Query | Type | Meaning |
|---|---|---|
| `agent_id`, `case_id`, `run_id` | string | Exact match |
| `state` | `active`, `ended`, `halted` | See `SessionState` |
| `verification_status` | `VERIFIED_SUCCESS`, `FAILED_POSTCONDITIONS`, `VERIFICATION_INCOMPLETE`, `none` | `none` means no result is stored |
| `min_severity` | `info`, `low`, `medium`, `high`, `critical` | Only sessions whose `max_severity` is at least this |
| `since`, `until` | timestamp | Filter on `started_at` |
| `limit`, `cursor` | | §2.1 |

Response: `Page<SessionSummary>`.

### 4.3 `GET /sessions/{session_id}`

Returns `SessionDetail`: the summary plus the contract view, the usage block, verification, detection
counts and active interventions. Returns `404` if no event, contract or alert has this `session_id`.

### 4.4 `GET /trajectories/{scope}/{id}`

The ordered trace of what an agent did within one scope. This is the main view for
"agent said done, was it?".

| `scope` | `id` | Ordering (`Trajectory.ordering`) | Source |
|---|---|---|---|
| `session` | `session_id` | `seq`: the Layer 2 per-session order | `events WHERE session_id ORDER BY seq` |
| `run` | `run_id` | `run_index`: the gateway-assigned `action_index`, the trusted run order | `run_order JOIN events ORDER BY action_index` |
| `case` | `case_id` | `ts` across sessions, grouped into one segment per session | `events WHERE case_id ORDER BY ts, rowid` |
| `agent` | `agent_id` | `ts` across sessions, grouped into one segment per session | `events WHERE agent_id ORDER BY ts, rowid` |

The `case` and `agent` scopes are **chronological views**. Within each segment, steps are still
sorted by `seq`. These views do not replace the trusted run order. See [persistence.md](persistence.md).

| Query | Type | Default | Meaning |
|---|---|---|---|
| `kinds` | CSV of `prompt`, `tool_use`, `egress`, `session`, `approval`, `control` | all | Filter steps by kind |
| `statuses` | CSV of `completed`, `blocked`, `redacted`, `pending_approval`, `failed` | all | Filter steps by status |
| `from_seq`, `to_seq` | int | – | Inclusive `seq` bounds. `session` scope only. |
| `since`, `until` | timestamp | – | `case` and `agent` scopes |
| `view` | `summary`, `full` | `summary` | `summary` returns `ActionSummary` steps. `full` returns `Action` steps. |
| `include_detections` | bool | `true` | Attach `DetectionRef`s to the steps they reference |
| `limit`, `cursor` | | 1000 | Pages over steps. Segment headers repeat on each page. |

Durable `PENDING` intent records are **excluded**. They have no consumer `seq` and never count as
executed steps. To see an intent, open the action with `GET /actions/{event_id}`, which lists it
under `related`.

Response: `Trajectory`.

### 4.5 `GET /actions`

A flat, filterable action list for tables and drill-down.

| Query | Type | Meaning |
|---|---|---|
| `session_id`, `run_id`, `case_id`, `agent_id`, `action_id` | string | Exact match. `action_id` returns intent, result and receipt evidence together. |
| `kinds`, `statuses` | CSV | As in §4.4 |
| `decisions` | CSV of `ALLOW`, `BLOCK`, `REDACT`, `REQUIRE_APPROVAL`, `ALERT` | Final gateway decision |
| `name` | string | Tool name, model ID or egress host |
| `side_effects` | CSV of `read`, `write`, `irreversible` | Tool steps only |
| `include_intents` | bool, default `false` | Include `PENDING` intent records |
| `since`, `until`, `limit`, `cursor` | | Sorted by `ts` ascending, then insertion order |

Response: `Page<ActionSummary>`.

### 4.6 `GET /actions/{event_id}`

One action with everything the dashboard needs to explain it. Returns `Action` (§5.4), or `404`.

### 4.7 `GET /detections`

All detection events, newest first. A detection event is "something suspicious was recorded",
whichever component recorded it. §6.1 maps each source to the model.

| Query | Type | Meaning |
|---|---|---|
| `session_id`, `run_id`, `case_id`, `agent_id` | string | Scope filter |
| `sources` | CSV of `gateway`, `finding`, `alert`, `verification` | Default: all |
| `names` | CSV | Exact `name`, for example `risk.trajectory_high` or `signature-scanner` |
| `min_severity` | severity | Inclusive lower bound |
| `trigger_event_id` | string | Detections caused by one action |
| `since`, `until` | timestamp | Filter on detection `ts` |
| `order` | `desc`, `asc` | Default `desc`. Use `asc` with `since` for polling. |
| `limit`, `cursor` | | §2.1 |

**Polling for live panels:** remember the largest `ts` you have seen, call
`?since=<that ts>&order=asc` every 1–2 s, and deduplicate by `detection_id`, because `since` is
inclusive.

Response: `Page<DetectionEvent>`.

### 4.8 `GET /detections/{detection_id}`

Returns one `DetectionEvent` (§5.6), or `404`.

### 4.9 `GET /catalog/detections`

The static, versioned catalog that turns `name` and `reason` codes into readable text and OWASP
mappings. It is loaded from `config/detection-catalog.yaml` (planned) and hot-reloaded on change
the same way as the policy file. Response: `{ "catalog_version": "c3", "entries": [DetectionCatalogEntry] }`.

### 4.10 `GET /sessions/{session_id}/usage`

Usage and budget state for one session. Returns `SessionUsage` (§5.8).

### 4.11 `GET /metrics/usage`

Aggregated usage for cost and budget panels.

| Query | Type | Default | Meaning |
|---|---|---|---|
| `group_by` | `agent`, `session`, `case`, `model`, `tool`, `day` | `agent` | One `UsageBucket` per group |
| `agent_id`, `case_id` | string | – | Filter |
| `since`, `until` | timestamp | last 24 h | Window |
| `limit` | int | 100 | Maximum number of buckets, ordered by `cost_usd`, then `total_tokens` |

Response: `UsageReport`. With `group_by=day`, each bucket also carries the daily budget once one
is configured. See §8, item 7.

### 4.12 `GET /metrics/security`

The posture headline: what was inspected, what was stopped, and what was found.

| Query | Default | Meaning |
|---|---|---|
| `since`, `until` | last 24 h | Window |
| `agent_id`, `session_id` | – | Filter |
| `top` | 10 | Length of the `top_detections` and `top_blocked_tools` lists |

Response: `SecurityOverview` (§5.10).

### 4.13 `GET /metrics/timeseries`

| Query | Values | Default |
|---|---|---|
| `metric` (required) | `actions`, `blocked`, `redacted`, `detections`, `input_tokens`, `output_tokens`, `cost_usd`, `interception_overhead_ms_p95` | – |
| `bucket` | `1m`, `5m`, `1h`, `1d` | `5m` |
| `since`, `until` | timestamp | last 1 h |
| `session_id`, `agent_id` | string | – |

At most 1,000 points are returned. A wider window returns `400 bad_request`. Buckets with no data
are returned with `value: 0`, or with `null` for the percentile metric, so the chart has no gaps.

Response: `TimeSeries`.

### 4.14 `GET /interventions`

The feedback loop's audit trail: policy adjustment signals proposed by the consume plane and
whether Layer 1 applied them.

| Query | Meaning |
|---|---|
| `session_id`, `source_plugin` | Filter |
| `actions` | CSV of `ALERT`, `REQUIRE_APPROVAL_FOR`, `BLOCK_TOOLS`, `STRICT_MODE`, `HALT_SESSION` |
| `applied` | `true` or `false` |
| `active` | `true` returns only applied signals whose TTL has not expired |
| `since`, `until`, `limit`, `cursor` | |

Response: `Page<Intervention>`.

### 4.15 `GET /sessions/{session_id}/verification`

The independent outcome verification for a finished session. Returns `Verification` (§5.12), or
`404` when no result is stored yet. "Not verified yet" and "unknown session" are different cases:
for a known session without a result, the body is `{"verification_status": null, "checks": []}`
with status `200`.

### 4.16 `GET /system/stats`

Store health for the operator panel: `EventStore.get_stats()` plus delivery backlog. Returns
`StoreStats` (§5.13).

### 4.17 `GET /export/sessions/{session_id}`

The exportable audit log for one session, as NDJSON. Every line is one object with a `record_type`.
Lines are written in this order:

1. `session`: the `SessionDetail`
2. `action`: each `Action.event` in `seq` order, intents included (`include_intents=true`)
3. `detection`: each `DetectionEvent`
4. `intervention`: each `Intervention`
5. `verification`: the `Verification`, if one exists
6. `export_footer`: `{"record_type": "export_footer", "rows": N, "sha256": "<hex of all prior lines>", "exported_at": "..."}`

`Content-Disposition: attachment; filename="audit-<session_id>.ndjson"`. The export streams pages
and holds one read snapshot. It enforces `PersistenceSettings.max_export_rows` and
`max_export_bytes` and fails with `413` before it sends anything. Run-scoped exports with
principal checks and retention holds remain on `AuditReader.export_jsonl`.

### 4.18 Configuration reads and `PUT /configs/{name}`

Names are case-sensitive: `lenient`, `standard`, `strict`. Other syntactically valid names
return `404`; invalid names return `400`. The three built-in defaults are all editable through
the backend; untracked draft files in `config/presets/` are not exposed.

`GET /configs` returns `ConfigSummary[]`; `GET /configs/{name}` returns the complete saved
`PolicyConfig`. Its `ETag` is the quoted `sha256:<64 lowercase hex digits>` revision. Reads
return `404`/`503` for missing configs/unavailable storage.

`PUT /configs/{name}` takes a complete `PolicyConfig` (§5.15), **not a partial patch**.
Its body `name` must equal the path name. The endpoint only replaces existing configs and
requires management authentication. Successful response: **200**, JSON `ConfigUpdateResult`:

```json
{
  "name": "standard",
  "revision": "sha256:<64 lowercase hex digits>",
  "updated_at": "2026-10-04T15:00:00.000Z",
  "selected": true,
  "active_revision": "sha256:<previously-selected-hash>",
  "requires_selection": true
}
```

Saving does **not** activate the new revision. Repeating identical normalized content preserves
its revision and `updated_at`. Errors: **400, 401, 404, 405, 413, 415, 422, 503** (§2.2).

### 4.19 `PUT /config-selection`

Authenticated RPC-style replacement of the backend selection. Request: `ConfigSelectionRequest`:

```json
{ "name": "standard", "revision": "sha256:<64 lowercase hex digits>" }
```

`revision` is mandatory: obtain it from the config list, the quoted GET ETag, or an update result.
The backend checks it against the saved config **inside the same locked transaction** as selection.
Successful response: **200**, JSON `ConfigSelectionResult`:

```json
{
  "name": "standard",
  "revision": "sha256:<64 lowercase hex digits>",
  "selected_at": "2026-10-04T15:01:00.000Z",
  "effective_for": "new_sessions"
}
```

Selecting the same name/revision is idempotent and preserves `selected_at`. Existing sessions
keep their pinned config, spent budgets, approval state and Task Contract. The next trusted
session binding loads the selected snapshot; no server restart is required. Errors:
**400, 401, 404, 405, 409, 413, 415, 422, 503** (§2.2). No empty success responses or `204`.

### 4.20 `GET /metrics/performance`

Performance telemetry of the control layer: what the gateway adds to each action, split by the
hybrid defence's two paths.

| Query | Default | Meaning |
|---|---|---|
| `since`, `until` | last 24 h | Window |
| `agent_id`, `session_id` | – | Filter |

```json
{
  "since": "...", "until": "...",
  "actions_evaluated": 404,
  "interception_overhead_ms": {"p50": 1.1, "p95": 228.0, "p99": 252.0},
  "by_method": {
    "deterministic": {"runs": 404, "skipped": 0, "p50": 0.9, "p95": 2.4, "p99": 6.1},
    "semantic": {"runs": 148, "skipped": 24, "p50": 152.0, "p95": 234.0, "p99": 255.0}
  },
  "backend_latency_ms": {"p50": 41.0, "p95": 640.0, "p99": 910.0},
  "overhead_share": 0.22
}
```

- `actions_evaluated` counts gateway-evaluated actions, as in `SecurityOverview` rates.
- `interception_overhead_ms` is the whole gateway time per action, semantic check included.
  `SecurityOverview.interception_overhead_ms` stays as it is.
- `by_method` sums `auditor_decisions[].latency_ms` per action by the auditor's `Method`.
  `runs` is the number of actions the path ran on. `semantic.skipped` counts actions where the
  semantic check would have run but a deterministic control had already denied.
- `backend_latency_ms` is `Usage.latency_ms`: the model or tool call itself.
- `overhead_share` is total interception overhead divided by total overhead plus backend latency,
  `0..1`, or `null` when the window has no executed action.

## 5. Models

Notation: `T?` means the value can be `null`. Every field listed is always present, with `null`
when the value is unknown, so clients do not need to check for missing keys.

### 5.1 Enums

```text
Kind            = "prompt" | "tool_use" | "egress" | "session" | "approval" | "control"
Status          = "completed" | "blocked" | "redacted" | "pending_approval" | "failed" | "pending"
                  ("pending" appears only for intent records, with include_intents=true)
Decision        = "ALLOW" | "BLOCK" | "REDACT" | "REQUIRE_APPROVAL" | "ALERT"
Severity        = "info" | "low" | "medium" | "high" | "critical"     (ordered)
DetectionSource = "gateway" | "finding" | "alert" | "verification"
Method          = "deterministic" | "semantic"
SessionState    = "active" | "ended" | "halted"
VerificationStatus = "VERIFIED_SUCCESS" | "FAILED_POSTCONDITIONS" | "VERIFICATION_INCOMPLETE"
RiskLevel       = "low" | "medium" | "high" | "critical"
UsageSource     = "reported" | "estimated" | "mixed"
```

`SessionState`: `ended` if a `session` event with `phase = ended` exists. `halted` if an applied
`HALT_SESSION` intervention exists and no `ended` event exists. Otherwise `active`.

### 5.2 `Usage`

```json
{ "input_tokens": 812, "output_tokens": 240, "total_tokens": 1052,
  "cost_usd": 0.0041, "latency_ms": 142.5, "source": "estimated" }
```

The values come from `context.actual_usage` (`input_tokens`, `output_tokens`, `latency_ms`,
`actual_cost`). `source` is `estimated` when Layer 1 recorded its conservative pre-dispatch
estimate, which is the current behaviour of `intercept/governed/prompts.py`, and `reported` when
the provider returned trusted usage. The dashboard must label estimated numbers as estimates.

### 5.3 `ActionSummary`

One row of a trajectory or an action list.

```json
{
  "event_id": "evt_01J9ZK3Q8W2M5N7R4T6V8X0Y1A",
  "action_id": "act_7",
  "seq": 7,
  "run_index": 12,
  "ts": "2026-10-03T15:42:10.500Z",
  "session_id": "sess_onb_APP0007",
  "run_id": "run_0042",
  "agent_id": "onboarding-agent",
  "case_id": "APP-0007",
  "kind": "tool_use",
  "name": "create_client",
  "side_effect": "write",
  "status": "blocked",
  "executed": false,
  "decision": "BLOCK",
  "triggered_rules": [{"auditor": "signature-scanner", "decision": "BLOCK", "rule_id": "SIG-003"}],
  "reason_code": "SIGNATURE_MATCH",
  "policy_version": "v12",
  "usage": { "...": "Usage" },
  "interception_overhead_ms": 1.2,
  "detections": [{"detection_id": "gw_evt_01J9ZK...", "name": "signature-scanner", "severity": "high"}],
  "max_severity": "high",
  "fault_injected": false
}
```

| Field | Source |
|---|---|
| `seq` | Layer 2 session sequence. `null` for intents. |
| `run_index` | `context.action_index`. `null` when the event has no run binding. |
| `name` | Depends on `kind`. `tool_use`: the tool name. `prompt`: the model ID. `egress`: `host`. `session`: `phase`. `approval`: `decision`. `control`: `change`. |
| `side_effect` | `tool_use` only, else `null` |
| `executed` | `status` is `completed` or `redacted`, the same as `AgentAction.executed` |
| `decision` | `interception_metadata.final_decision`. `null` for `session` and `approval` events that did not pass the gateway. |
| `triggered_rules` | Auditor decisions other than `ALLOW`. Auditor free-text reasons are never included. |
| `reason_code` | `context.reason_code`: a fixed code of at most 64 characters, resolved through the catalog |
| `detections` | `DetectionRef[]`: detections whose `trigger_event_id` or `evidence_event_ids` include this event. Empty when `include_detections=false`. |
| `max_severity` | The highest severity in `detections`, or `null` |

### 5.4 `Action`

The full view of one action. `event` is the **unchanged Event Envelope v2.1**, produced by
`persistence.adapters.to_consumer_v21`, so the dashboard and the consume plane see one contract.
The other fields are read-time annotations.

```json
{
  "summary": { "...": "ActionSummary" },
  "event": { "schema_version": "2.1", "event_id": "...", "seq": 7, "action_type": "tool_call",
             "action_details": {"name": "create_client", "side_effect": "write", "transport": "inproc",
                                "parameters": {"application_id": "APP-0007"}, "result": null, "error": null},
             "interception_metadata": {"final_decision": "BLOCK", "policy_version": "v12",
                                       "auditor_decisions": [ ... ], "interception_overhead_ms": 1.2},
             "metrics": { ... }, "fault_injected": false },
  "context": {
    "contract_id": "contract_APP0007_v1", "principal_id": "op_1", "policy_hash": "<sha256>",
    "feed_version": "local-signatures-v1", "approval_id": null, "intervention_id": null,
    "effect_receipt_id": null, "semantic_model": null, "semantic_model_version": null,
    "reserved_usage": {"reserved_tokens": 2048}
  },
  "related": [
    {"event_id": "evt_intent_7", "status": "pending", "ts": "2026-10-03T15:42:10.410Z", "relation": "intent"}
  ],
  "previous_event_id": "evt_...6",
  "next_event_id": "evt_...8",
  "detections": [ { "...": "DetectionEvent" } ]
}
```

- `context` is the sanitized `AuditContext` without fields already in `event`. `reason_code` and
  `actual_usage` are already in `summary`.
- `related` lists the other evidence records with the same `action_id`. `relation` is `intent`,
  `result`, `receipt` or `control`.
- `previous_event_id` and `next_event_id` are the neighbours by `seq` in the same session. They
  drive the trajectory step-through controls.
- `action_details.parameters` holds only the structured evidence that passed the privacy
  allowlist. The fields listed in `sanitized_fields` were removed before storage.
- Errors decode as `"[OMITTED]"`, never as raw text.

### 5.5 `Trajectory`

```json
{
  "scope": "session",
  "id": "sess_onb_APP0007",
  "ordering": "seq",
  "segments": [
    {
      "session_id": "sess_onb_APP0007",
      "run_id": "run_0042",
      "agent_id": "onboarding-agent",
      "case_id": "APP-0007",
      "contract_id": "contract_APP0007_v1",
      "state": "ended",
      "started_at": "2026-10-03T15:40:00.000Z",
      "ended_at": "2026-10-03T15:43:02.000Z",
      "end_reason": "agent_finished",
      "steps": [ "ActionSummary or Action ..." ],
      "gaps": [{"after_seq": 4, "before_seq": 6}]
    }
  ],
  "totals": {
    "steps": 9, "executed": 6, "blocked": 2, "redacted": 0, "pending_approval": 0, "failed": 1,
    "detections": 4, "max_severity": "critical",
    "usage": { "...": "Usage" }
  },
  "risk": { "level": "critical", "expected_loss": 22.77, "failure_probability": 0.95,
            "finding_id": "fnd_3b1c...", "source": "trajectory-risk" },
  "verification_status": "FAILED_POSTCONDITIONS",
  "next_cursor": null,
  "has_more": false
}
```

- `segments` has exactly one entry for the `session` and `run` scopes, and one entry per session
  for `case` and `agent`.
- `gaps` lists missing `seq` numbers, which can come from pruning or a filter. When `kinds` or
  `statuses` filters are active, `gaps` is empty. A client can use it to show that evidence is
  incomplete.
- `risk` comes from the most severe `risk.trajectory_*` finding of the trajectory-risk plugin.
  `level` is always available. `expected_loss` and `failure_probability` stay `null` until the
  finding projection persists them (§8, item 2). If no risk finding exists, `risk` is `null`. The
  dashboard must label the score as heuristic. See [trajectory-risk-model.md](trajectory-risk-model.md).
- `verification_status` is set for the `session` and `run` scopes only, and is `null` otherwise.

### 5.6 `DetectionEvent`

One saved sign of suspicious behaviour. The core triple is **`name`, `ts`, `reason`**. The other
fields scope it, rank it, and link it to evidence.

```json
{
  "detection_id": "fnd_3b1c9a0e4f2d8c7b6a5e4d3c",
  "name": "risk.trajectory_critical",
  "ts": "2026-10-03T15:42:11.020Z",
  "reason": "OUT_OF_CONTRACT_TOOL",
  "reason_text": "The agent called a tool that is not in its Task Contract.",
  "severity": "critical",
  "source": "finding",
  "detector": "trajectory-risk",
  "detector_version": "1.0",
  "method": "deterministic",
  "confidence": null,
  "action_taken": "HALT_SESSION",
  "session_id": "sess_onb_APP0003",
  "run_id": "run_0040",
  "agent_id": "onboarding-agent",
  "case_id": "APP-0003",
  "trigger_event_id": "evt_...9",
  "evidence_event_ids": ["evt_...6", "evt_...7", "evt_...9"],
  "policy_version": "v12",
  "owasp": ["LLM06:2025 Excessive Agency"],
  "details": {"signals": {"out_of_contract_tool": 1, "out_of_scope_target": 2}}
}
```

| Field | Type | Rule |
|---|---|---|
| `detection_id` | string | Stable and unique across sources. Gateway detections use `gw_<event_id>`, and verification detections use `ver_<session_id>`. |
| `name` | string | What was detected, as a machine name. It is a catalog key. |
| `ts` | timestamp | When it was detected. For gateway detections this is the action `ts`. |
| `reason` | string? | A fixed reason code, at most 64 characters, never free text. `null` when the source has none. |
| `reason_text` | string? | The catalog text for `reason`, or for `name` when there is no reason. `null` if the catalog lacks the key. It is never read from storage. |
| `severity` | Severity | §6.2 |
| `source` | DetectionSource | §6.1 |
| `detector` | string | The auditor, plugin or verifier that produced the detection |
| `detector_version` | string? | Plugin version for findings |
| `method` | Method | `semantic` detections always carry `confidence` |
| `confidence` | number? | `0..1`, or `null` for deterministic detections |
| `action_taken` | string | What happened as a result: a gateway `Decision`, an adjustment action, or `NONE` |
| `session_id` | string | Always present |
| `run_id`, `agent_id`, `case_id` | string? | Scope fields |
| `trigger_event_id` | string? | The action that caused the detection |
| `evidence_event_ids` | string[] | Supporting events. IDs only, never content. |
| `policy_version` | string? | The policy that was active for the trigger |
| `owasp` | string[] | From the catalog. Empty when the catalog has no mapping. |
| `details` | object | Structured numbers and codes only, as allowed by the store projection. Treat it as opaque. |

`DetectionRef` is the short form embedded in actions:
`{ "detection_id", "name", "severity" }`.

### 5.7 `DetectionCatalogEntry`

```json
{
  "name": "risk.trajectory_critical",
  "title": "Trajectory risk critical",
  "description": "Cumulative expected loss of the session crossed the critical threshold.",
  "default_severity": "critical",
  "reasons": { "OUT_OF_CONTRACT_TOOL": "The agent called a tool that is not in its Task Contract." },
  "owasp": ["LLM06:2025 Excessive Agency"],
  "control_family": "trajectory"
}
```

`control_family` is one of `pii`, `secrets`, `injection`, `budget`, `exploit_signature`,
`trajectory` or `outcome`. These are the groups of the dashboard test-suite strip
([dashboard-ui.md](dashboard/dashboard-ui.md)).

### 5.8 `SessionUsage`

```json
{
  "session_id": "sess_onb_APP0007",
  "window": {"first_ts": "2026-10-03T15:40:00.000Z", "last_ts": "2026-10-03T15:43:02.000Z", "duration_s": 182.0},
  "usage": { "...": "Usage, summed over executed and failed model calls" },
  "model_calls": {"total": 6, "completed": 5, "blocked": 1, "failed": 0, "by_model": {"claude-haiku-4-5-20251001": 6}},
  "tool_calls": {"total": 9, "executed": 6, "blocked": 2, "pending_approval": 0, "failed": 1,
                 "by_tool": [{"tool": "create_client", "total": 2, "blocked": 1, "side_effect": "write"}]},
  "egress_calls": {"total": 0, "blocked": 0, "by_host": {}},
  "latency_ms": {"backend_sum": 1840.2, "action_p50": 120.0, "action_p95": 410.0,
                 "interception_overhead_p50": 0.9, "interception_overhead_p95": 2.4},
  "budgets": [
    {"resource": "tokens", "limit": 20000, "used": 9120, "reserved": 2048, "remaining": 8832,
     "utilisation": 0.558, "exceeded": false, "scope": "session"},
    {"resource": "tool_calls", "limit": 30, "used": 9, "reserved": 0, "remaining": 21,
     "utilisation": 0.3, "exceeded": false, "scope": "session"},
    {"resource": "cost_usd", "limit": null, "used": 0.0041, "reserved": 0, "remaining": null,
     "utilisation": null, "exceeded": false, "scope": "session"}
  ],
  "budget_blocks": 0
}
```

- Model calls that failed or were cancelled still count toward `usage`, because Layer 1 keeps the
  charge ([integrated-runtime.md](integrated-runtime.md)).
- Budget `limit` comes from the stored Task Contract's `Budget`. A `null` limit means the resource
  is unlimited, and then `remaining` and `utilisation` are also `null`.
- `reserved` is the sum of `reserved_usage` on intents that have no result yet.
- `remaining` is `limit − used − reserved`, never below 0.
- `budget_blocks` counts actions that were blocked with a budget reason code.

### 5.9 `UsageReport` and `UsageBucket`

```json
{
  "group_by": "model",
  "since": "2026-10-03T09:00:00.000Z",
  "until": "2026-10-04T09:00:00.000Z",
  "totals": { "...": "Usage" },
  "buckets": [
    {"key": "claude-haiku-4-5-20251001", "sessions": 14, "model_calls": 88, "tool_calls": 131,
     "blocked": 9, "usage": { "...": "Usage" }, "budget": null}
  ]
}
```

`budget` uses the same shape as the entries of `SessionUsage.budgets`, with `scope: "day"`, and is
filled only for `group_by=day` once a daily budget exists in the policy.

### 5.10 `SecurityOverview`

```json
{
  "since": "...", "until": "...",
  "actions": {"total": 412, "by_decision": {"ALLOW": 360, "BLOCK": 31, "REDACT": 14, "REQUIRE_APPROVAL": 3, "ALERT": 4},
              "by_kind": {"prompt": 120, "tool_use": 280, "egress": 0, "session": 8, "approval": 0, "control": 4}},
  "block_rate": 0.075,
  "redact_rate": 0.034,
  "detections": {"total": 58, "by_severity": {"info": 0, "low": 14, "medium": 20, "high": 19, "critical": 5},
                 "by_source": {"gateway": 52, "finding": 4, "alert": 1, "verification": 1},
                 "by_control_family": {"secrets": 9, "injection": 12, "pii": 14, "budget": 2,
                                       "exploit_signature": 10, "trajectory": 4, "outcome": 1}},
  "top_detections": [{"name": "signature-scanner", "count": 17, "max_severity": "high"}],
  "top_blocked_tools": [{"tool": "run_code", "count": 6}],
  "sessions": {"total": 8, "active": 1, "halted": 1, "by_risk_level": {"low": 5, "medium": 1, "high": 1, "critical": 1}},
  "verification": {"VERIFIED_SUCCESS": 5, "FAILED_POSTCONDITIONS": 1, "VERIFICATION_INCOMPLETE": 1, "none": 1},
  "interventions": {"proposed": 3, "applied": 3, "active": 1},
  "interception_overhead_ms": {"p50": 0.9, "p95": 2.4, "p99": 6.1},
  "policy_versions": ["v12"],
  "feed_versions": ["local-signatures-v1"]
}
```

- Rates are computed over gateway-evaluated actions, which means every action except `session`
  and `approval` events.
- `by_control_family` comes from the catalog. Names that are not in the catalog are counted under
  `"other"`.

### 5.11 `Intervention`

```json
{
  "signal_id": "sig_9f2e...",
  "ts": "2026-10-03T15:42:11.100Z",
  "session_id": "sess_onb_APP0003",
  "action": "HALT_SESSION",
  "tools": [],
  "scope": "session",
  "ttl_seconds": 1800,
  "expires_at": "2026-10-03T16:12:11.100Z",
  "source_plugin": "trajectory-risk",
  "trigger_event_id": "evt_...9",
  "applied": true,
  "applied_event_id": "evt_...10",
  "active": true,
  "reason": "RISK_CRITICAL"
}
```

The fields come from `policy_signals.signal_json` (`PolicyAdjustmentSignal`) and its `applied`
flag. `applied_event_id` is the `control` event with `change = adjustment_applied` and the same
`signal_id`. `reason` is returned only when it is a token. Free text returns `null`.

### 5.12 `Verification`

```json
{
  "session_id": "sess_onb_APP0003",
  "verification_status": "VERIFICATION_INCOMPLETE",
  "verified_at": "2026-10-03T15:43:03.000Z",
  "checks": [
    {"id": "ONB-P1", "status": "PASS", "detail": null, "evidence_source": "bank_snapshot"},
    {"id": "screen_sanctions", "status": "INCOMPLETE", "detail": "MISSING_RECEIPT", "evidence_source": null}
  ]
}
```

This is the stored `verification_results` projection unchanged, plus `verified_at` (§8, item 3).
`detail` is a fixed code. Readable text comes from the catalog, under
`name = "verification.<check id>"`.

### 5.13 `StoreStats`

```json
{
  "total_events": 4120, "total_alerts": 3, "total_findings": 12, "total_dlq_records": 0,
  "events_by_type": {"TOOL_CALL": 2800, "LLM_INVOCATION": 1200},
  "events_by_status": {"EXECUTED": 3700, "BLOCKED": 310},
  "alerts_by_severity": {"HIGH": 2, "CRITICAL": 1},
  "average_latency_ms": 1.3,
  "pending_deliveries": {"trajectory-risk": 0, "outcome-verifier": 0, "live-feed": 2},
  "db_size_bytes": 5242880,
  "schema_version": 3,
  "evidence_stores": 1
}
```

`evidence_stores` is the number of per-session stores in `--evidence-dir`, or `null` when no
directory is configured. The stub already returns the real value for this field.

This is the only endpoint that shows storage-level enums. It reports operator diagnostics and is
not a domain view.

### 5.14 `SessionSummary` and `SessionDetail`

```json
{
  "session_id": "sess_onb_APP0007",
  "run_id": "run_0042",
  "agent_id": "onboarding-agent",
  "case_id": "APP-0007",
  "contract_id": "contract_APP0007_v1",
  "policy_version": "v12",
  "state": "ended",
  "started_at": "2026-10-03T15:40:00.000Z",
  "ended_at": "2026-10-03T15:43:02.000Z",
  "end_reason": "agent_finished",
  "action_count": 9,
  "blocked_count": 2,
  "detection_count": 4,
  "max_severity": "critical",
  "risk_level": "critical",
  "verification_status": "FAILED_POSTCONDITIONS",
  "usage": { "...": "Usage" }
}
```

`SessionDetail` adds the following fields to `SessionSummary`:

```json
{
  "contract": {
    "contract_id": "contract_APP0007_v1", "role": "kyc_onboarding",
    "target_ids": ["APP-0007"], "allowed_tools": ["read_application", "screen_sanctions", "create_client"],
    "postconditions": ["ONB-P1", "ONB-P2"],
    "budget": {"tokens": 20000, "tool_calls": 30, "cost_usd": null},
    "policy_version": "v12", "policy_hash": "<sha256>", "feed_version": "local-signatures-v1"
  },
  "run": {"lifecycle": "SEALED", "sealed_at": "...", "expired_at": null, "total_events": 18},
  "detections_by_severity": {"info": 0, "low": 1, "medium": 1, "high": 1, "critical": 1},
  "active_interventions": [ "Intervention ..." ],
  "verification": { "...": "Verification or null" }
}
```

The `contract` block is the stored `TaskContract` **without `objective`**. `run` is `null` for
sessions without a run binding.

### 5.15 `PolicyConfig`

The complete JSON shapes are the three files in `config/presets/`. All listed fields are required;
unknown keys are rejected at every model boundary. `trajectory_risk` currently accepts only `{}`.
Model definitions and the generated OpenAPI schema are in `configuration/models.py`.

| Field | Type and constraints |
|---|---|
| `name` | string, `[A-Za-z0-9][A-Za-z0-9_-]{0,39}`; must match path |
| `description` | string, at most 512 characters |
| `allowed_tools` | 1–100 unique registered tool names |
| `admin_tools` | 0–100 unique registered tool names |
| `require_approval` | 0–100 unique names, subset of `allowed_tools ∪ admin_tools` |
| `budget.tokens` | integer, 1–1,000,000 |
| `budget.tool_calls` | integer, 1–1,000 |
| `budget.cost_usd` | finite number 0–1,000 or `null` (no financial cap) |
| `allowed_models` | 1–100 unique operator-approved local model IDs; `[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}`. IDs are syntax-validated, not provider-discovered; slash-separated OpenCode provider IDs are not accepted by the local prompt gateway. |
| `max_output_tokens` | integer, 1–8,192, matching the local prompt gateway |
| `feed_version` | bounded telemetry-safe identifier |
| `controls.email_recipients` | at most 100 email-shaped strings, each at most 254 characters |
| `controls.egress_hosts`, `controls.model_source_hosts` | at most 100 hostnames each, at most 253 characters; no schemes, ports or paths |
| `controls.allowed_model_suffixes` | at most 20 suffixes of the form `.safetensors`, at most 40 characters |
| `auditors` | 0–32 entries: `id`, `type`, `config`; unique telemetry-safe IDs |
| `intercept.trajectory_risk` | empty reserved object; stored-only |
| `intercept.velocity_guard.window_s` | finite number, greater than 0 and at most 3,600; stored-only |
| `intercept.velocity_guard.max_calls` | integer, 1–10,000; stored-only |
| `intercept.feedback.enabled` | boolean; stored-only |
| `intercept.feedback.allow_agent_scope` | must be `false`; stored-only |
| `intercept.feedback.max_ttl_s` | integer, 1–86,400; stored-only |
| `intercept.feedback.max_signals_per_session_per_minute` | integer, 1–1,000; stored-only |
| `intercept.feedback.allowed_actions` | map of `trajectory-risk` / `velocity-guard` to at most 5 adjustment actions per plugin; stored-only |
| `intercept.semantic_guard.block_threshold`, `approve_threshold`, `alert_threshold` | finite numbers, `0 ≤ alert ≤ approve ≤ block ≤ 1`; stored-only risk-score thresholds, not adherence percentages |
| `intercept.semantic_guard.on_error` | must be `BLOCK`; stored-only |

Auditor types are a discriminated union:
- `pattern_scanner`: `config: {patterns: string[], action}`. At most 256 literal patterns,
  each 1–256 characters. They are not executable regular expressions.
- `classified_scanner`: `config: {classes: string[], action}`. 1–5 classes drawn from
  `pesel`, `iban`, `aws_access_key`, `private_key`, `api_key`.
- `tool_allowlist`: `config: {allowed_tools: string[]}`; at most 100 registered tool names.

Scanner `action` is `BLOCK`, `REDACT`, `REQUIRE_APPROVAL` or `ALERT`.
Adjustment actions are `ALERT`, `REQUIRE_APPROVAL_FOR`, `BLOCK_TOOLS`, `STRICT_MODE`, `HALT_SESSION`.
Arbitrary webhook endpoints, filesystem paths, handlers, executable plugin imports and credential
fields are not accepted. Booleans are not numbers. JSON duplicate keys and non-finite constants
are rejected before model validation.

**Enforcement boundary:** the selected config supplies tools/admin tools, approvals, auditors,
egress controls, model allowlist, output cap, feed identity and per-session budgets to interception.
Model controls/token accounting apply where calls actually traverse `PromptGateway`; this does not
claim that every OpenCode provider request is intercepted or that financial pricing is implemented.
The `intercept` plugin settings are stored and validated only. The consume plane continues using
its existing configuration; no VelocityGuard or semantic inference implementation is added here.

### 5.16 Configuration results

`ConfigSummary`: `{name: string, preset: true, description: string, revision: string}`.

`ConfigUpdateResult`:
- `name`, `revision`: saved config identity and SHA-256 of canonical validated JSON.
- `updated_at`: UTC timestamp of the last content change.
- `selected`: whether this config's **name** is selected, even if its saved revision differs.
- `active_revision`: the currently selected revision globally, regardless of the saved config name.
- `requires_selection`: `true` unless this name/revision is already selected.

`ConfigSelectionRequest`: `{name: string, revision: string}`; both fields required, extras rejected.
`ConfigSelectionResult`: `{name: string, revision: string, selected_at: UTC timestamp,
effective_for: "new_sessions"}`. All result fields are always present and non-null.

### 5.17 ConfigService lifecycle and deployment

The backend object `configuration.service.ConfigService` is shared by both API applications and
the interception service through a common `CONFIG_DIR` (default `var/config` under the repository).
`--config-dir` overrides that directory for the standalone REST/gateway CLIs. They must point
to the same operator-owned directory, outside the agent workspace. The web session backend passes
this directory to its gateway subprocess. Explicit gateway `--policy` remains a legacy override
and bypasses the managed selection; `simulation.opencode_runner --policy` forwards that override
for reproducible scripted fixtures. Without the override, even the non-interactive runner uses
the managed selection (standard's approval requirement can prevent automatic client creation).

On first startup/access, defaults are seeded from `config/presets/{lenient,standard,strict}.json`
and `standard` is selected. Existing state is never reseeded on restart. The authoritative
`CONFIG_DIR/state.json` contains the three editable configs, selected name/revision/full snapshot,
timestamps and the last 1,000 sanitized update/selection metadata records. Repository preset files
are templates, not overwritten by PUT. Old loose custom files are not imported or selectable.

This single JSON transaction file avoids partial commits between config content and selection.
An initialization marker distinguishes first startup from deleted state; the lock and marker
files contain no policy payloads.
Linux `flock` serializes access across API workers/gateway processes; unique temporary files,
file fsync, atomic replacement and directory fsync provide durable commits on a local filesystem.
Missing/corrupt existing state fails closed; it is not silently reset to defaults. Selection
contains its own immutable snapshot, so editing the selected config does not activate edits.
New Task Contracts pin the selected revision's hash; existing contracts remain unchanged.

## 6. Mapping from storage

### 6.1 Detection sources

| `source` | Storage | One detection per | `name` | `reason` | `action_taken` |
|---|---|---|---|---|---|
| `gateway` | `events` whose final verdict is not `ALLOWED`. Intents are excluded. | Event | `rule_id` of the first non-`ALLOW` auditor, or the auditor name if the rule is `null` | `context.reason_code` | The final `Decision` |
| `finding` | `consumer_findings` | Finding | `rule_id`, for example `risk.trajectory_high` or `gateway.violation` | `details.reason_code` if present, else `null` | The intervention whose `trigger_event_id` matches, else `NONE` |
| `alert` | `alerts` | Alert | `rule` | `evidence.reason_code` if present, else `null` | `action_taken` |
| `verification` | `verification_results` where the status is not `VERIFIED_SUCCESS` | Session | `verification.failed_postconditions` or `verification.incomplete` | `detail` of the first non-`PASS` check | `NONE`. Verification detects after the fact. It does not prevent, and the dashboard must not call it a block. |

Gateway detections are **derived at read time** and not stored twice, so changing the mapping
needs no migration. Gateway detections are queried through `idx_events_type_status`. Findings and
alerts are queried through their own `session_id` and `ts` indexes.

### 6.2 Severity

| Source | Mapping |
|---|---|
| `gateway` | `BLOCK` → `high`. `ALERT` and `REQUIRE_APPROVAL` → `medium`. `REDACT` → `low`. The catalog's `default_severity` overrides this per `name`, so a signature hit can be `critical`. |
| `finding` | Stored severity: `low`, `medium`, `high` or `critical` |
| `alert` | Stored uppercase severity, lowercased (`INFO` → `info`) |
| `verification` | `FAILED_POSTCONDITIONS` → `critical`. `VERIFICATION_INCOMPLETE` → `high`. |

### 6.3 Store methods used

| Endpoint | Existing method | New read query needed |
|---|---|---|
| trajectory `session` | `EventStore.get_events_by_session_seq` | Paged variant with `from_seq` and `to_seq` |
| trajectory `run` | `EventStore.get_run_events` | Paged variant |
| trajectory `case` and `agent` | `get_events_by_case`, `query_events(agent_id=...)` | Keyset pagination on `(ts, rowid)` instead of `OFFSET` |
| actions | `query_events` | Filters for `decisions`, `name`, `side_effects`, `since`, `until` and `action_id` (`json_extract`) |
| action | `get_event`, plus `to_consumer_v21` | Related records by `context.action_id`, and neighbours by `seq` |
| detections | `list_consumer_findings`, `get_recent_alerts` | Cross-session queries with time and severity filters, and a k-way merge of the four sources by `ts` |
| verification | `get_verification` | – |
| sessions | – | `list_sessions`: `session_sequences` left join `task_contracts`, plus `session` phase events |
| interventions | – | `list_policy_signals` with filters |
| stats | `get_stats`, `pending_deliveries` | – |

All new queries live in a new `persistence/query.py` (`ReadQueries`). It is read-only, takes its
own read-only connection, and every result goes through the existing `sanitize_*` functions, as
the current readers do.

## 7. Security notes

- **Evidence remains read-only:** evidence connections use `mode=ro` and evidence routes have no
  write SQL. Configuration writes use a separate backend store, not the evidence write path.
- **Management authentication:** the two PUTs require an operator-managed `CONFIG_ADMIN_TOKEN`.
  Without it they fail closed with `401`. This is separate from gateway/agent tokens and must be
  supplied securely to the REST/web server, not embedded in preset files, frontend assets or logs.
  Gateway/OpenCode subprocess environments exclude it. Use a trusted frontend backend/proxy to
  attach it, or authenticated operator requests; CORS and loopback binding are not authorization.
- **Unauthenticated reads:** the read side trusts its network position. It binds loopback by default, so on
  a shared machine any local process can read the sanitized evidence, and that includes an agent
  that can run shell commands. In the governed pipeline the agent only gets gateway-backed tools,
  so it has no direct way to call this port. The API runs in a separate process that does not get
  the gateway tokens (`INTERCEPT_TOKEN` and `INTERCEPT_ADMIN_TOKEN`).
- **No content dereference:** `ContentRef` metadata is returned, but `agent_content` bodies are
  not served. A body viewer would need its own authorization and redaction review. It is out of
  scope.
- **Bounded work:** `limit` is at most 1,000, time series have at most 1,000 points, exports
  follow the store quotas, and each query is bounded by the SQLite busy timeout. Expensive
  aggregates such as `/metrics/*` may be cached for 1 s.
- **Public demo:** never bind this API to a public interface. If judges reach the dashboard over
  the network, keep the API on loopback and let the dashboard backend proxy the reads it needs.

## 8. Required persistence changes

These are gaps between what the contract above needs and what Layer 2 stores today:

| # | Gap | Change | Effect if not done |
|---|---|---|---|
| 1 | `_finding_projection` drops `created_at`, so findings have no timestamp | Add `created_at` (validated timestamp) to the allowlist in `EventStore._finding_projection` | Use the `ts` of `trigger_event_id` as the detection `ts` |
| 2 | Finding `details` are dropped, so trajectory risk has only a level | Allowlist numeric `details.expected_loss`, `details.failure_probability`, `details.signals` (`{code: int}`) and token `details.reason_code` | `Trajectory.risk.expected_loss`, `failure_probability` and finding `reason` are `null` |
| 3 | `verification_results` has no timestamp | Store `verified_at` in the projection | Use the `ts` of the session `ended` event |
| 4 | No cross-session finding or alert queries, and `get_recent_alerts` has no filters | Add `ReadQueries.detections(...)`, plus an index `consumer_findings(session_id)` and an extracted `created_at` column with an index | – (needed for §4.7) |
| 5 | No session listing | Add `ReadQueries.list_sessions(...)` | – (needed for §4.2) |
| 6 | `actual_usage.actual_cost` is never written, so cost is always 0 | Layer 1 computes cost from a per-model price table in the policy file and writes `actual_cost` and `usage_source` | `cost_usd` is `0` and `source` is `estimated` |
| 7 | No daily budget exists in the policy, though the brief asks for per-session and per-day budgets | Add `budget_daily` to the policy file and enforce it in Layer 1. The API only reports it. | `UsageBucket.budget` is `null` |
| 8 | No detection catalog | Add `config/detection-catalog.yaml` with titles, reason texts, OWASP tags and control families, covering every auditor rule, consume-plane rule ID and verification check | `reason_text` is `null`, `owasp` is empty, families count as `"other"` |

None of these changes alters Event Envelope v2.1 or the decision semantics.

## 9. Out of scope for v1

- Evidence writes, approvals, live-session policy mutation, retention pruning and consumer admin stay on their existing
  trusted paths. See [application-documentation.md §5.4](application-documentation.md#54-gateway-dashboard-api-contract-for-judge-ui)
  and [persistence.md](persistence.md).
- Push delivery. Dashboards poll this API, and live gateway decisions come from the gateway's
  `/api/v1/events/stream` SSE. A future `/api/v1/stream` could push detections, but it would be
  volatile and never a substitute for the durable lists.
- Per-principal read scoping. Reads rely on the loopback bind; config PUTs require management authentication. `AuditReader`
  and `ReadScope` remain the API for principal-scoped run export.
- Content bodies (§7), and the consume plane's in-process metric gauges. Those are exposed by the
  consume-plane runtime (`/consumer/metrics`, [consumer-plane.md §9.2](consumer-plane.md#92-metrics))
  and are not persisted.

## 10. Documentation changes made with this file

- **Config management:** shared `configuration/` models/router/service mounted in the dashboard REST
  and web servers; two authenticated PUTs, three real config reads, durable backend selection, and
  session-binding integration into interception. Config state is separate from evidence SQLite.

- **New:** `docs/rest.md` (this file).
- **New code:** the `persistence/http_api/` stub (FastAPI and uvicorn) and `tests/test_http_api_stub.py`.
- **Startup:** `simulation/live_pipeline.py` (`scripts/run_live_pipeline.sh`) starts the API next to
  the gateway. The new `scripts/run_rest_api.sh` runs it on its own. Both are documented in
  [scripts/README.md](../scripts/README.md).
  `pyproject.toml` adds `fastapi` and `uvicorn` to the dependencies and `httpx2` to the dev dependencies.
- [persistence.md](persistence.md): the statements "no public endpoint" and "no public
  export/admin endpoint" now say that the write and admin APIs stay internal, and that a
  read-only HTTP API is planned in this file.
- [application-documentation.md §5.4](application-documentation.md#54-gateway-dashboard-api-contract-for-judge-ui):
  a note that §5.4 covers the gateway (Layer 1) endpoints, and that history, trajectories,
  detections and usage come from this read API.
- [dashboard/dashboard-ui.md](dashboard/dashboard-ui.md), open question 3: the dashboard reads
  history, trajectories and metrics from this API.
- [consumer-plane.md §9.2](consumer-plane.md#92-metrics): an explanation of how the in-process
  `/consumer/metrics` differs from the persisted usage metrics here.
