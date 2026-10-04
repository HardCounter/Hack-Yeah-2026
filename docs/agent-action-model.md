# Agent Action Model and the Three-Plane Pipeline

**Status (2026-10-03):** implemented. Verified with `uv run pytest` (290 passed, 1 skipped:
the Ollama model is not pulled), the Node adapter tests, `scripts/check_opencode_pipeline.sh`, and the
scripted runs `simulation.agent APP-0001` (`VERIFIED_SUCCESS`) and
`APP-0003 --fault skip_step:screen_sanctions` (`VERIFICATION_INCOMPLETE`).

An **agent action** is a prompt or model request, a tool use, an egress call, or a
session, approval or control lifecycle event. It passes through the three planes in three representations,
and each translation between them happens in exactly one place.

```text
Layer 1  interception     contracts.ActionProposal ──► GatewayDecision (ALLOW/BLOCK/REDACT/REQUIRE_APPROVAL/ALERT)
                                   │  persistence.events.build_action_event(...)
                                   ▼
Layer 2  persistence      ActionEventEnvelope (internal storage, uppercase enums, schema "2.0")
                                   │  GovernedPersistence.append / .intent  → Layer 2 assigns session seq + outbox job
                                   │  persistence.adapters.to_consumer_v21  → Event Envelope v2.1 dict (the wire contract)
                                   │  persistence.adapters.to_agent_action  = contracts.decode_event(to_consumer_v21(e))
                                   ▼
Layer 3  consume plane    contracts.AgentAction ──► plugins (PersistenceEventSource / PersistenceTrajectoryReader)
```

## Single sources

| Concern | The only definition | Used by |
|---|---|---|
| Kinds, statuses, `ActionProposal`, `AgentAction` and payloads | `contracts/action.py` | all three planes; `consume_plane.sdk` re-exports |
| Decisions | `contracts/decision.py` (`DECISIONS`, `GatewayDecision`) | Layer 1, persistence vocabulary |
| Wire `action_type` → kind | `contracts.action.WIRE_ACTION_TYPES` / `kind_for_action_type` | decoder, `persistence/governed.py` filters |
| Decision → canonical status | `contracts.action.STATUS_FOR_DECISION` / `status_for_decision` | `persistence/vocabulary.py` |
| Event Envelope v2.1 → `AgentAction` | `contracts/wire.py` (`decode_event`) | consume plane, persistence adapter, tests |
| Canonical ↔ storage enums | `persistence/vocabulary.py` | the event builder, the v2.1 encoder, intent checks |
| Gateway action → storage envelope | `persistence/events.py` (`build_action_event`) | tool calls, model requests, session lifecycle, feedback control events |
| Storage → v2.1 / `AgentAction` | `persistence/adapters/consumer_v21.py` | `GovernedPersistence.wire_*`, consume-plane adapters |
| Feedback ladder (`ADJUSTMENT_ORDER`) | `contracts/feedback.py` | consume-plane feedback controller and config |
| Durable dispatch intent check | `persistence.vocabulary.is_intent` | prompt budget accounting, governed persistence, consume adapters |

Unchanged on purpose:
- **Storage format:** SQLite rows, the uppercase enums and internal schema `"2.0"`, so no migration.
- **The v2.1 wire format.**
- **Decision semantics.**

## Code layout

```text
contracts/            shared models, no runtime imports: action, decision, feedback, task_contract, verification, wire
intercept/
  policy/             runs.py (per-run tool admission), auditors.py (pipeline), config.py (single loader)
  governed/           gateway.py (GovernedGateway), prompts.py (PromptGateway), baseline.py, registry.py
  service/            server.py (HTTP transport + standalone gateway), local.py (HTTP over governed runtime),
                      receiver.py (observe-only diagnostics)
  tools/              execution.py + worker.py (subprocess tool execution for the standalone gateway)
  cli/                operator.py (bind a session), prepare.py (operator demo configs)
persistence/
  models.py           storage dataclasses + enums          vocabulary.py   canonical <-> storage
  events.py           Layer 1 -> envelope builder           adapters/       storage -> v2.1 / AgentAction
  store.py schema.py settings.py privacy.py writer.py reader.py queue.py worker.py business.py
  maintenance.py governed.py demo.py
consume_plane/        runtime, ports, adapters (memory, jsonl, persistence), plugins; model/outputs.py
```

Entry points moved with their modules:
- `python -m intercept.service.server`, `intercept.service.local` and `intercept.service.receiver`
- `intercept.cli.operator` and `intercept.cli.prepare`
- `intercept.tools.worker`, spawned by `tools/execution.py`

Scripts and docs are updated.

## Changes from the earlier layout

- **One action, two parallel representations, now unified.** The consume plane owned `AgentAction`
  and the decoder in `consume_plane/model/`. Layer 1 used `contracts.ActionProposal` and hand-built
  storage envelopes in three places: `intercept/governed.py` (tool calls), the feedback control event,
  and `intercept/prompts.py`. Each carried its own decision → status/verdict tables, and
  `consumer_v21.py` held the inverse tables. Now there is one canonical model, one builder and one
  translation module. `consume_plane/model/{actions,decode,contract}.py` were removed; import from
  `contracts` instead.
- **`AgentAction.action_id`** is now decoded from the v2.1 `action_id` field. It correlates the
  intent, result and receipt evidence of one gateway action.
- **`intercept/`** was one flat package mixing two stacks: the standalone HTTP policy gateway and the
  governed runtime. It is now grouped by role. The 1,051-line `governed.py` lost its baseline capture
  to `governed/baseline.py`. `GovernedError` lives in `intercept/governed/__init__.py`.
- **`persistence/models.py`:** four enum parsers became one `_parse_enum`, and seven copies of
  `to_json`/`from_json` became one mixin, with no behaviour change (574 → 523 lines). Raw
  `status.value == "PENDING"` checks became `is_intent`.
- **`tests/support/test_opencode_pipeline.py`:** the test now runs the same OpenCode binary its skip check
  found. Before, it fell back to an uninstalled pinned path and failed when only `opencode` on PATH
  was available.

Small behaviour changes from the shared builder. All are covered by the existing tests.

| Before | Now |
|---|---|
| Tool-call `trace_id` was `run_id or ""` | `run_id or session_id`, as model requests already did |
| An auditor row's `rule_id` was dropped on the tool path (only `code` was read) | Either key is recorded |
| Model-request `reason_code` was untruncated | Capped at 64 characters, like tool calls |
