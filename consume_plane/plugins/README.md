# Adding a consume-plane plugin

The consume plane (Layer 3) runs plugins **after** an action has been decided and persisted. Plugins
read the session trajectory and the Task Contract, then emit findings, metrics and (optionally)
policy-adjustment proposals back to the gateway. Full design: `docs/consumer-plane.md`.

## 1. Pick the right plane first

| The check… | Put it in |
|---|---|
| looks at one call only, is O(1)/cheap, and must be able to **block** it | **Interception plane**: an auditor in `intercept/policy/auditors.py`, enabled under `auditors:` in the policy/preset |
| needs history (earlier steps, counts, ordering), the Task Contract, persisted bank state, or slow/model calls | **Consume plane**: a plugin (this guide) |

Example: the old `gateway-violations` plugin only relabelled a decision the gateway had already
made, using no history. It was moved into the interception plane as the `domain_blocklist`
auditor, and the gateway now labels each deny itself (`intercept/governed/reason_families.py`).
Do not add a consume plugin for something the hot path can decide on its own.

## 2. Write the plugin

No base class is needed; the loader checks the shape (`consume_plane/runtime/loader.py`). Import only
from `consume_plane.sdk`.

```python
from consume_plane.sdk import AdjustmentProposal, FindingDraft, Subscription


class RepeatedRejects:
    name = "repeated-rejects"          # kebab-case, unique
    version = "1.0.0"
    method = "deterministic"           # or "semantic" (model-based)
    subscription = Subscription(kinds=frozenset({"tool_use"}), tools=frozenset({"reject_application"}))

    async def setup(self, ctx):        # optional; ctx.config is this plugin's config block
        self.limit = ctx.config.get("limit", 2)

    async def handle(self, action, ctx):
        rejects = (await ctx.trajectory()).count(
            lambda a: a.kind == "tool_use" and a.payload.tool == "reject_application" and a.executed)
        crossed = rejects == self.limit + 1  # report once, when the limit is first crossed
        ctx.record_decision("LIMIT_CROSSED" if crossed else "OVER_LIMIT" if rejects > self.limit else "WITHIN_LIMIT",
                            f"{rejects} rejects in this session (limit {self.limit})",
                            rejects=rejects, limit=self.limit)
        if crossed:
            ctx.emit_finding(FindingDraft(
                rule_id="workflow.repeated_rejects", severity="medium",
                summary=f"{rejects} rejects in one session (limit {self.limit})",
                evidence_event_ids=(action.event_id,),
            ))

    async def teardown(self):          # optional
        pass


PLUGINS = [RepeatedRejects]            # required for drop-in files
```

## 3. Register it

- **Drop-in file:** put it in `plugins/` (the `plugin_dirs` in `consume_plane.yaml`) with a
  module-level `PLUGINS` list. Files starting with `_` are skipped. Drop-in plugins are enabled by
  default. See `plugins/velocity_guard.py`.
- **Built-in:** put it in `consume_plane/plugins/` and reference it by import path:

```yaml
plugins:
  repeated-rejects:
    handler: "consume_plane.plugins.repeated_rejects:RepeatedRejects"
    config: {limit: 2}
    # enabled: false        # turns it off without deleting it
```

A load error (bad shape, duplicate name, failing `setup`) stops startup when
`on_plugin_load_error: fail` (default), or skips the plugin with a warning when it is `skip`.

## 4. Subscription

`Subscription(kinds=..., tools=None, statuses=None, agents=None, needs_trajectory=True, needs_content=False)`

- `kinds`: `tool_use`, `prompt` (LLM calls), `egress`, `session`, `approval`, `control`, or `"*"`.
- `tools` filters `tool_use` actions only; `statuses` filters on e.g. `blocked`, `completed`.
- `needs_trajectory=False` / `needs_content=False` are enforced: calling `ctx.trajectory()` or
  `ctx.content()` without them raises `PermissionError`.
- To run once per finished session: `kinds={"session"}` and check `action.payload.phase == "ended"`
  (see `outcome_verifier.py`).

## 5. What `ctx` gives you

| Call | Notes |
|---|---|
| `await ctx.trajectory()` | session actions up to **and including** the current one; never later events |
| `await ctx.contract()` | the trusted `TaskContract`, or `None` |
| `await ctx.content(ref)` | stored bodies (needs `needs_content=True`) |
| `ctx.emit_finding(FindingDraft(...))` | severity `low/medium/high/critical` |
| `ctx.emit_metric(name, value, **labels)` | exposed as telemetry, labelled with the plugin name |
| `ctx.propose_adjustment(AdjustmentProposal(...))` | `REQUIRE_APPROVAL_FOR`, `BLOCK_TOOLS`, `HALT_SESSION`, … |
| `ctx.record_decision(decision, reasoning, **factors)` | trace a decision point (see §6, rule 6) |
| `await ctx.run_blocking(fn, *args)` | run blocking I/O (SQLite, files) off the event loop |
| `ctx.config`, `ctx.log` | config block and a per-plugin logger |

Adjustments are **denied by default**. A plugin may only send the actions listed for it in
`consume_plane.yaml` → `consume_plane.feedback.allowed_actions`.

## 6. Rules

1. **Idempotent.** Delivery is at-least-once. Outputs are buffered and committed only when
   `handle` returns normally, so a retry does not duplicate them. Keep no state that a replay would
   corrupt; recompute from `ctx.trajectory()` instead.
2. **Bounded.** Each call has `plugin_timeout_s` and up to `max_attempts` attempts.
3. **Deterministic plugins are pure** in (action, trajectory, contract, config): no network, no models.
4. **Semantic plugins** must set `confidence` in `[0, 1]` on findings; `critical` is capped to `high`.
5. **No PII or argument values in outputs.** Reference `event_id`s, tool names and fixed codes.
6. **Record every decision point** with `ctx.record_decision("UPPER_SNAKE_CODE", "one brief sentence", **factors)`,
   including "nothing to do" and "skipped" outcomes. Build `reasoning` from codes, tool names and numbers
   only; `factors` are numbers, booleans, codes or small maps/lists of them. The manager persists it with
   the findings and adjustments of the same run, and records failed runs itself. The trace is served by
   `GET /api/v1/sessions/{id}/decisions` (`docs/rest.md` §4.21).

## 7. Test it

Use the in-memory harness from `tests/support/consume_plane/conftest.py`:

```python
import asyncio
from conftest import action


def test_repeated_rejects(harness):
    actions = [action(i, tool="reject_application", side_effect="write") for i in range(1, 5)]
    h = harness(actions, [RepeatedRejects], plugin_config={"repeated-rejects": {"limit": 2}})
    asyncio.run(h.run())
    assert [f.rule_id for f in h.findings()] == ["workflow.repeated_rejects"]
```

Run it with `scripts/test.sh consume`. Cover at least one allowed (no finding) case and one flagged
case, plus any feedback proposal (`h.channel.signals`).

## Checklist

- [ ] It needs history, contract, persisted state, or slow work; otherwise make it an interception auditor.
- [ ] `name`, `version`, `method`, `subscription`, `async handle(self, action, ctx)` defined.
- [ ] Registered (drop-in `PLUGINS` list or `handler:` entry) and config documented in `consume_plane.yaml`.
- [ ] Any adjustment actions added to `feedback.allowed_actions`.
- [ ] Findings and recorded decisions contain no PII or raw argument values.
- [ ] Every decision point calls `ctx.record_decision`.
- [ ] Positive and negative tests pass under `scripts/test.sh consume`.
