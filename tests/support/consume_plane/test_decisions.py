"""Control-plane decision trace: every plugin decision point and failed plugin run is recorded."""
import asyncio
import json

from conftest import action
from consume_plane.plugins.trajectory_risk import TrajectoryRisk
from consume_plane.runtime.config import FeedbackConfig
from consume_plane.sdk import FindingDraft, Subscription
from contracts.task_contract import Budget, TaskContract

CONTRACT = TaskContract(
    contract_id="ctr_1", session_id="sess_1", agent_id="onboarding-agent", role="onboarding analyst",
    objective="Process application APP-0001", target_ids=frozenset({"APP-0001"}),
    allowed_tools=frozenset({"read_application", "create_client"}), postconditions=(),
    budget=Budget(tokens=20000, tool_calls=30), policy_version="v1",
)


def decisions(h, plugin=None):
    items = sorted(h.sink.decisions.values(), key=lambda d: (d.trigger_seq, d.decision_id))
    return [d for d in items if plugin is None or d.plugin == plugin]


def test_trajectory_risk_records_every_assessment_and_links_outputs(harness):
    actions = [action(1, tool="read_application"),
               action(2, tool="create_client", side_effect="irreversible"),   # no screening: medium
               action(3, tool="create_client", side_effect="irreversible"),   # duplicate write: high
               action(4, tool="read_application", args={"app_id": "APP-0002"})]
    h = harness(actions, [TrajectoryRisk],
                feedback=FeedbackConfig(allowed_actions={"trajectory-risk": ["REQUIRE_APPROVAL_FOR"]}))
    h.reader.add_contract(CONTRACT)
    asyncio.run(h.run())

    trace = decisions(h, "trajectory-risk")
    assert [(d.trigger_seq, d.decision) for d in trace] == [
        (1, "NO_CHANGE"), (2, "LEVEL_RAISED"), (3, "LEVEL_RAISED"), (4, "NO_CHANGE")]
    assert all(d.outcome == "decided" and d.reason is None for d in trace)
    raised = trace[2]
    assert raised.factors["level"] == "high" and raised.factors["previous_level"] == "medium"
    assert "expected loss" in raised.reasoning and "proposing require approval" in raised.reasoning
    # The decision points at exactly the outputs it produced.
    finding = h.findings("risk.trajectory_high")[0]
    assert raised.finding_ids == (finding.finding_id,)
    assert raised.adjustments[0]["action"] == "REQUIRE_APPROVAL_FOR"
    assert raised.adjustments[0]["outcome"] == "accepted" and raised.adjustments[0]["signal_id"].startswith("sig_")
    assert trace[0].finding_ids == () and trace[0].adjustments == ()


class Emits:
    name, version, method = "emits", "1", "deterministic"
    subscription = Subscription(kinds=frozenset({"tool_use"}), needs_trajectory=False)

    async def handle(self, action, ctx):
        ctx.emit_finding(FindingDraft(rule_id="x.y", severity="low", summary="s", evidence_event_ids=(action.event_id,)))


def test_outputs_without_an_explicit_decision_are_still_traced(harness):
    h = harness([action(1)], [Emits])
    asyncio.run(h.run())
    [d] = decisions(h)
    assert d.decision == "OUTPUT_EMITTED" and d.finding_ids == (h.findings()[0].finding_id,)


class Broken:
    name, version, method = "broken", "1", "deterministic"
    subscription = Subscription(kinds=frozenset({"tool_use"}), needs_trajectory=False)

    async def handle(self, action, ctx):
        raise ValueError("Jan Kowalski PESEL 44051401359")       # message must never be persisted


def test_failures_record_a_fixed_reason_per_attempt_then_give_up(harness):
    h = harness([action(1)], [Broken], max_attempts=2)
    asyncio.run(h.run())
    trace = decisions(h, "broken")
    assert [(d.outcome, d.attempt, d.decision, d.reason) for d in trace] == [
        ("failed", 1, "PLUGIN_FAILED", "ValueError"), ("dead_lettered", 2, "PLUGIN_GAVE_UP", "ValueError")]
    assert "Kowalski" not in json.dumps([d.to_dict() for d in trace])
    assert "attempt 2 of 2 failed (ValueError); no further retries" == trace[1].reasoning


class Slow:
    name, version, method = "slow", "1", "deterministic"
    subscription = Subscription(kinds=frozenset({"tool_use"}), needs_trajectory=False)

    async def handle(self, action, ctx):
        await asyncio.sleep(1)


def test_timeouts_are_recorded_as_timeout(harness):
    h = harness([action(1)], [Slow], max_attempts=1, plugin_timeout_s=0.01)
    asyncio.run(h.run())
    [d] = decisions(h, "slow")
    assert (d.outcome, d.reason) == ("dead_lettered", "TIMEOUT")


def test_decision_ids_are_stable_across_redelivery(harness):
    ids = []
    for _ in range(2):
        h = harness([action(1, tool="read_application")], [TrajectoryRisk])
        h.reader.add_contract(CONTRACT)
        asyncio.run(h.run())
        ids.append([d.decision_id for d in decisions(h)])
    assert ids[0] == ids[1] and len(ids[0]) == 1
