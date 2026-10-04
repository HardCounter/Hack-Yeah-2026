# Consume Plane: Implementation Notes

> Current integration: [integrated-runtime.md](integrated-runtime.md). The canonical consumer
> wire contract is Event Envelope v2.1 and decisions are ALLOW/BLOCK/REDACT/
> REQUIRE_APPROVAL/ALERT. Older v1/v2.0 examples, uppercase storage enums and
> standalone/unwired status notes below are historical design or internal formats;
> they do not define additional supported external contracts.

Companion to [consumer-plane.md](consumer-plane.md). It records what is built, the decisions taken
where the design was silent, and what is deferred.

**Status (2026-10-03):** phase 0 (contract) and the minimal core of phase 1 (runtime) are
implemented and tested with in-memory and JSONL adapters. Nothing is connected to Layer 1 or
Layer 2 yet, and no built-in detection plugins exist.

## What exists

| Area | Path | Tested by |
|---|---|---|
| `AgentAction` model and payloads | `contracts/action.py` | `test_decode.py` |
| Wire decoder, envelope v2.1 ([spec](consumer-plane-event-envelope.md)) | `contracts/wire.py` | `test_decode.py` |
| Task Contract, findings, proposals, signals | `contracts/task_contract.py`, `consume_plane/model/outputs.py` | `test_feedback.py`, `test_manager.py` |
| Ports (protocols) | `consume_plane/ports/` | indirectly |
| Plugin SDK surface | `consume_plane/sdk.py` | `tests/support/consume_plane/fixtures/plugins/velocity_observer.py` |
| Memory and JSONL adapters | `consume_plane/adapters/` | all tests |
| Config (`consume_plane.yaml`) | `consume_plane/runtime/config.py` | `test_loader.py` |
| Loader and registry (drop-in files, `module:Class`, embedded classes) | `runtime/loader.py`, `registry.py` | `test_loader.py` |
| Plugin context (snapshot, permissions, buffered outputs) | `runtime/context.py` | `test_manager.py` |
| Completion ledger and per-plugin dead letters (SQLite) | `runtime/ledger.py` | `test_manager.py` |
| Manager (partitions, timeouts, retries, settle) | `runtime/manager.py` | `test_manager.py` |
| Feedback controller | `runtime/feedback.py` | `test_feedback.py` |
| CLI replay | `consume_plane/__main__.py` | `test_replay_cli.py` |
| Consumer-only drop-in fixture | `tests/support/consume_plane/fixtures/plugins/velocity_observer.py` | `test_loader.py`, `test_replay_cli.py` |
| Built-in `trajectory-risk` plugin ([model](trajectory-risk-model.md)) | `consume_plane/plugins/trajectory_risk.py` | `test_trajectory_risk.py` |
| Test and demo scripts | `scripts/test_consume_plane.sh`, `scripts/run_consume_plane.sh` | run manually |

```bash
uv sync
uv run pytest tests/support/consume_plane -q
uv run python -m consume_plane --replay tests/support/consume_plane/fixtures/velocity_burst.jsonl \
    --contracts tests/support/consume_plane/fixtures/velocity_burst.contracts.jsonl
```

Dependencies added: `pyyaml` (runtime, MIT) and `pytest` (dev group, MIT).

## Decisions where the design was silent

1. **Package name `consume_plane/`** (resolves open question 3 in the design). The config file is
   `consume_plane.yaml`, with top-level keys `consume_plane:` and `plugins:`.
2. **Envelope.** The consume plane decodes only v2.1 (see the envelope doc). This narrows the
   design's "parse both v1.0 and v2.0" to one format plus a converter in Layer 2.
3. **Explicit discovery.** A drop-in file must export `PLUGINS = [...]`. A file without it is a
   load error rather than being silently ignored. Files starting with `_` are skipped.
4. **Configured but missing plugin.** An entry under `plugins:` with no `handler` that matches no
   drop-in file is a load error. A disabled entry for a missing plugin is not an error.
5. **Setup failures** follow `on_plugin_load_error`, the same as import failures. In `fail` mode
   all problems are reported together.
6. **Snapshot bound.** `ctx.trajectory(up_to=...)` can narrow the snapshot but never look past the
   handled event's `seq`. `needs_trajectory=False` makes `ctx.trajectory()` raise `PermissionError`.
7. **`TrajectoryReader.session(limit=N)`** returns the most recent N actions.
8. **Semantic findings.** A finding without `confidence` in [0, 1] raises (the plugin run fails).
   `critical` is lowered to `high` and records `details.severity_capped_from`.
9. **Event time everywhere.** TTLs, rate limits, and signal timestamps in the feedback controller
   use `action.ts`, not wall-clock time, so replays are deterministic.
10. **Feedback defaults deny.** A plugin missing from `feedback.allowed_actions` may propose nothing.
11. **"Tighten-only" is structural.** No proposal type removes a restriction, so a loosening request
    cannot be expressed. A proposal is rejected as `already_active` when an unexpired signal for the
    same scope has the same action and a superset of its tools, or when a `HALT_SESSION` is active.
    The design's "merge and extend TTL" is simplified to "reject the duplicate".
12. **Signal and finding IDs are deterministic** (hashes of plugin, version, event, and rule), so
    retries produce the same IDs.
13. **Plugin failure findings** contain only the exception type. The message (truncated to
    300 characters) stays in the ledger, because exception text can carry payload content.
14. **Plugin metric labels.** The `plugin` label is always set by the manager and overrides any
    plugin-supplied `plugin` label.
15. **Relative paths** in the config are resolved against the config file's directory.
16. **The ledger persists across runs.** Replaying the same events against the same
    `ledger_path` reruns nothing, because every plugin is already `done`. Use a fresh path, or
    `":memory:"`, for repeat demo replays. Bumping a plugin `version` reprocesses its events by design.

## Deviations from the design (known gaps)

| Design says | Implemented | Consequence |
|---|---|---|
| Sink outbox; sink failure does not fail the plugin | No outbox. A sink or feedback failure during commit counts as a plugin failure and is retried | Findings can be written twice. They carry the same `finding_id`, and readers must deduplicate on it. `JsonlSink` can contain duplicate lines |
| Partial commit is atomic | Findings are written, then proposals are submitted | A failure between the two re-runs the plugin. The proposal becomes `already_active` if it went through |
| Shutdown nacks unstarted work | `stop()` drains the queued work, then tears down | Slower shutdown under load. Nothing is lost |
| `ctx.state`, `ctx.clock` | Not implemented | Plugins recompute from the trajectory and use `action.ts` |

## Deferred (not built)

- Remaining built-in plugins from design §10 (`usage-accountant`, `loop-detector`, `scope-drift`,
  `repeat-side-effect`, `step-order`, `outcome-verifier`, `trajectory-grader`).
- Layer 2 adapters for the real Queue 2 and store, `StoreSink`, `SseSink`, and a feedback
  transport into Layer 1's policy cache. `MemoryFeedbackChannel` only collects signals.
- Circuit breaker, `/consumer/health` and `/consumer/metrics` endpoints, and metric cardinality caps.
- Config hot reload, and the plugin-directory permission warning.
- Webhook plugins.
- Feedback decision log persistence. `FeedbackController.log` is in memory only.
- A check that no finding or log line contains the planted PESEL, IBAN, or key values. This needs
  real plugins that handle content.
