# Control-plane decision trace

Every time a consume-plane (Layer 3) plugin reaches a decision, the decision is written to the
session's evidence database and can be inspected over REST. This includes decisions to do nothing:
"risk unchanged", "review skipped", "outcome verified". Failed plugin runs are recorded too, with a
fixed reason code.

Findings say what was detected. The decision trace says **why each plugin did or did not act, on
which step, with which inputs**, and which findings and policy adjustments came out of it. It is the
audit log of the asynchronous control plane.

```text
 Layer 1 gateway ──► Layer 2 evidence store ──► Layer 3 ConsumerManager ──► plugin.handle(action, ctx)
                          (events, outbox)              │                      │ ctx.record_decision(...)
                                                        │                      │ ctx.emit_finding(...)
                                                        │                      │ ctx.propose_adjustment(...)
                                                        ▼                      ▼
                          plugin_decisions table ◄── _commit: findings + feedback + decisions (one run)
                          (same evidence store)  ◄── _record_failure: failed / dead_lettered runs
                                    │
                                    ▼
          GET /api/v1/sessions/{id}/decisions · GET /api/v1/decisions/{id} · NDJSON export
```

## 1. What is recorded

One `PluginDecision` per decision point (`consume_plane/model/outputs.py`):

| Field | Meaning |
|---|---|
| `ts` | when the decision was committed |
| `session_id`, `run_id`, `agent_id`, `case_id` | session context |
| `trigger_event_id`, `trigger_seq` | the event the plugin was handling |
| `plugin`, `plugin_version`, `method` | which plugin decided (`deterministic` or `semantic`) |
| `outcome` | `decided`; `failed` (the run raised and will be retried); `dead_lettered` (given up) |
| `decision` | UPPER_SNAKE code, e.g. `NO_CHANGE`, `LEVEL_RAISED`, `VERIFIED_SUCCESS`, `REVIEW_SKIPPED` |
| `reasoning` | one brief sentence: why, in codes, names and numbers |
| `reason` | **failures only**: exception type or `TIMEOUT`, never the error message |
| `factors` | the inputs behind the decision (scores, levels, counts, thresholds) |
| `attempt`, `duration_ms` | delivery attempt and plugin run time |
| `finding_ids`, `adjustments` | outputs of the same run; each adjustment carries the feedback controller's outcome and `signal_id` |

Built-in decision points:

| Plugin | When | Decisions |
|---|---|---|
| `trajectory-risk` | every tool/egress event | `NO_CHANGE`, `LEVEL_RAISED` (+ proposed approval/halt) |
| `outcome-verifier` | session end | `VERIFIED_SUCCESS`, `FAILED_POSTCONDITIONS`, `VERIFICATION_INCOMPLETE` |
| `goal-alignment-judge` | triggering events (writes, egress, gateway interventions, every N tools, session end) | `REVIEW_SKIPPED` (`SAMPLING` / `BUDGET` / `BUSY` / `UNAVAILABLE`), `REVIEW_STARTED`, `VERDICT_ALIGNED` / `VERDICT_ALERT` / `VERDICT_APPROVAL_REQUIRED`, `REVIEW_DROPPED`, `REVIEW_FAILED` |
| manager, any plugin | failure / outputs without a recorded decision | `PLUGIN_FAILED`, `PLUGIN_GAVE_UP`, `OUTPUT_EMITTED` |

A plugin run that records no decision and produces no output leaves no trace. That is deliberate:
plugins only record at their real decision points.

Example (`out-of-scope` scenario, real run):

```text
seq  trigger                  plugin            decision      reasoning
  2  read_application:BLOCK   trajectory-risk   NO_CHANGE     expected loss 0.02 -> level low (was low); P(failure) 0.38; ...
  8  create_client:ALLOW      trajectory-risk   LEVEL_RAISED  expected loss 8.51 -> level high (was medium); ...; proposing require approval  [findings=1, REQUIRE_APPROVAL_FOR=accepted]
 10  ended:completed          outcome-verifier  VERIFIED_SUCCESS  15/15 checks passed  [findings=1]
```

## 2. How it is produced

1. A plugin calls `ctx.record_decision(code, reasoning, **factors)`. The call is buffered with the
   run's findings, metrics and proposals (`consume_plane/runtime/context.py`).
2. When `handle` returns, `ConsumerManager._commit` writes the findings and submits the proposals to
   the feedback controller. It then writes the decisions, linked to those finding IDs and to each
   proposal's outcome and signal ID. If the run produced outputs but recorded no decision, it adds
   `OUTPUT_EMITTED`, so no output is ever untraceable.
3. If `handle` raises, times out, or its commit fails, `_record_failure` writes a `failed` entry for
   each attempt and a `dead_lettered` entry when retries are exhausted.
4. Sinks that implement `write_decisions` receive the records:
   - `PersistenceFindingSink` writes them into the session's evidence store (`plugin_decisions` table);
   - `JsonlSink` writes `decisions.jsonl` next to `findings.jsonl`;
   - `MemorySink` keeps them in tests.

**Idempotent.** `decision_id` is a hash of plugin, version, trigger event, outcome, index and
attempt. A redelivered event produces the same IDs, and the store keeps the first write
(`INSERT OR IGNORE`). Retries therefore never duplicate the trace or fail on a different timestamp.

