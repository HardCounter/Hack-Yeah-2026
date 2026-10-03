# Event Envelope v2.1 (Persistence → Consume Plane)

**Status (2026-10-03):** proposed by the consume-plane work and implemented in its decoder
(`consume_plane/model/decode.py`). Layer 1 and Layer 2 have not adopted it yet, so it needs
team confirmation.

## Why this exists

The consume plane needs fields that neither existing envelope provides:

| Need | Envelope v2.0 (`application-documentation.md` 5.1) | Envelope v1.0 (`system-architecture.md` 6) |
|---|---|---|
| Per-session order (`seq`) for snapshots and ordering | missing | `step_id` only, set by the agent wrapper |
| Lifecycle events (session start/end) to trigger outcome verification | missing | `case_started` / `case_closed` |
| Approval and policy-change events for the audit loop | missing | `human_decision` only |
| Policy version the gateway applied | missing | missing |
| Final gateway decision | only per auditor | missing |
| Attributing scripted faults | missing | `payload.fault_injected` |
| Bodies kept out of the event (privacy, size) | `result` inline | `*_ref` pointers |

v2.1 is v2.0 with these additions. It keeps v2.0 field names wherever they exist, so a
v2.0 producer needs additions only, not renames.

## Envelope

```json
{
  "schema_version": "2.1",
  "event_id": "evt_01J9ZK3Q8W2M5N7R4T6V8X0Y1A",
  "seq": 7,
  "ts": "2026-10-03T15:42:10.500Z",

  "run_id": "run_0042",
  "trace_id": "tr_4bf92f3577b34da6a3ce929d0e0e4736",
  "session_id": "sess_onb_APP0007",
  "case_id": "APP-0007",
  "agent_id": "onboarding-agent",
  "step_id": 4,
  "parent_span_id": "span_3",

  "action_type": "tool_call",
  "status": "completed",
  "action_details": { "...": "see below" },

  "interception_metadata": {
    "final_decision": "ALLOW",
    "policy_version": "v12",
    "auditor_decisions": [
      {"auditor": "credential-shield", "decision": "ALLOW", "rule_id": null, "latency_ms": 0.8}
    ],
    "interception_overhead_ms": 1.2
  },
  "metrics": {"input_tokens": 0, "output_tokens": 0, "latency_ms": 142.5, "cost_usd": 0.0},
  "fault_injected": false
}
```

### Required fields

`schema_version` (must be `"2.1"`), `event_id`, `seq`, `ts`, `session_id`, `agent_id`,
`action_type`, `status`. All other fields are optional. `interception_metadata` is omitted for
events that did not pass the gateway (`session`, and `approval` when it is written by the human queue).

### Field rules

| Field | Rule | Owner |
|---|---|---|
| `event_id` | Globally unique, stable across retries. The consume plane deduplicates on it | Layer 1 |
| `seq` | Integer ≥ 0, strictly increasing per `session_id`, assigned **at persist time** so it matches store order | Layer 2 |
| `ts` | ISO 8601 with timezone (`Z` accepted). This is the time the gateway observed the action, and plugins use it as event time | Layer 1 |
| `session_id`, `agent_id`, `run_id`, `case_id` | Injected by the gateway from authenticated headers, never by the model | Layer 1 |
| `status` | `completed`, `blocked`, `redacted`, `pending_approval`, or `failed` | Layer 1 |
| `fault_injected` | `true` only for scripted faults from the test harness | Layer 1 |

### `action_type` → consume-plane `kind`

| `action_type` | `kind` | `action_details` |
|---|---|---|
| `llm_call` | `prompt` | `model`*, `provider`, `messages: [ContentRef]`, `completion: ContentRef\|null`, `tool_calls_requested: [{name*, parameters}]`, `stop_reason` |
| `tool_call`, `mcp_tool` | `tool_use` | `name`*, `side_effect: read\|write\|irreversible` (default `read`), `transport: inproc\|mcp\|http` (default `inproc`), `parameters: {}`, `result: ContentRef\|null`, `error` |
| `egress_http` | `egress` | `method`*, `host`*, `path`, `status_code`, `body: ContentRef\|null` |
| `session` | `session` | `phase: started\|ended`*, `contract_id`, `policy_version`, `end_reason` |
| `approval` | `approval` | `target_event_id`*, `decision: approved\|rejected\|expired`*, `approver_role`, `delay_ms` |
| `control` | `control` | `change: policy_reloaded\|adjustment_applied\|adjustment_expired`*, `policy_version`, `signal_id` |
| anything else | the raw string | kept as-is and delivered only to plugins that subscribe to `"*"` |

`*` = required within `action_details`.

`parameters` (tool arguments) are inline. Policy and trajectory checks need them, and Layer 1
has already redacted them. Bodies are **never inline**.

### ContentRef

```json
{"ref": "store://agent_content/<id>", "sha256": "<hex>", "size_bytes": 1832,
 "redacted": true, "trust": "untrusted"}
```

`ref` and `sha256` are required. `trust` defaults to `untrusted`. Tool results, documents, web
pages, and model completions are `untrusted`. The decoder rejects an inline body (for example the
v2.0 example's `"result": {"success": true}`). Layer 2 is responsible for moving bodies into the
content store before it enqueues the event.

## Delivery guarantees the consume plane relies on

1. An event is written to the store before it is put on Queue 2.
2. Queue 2 delivers at least once, with ack/nack. A nack carries a `retry_after_s` hint, and
   Layer 2 owns backoff and the message-level DLQ.
3. Within a session, Queue 2 delivers in `seq` order except during redelivery.

## Not supported

v1.0 events (`system-architecture.md` §6) are not decoded. If both producers must coexist,
add a v1.0 → v2.1 converter in Layer 2 instead of teaching consumers two formats.