**Compatible.** The table is created when a store is opened. Storage operates under schema `user_version = 3`
(with the read API also accepting stores written with `user_version = 4` during a short-lived build on 2026-10-04),
so stores without decision records remain readable and return an empty trace.

## 3. Privacy

The trace lives in the same audit store as the rest of the evidence, so the same rule applies:
structured metadata only, never interaction content. `persistence.privacy.decision_projection` is
applied by the writer **and again by the read API**:

- identifiers, codes, plugin names and failure reasons must be opaque tokens;
- `reasoning` is kept only if it uses a code/number character allowlist, has no e-mail, date, long
  digit run or secret marker, and is at most 240 characters. Otherwise it becomes `[OMITTED]`;
- each `factors` value must be a number, boolean, code, or small list/map of them. Others are
  dropped one by one, so one bad value never hides the rest;
- failure `reason` is the exception type only, because an exception message can contain data.

Plugins should build `reasoning` from codes, tool names and numbers. The projection is a safety net,
not a licence to write prose.

## 4. Reading it

| Endpoint | Returns |
|---|---|
| `GET /api/v1/sessions/{session_id}/decisions` | the session trace, oldest first. Filters: `plugins`, `outcomes`, `decisions`, `trigger_event_id`; keyset pagination; `summary` counts per plugin and outcome |
| `GET /api/v1/decisions/{decision_id}` | one decision |
| `GET /api/v1/export/sessions/{session_id}` | NDJSON audit export; includes `plugin_decision` records (persisted mode) |

Every item is joined with its `trigger` step (`kind`, `name`, `status`, gateway `decision`) and its
linked `findings`. Field details are in [docs/rest.md](rest.md) §4.21, §4.22 and §5.17.

The trace is also logged as `consume plugin.decision` lines in the pipeline trace, and counted in
the `consumer_decisions_total{plugin,outcome,decision}` metric.

## 5. Inspecting it locally

```bash
scripts/inspect_decisions.sh                          # run 4 governed scenarios, print each trace as a table
scripts/inspect_decisions.sh out-of-scope --json      # full records for one scenario
scripts/inspect_decisions.sh --serve                  # then browse the REST API (curl hints printed)
scripts/inspect_decisions.sh --judge clean            # include the goal-alignment judge (LLM_PROVIDER / .env)
```

Scenarios (`persistence/http_api/decisions_demo.py`) are real runs: scripted agent → gateway →
evidence store → consume plane. They need no model unless you pass `--judge`.

| Scenario | What the trace shows |
|---|---|
| `clean` | low risk until the irreversible `create_client`; `VERIFIED_SUCCESS` |
| `skip-screening` | gateway blocks `create_client`; risk stays low; `VERIFICATION_INCOMPLETE` |
| `duplicate-create` | second `create_client` blocked and traced as `NO_CHANGE` |
| `out-of-scope` | blocked read of another case raises later risk to high, and an accepted `REQUIRE_APPROVAL_FOR` |

## 6. Adding decision points to a plugin

```python
ctx.record_decision(
    "LIMIT_CROSSED",                                   # UPPER_SNAKE code
    f"{n} rejects in this session (limit {limit})",    # brief, codes and numbers only
    rejects=n, limit=limit,                            # factors
)
```

Record at every branch where the plugin decides, including "nothing to do" and "skipped". See
rule 6 in [consume_plane/plugins/README.md](../consume_plane/plugins/README.md).

## 7. Tests

`scripts/test.sh decisions` runs:

| File | Covers |
|---|---|
| `tests/support/consume_plane/test_decision_model.py` | unit: ID determinism, failure codes, record ↔ privacy projection, JSONL and persistence sinks, several decisions per run, retry trace |
| `tests/support/consume_plane/test_decisions.py` | manager: every risk assessment traced and linked to its finding/signal, implicit `OUTPUT_EMITTED`, failures with fixed reasons, timeouts, stable IDs |
| `tests/test_decision_trace_api.py` | privacy projection, store idempotency, REST filters/pagination/lookup, export, old stores, tampered rows re-projected |
| `tests/test_decisions_demo.py` | running scenarios end to end through the real gateway, store, consume plane and REST |
| judge and verifier tests | decision points of `goal-alignment-judge` and `outcome-verifier` |

The document-scope false positive is fixed: the gateway records trusted ownership from the
pinned baseline, and risk assessment distinguishes owned documents from foreign attempts.
`test_clean_run_has_no_out_of_scope_signal` passes without an expected-failure marker;
gateway regressions also reject forged ownership. New decisions record step probability,
consequence, expected loss and signals for dashboard projection.

## 8. Known limits

- **The judge has one review slot per session.** With a slow local model, an early background review
  can occupy that slot while later, more important events are traced as `REVIEW_SKIPPED` (`BUSY`), and
  the review can then be `REVIEW_DROPPED` at session end. You can see this with
  `scripts/inspect_decisions.sh skip-screening --judge` on an 8B Ollama model. A hosted model, or a
  queue that re-reviews the latest trigger once the slot frees, avoids it.
- A judge review that runs in the background is traced on the session's *next* event (`REVIEW_STARTED`,
  then the verdict later). A session that never ends leaves the last verdict unrecorded.
- The interception plane (Layer 1) records its own decisions in the action events
  (`interception_metadata`, `reason_code`). This trace covers Layer 3 only.
