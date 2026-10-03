from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from contracts import Budget, TaskContract
from consume_plane.adapters.persistence import (
    PersistenceEventSource,
    PersistenceFindingSink,
    PersistenceTrajectoryReader,
)
from consume_plane.model.outputs import Finding
from persistence import (
    ActionDetails,
    ActionEventEnvelope,
    ActionStatus,
    ActionType,
    AuditContext,
    AuditorVerdict,
    EventStore,
    InterceptionMetadata,
)
from persistence.governed import GovernedPersistence


def contract(session_id="sess-adapter"):
    return TaskContract(
        contract_id="ctr-adapter", session_id=session_id, run_id="run-adapter",
        principal_id="principal-test", agent_id="kyc-agent", role="reviewer",
        objective="Process the assigned application", case_id="APP-0001",
        target_ids=frozenset({"APP-0001"}), allowed_tools=frozenset({"read_application"}),
        postconditions=("ONB-P1",), budget=Budget(tokens=100, tool_calls=10),
        policy_version="policy-v1", policy_hash="a" * 64, feed_version="feed-v1",
    )


def event(seq: int, *, session_id="sess-adapter", event_type=ActionType.TOOL_CALL):
    if event_type == ActionType.SESSION:
        details = ActionDetails(name="session", wire_details={"phase": "ended", "contract_id": "ctr-adapter",
                                                                  "policy_version": "policy-v1"})
    else:
        details = ActionDetails(name="read_application", parameters={"app_id": "APP-0001"},
                                side_effect="read", transport="inproc")
    return ActionEventEnvelope(
        trace_id="trace-adapter", session_id=session_id, case_id="APP-0001", agent_id="kyc-agent",
        action_type=event_type, status=ActionStatus.EXECUTED, action_details=details,
        interception_metadata=InterceptionMetadata(verdict=AuditorVerdict.ALLOWED, policy_version="policy-v1"),
        event_id=f"event-adapter-{seq}",
        context=AuditContext(run_id="run-adapter", action_id=f"action-adapter-{seq}"),
        ts=datetime(2026, 10, 3, 12, seq, tzinfo=timezone.utc).isoformat(),
    )


async def make_governed(tmp_path):
    store = EventStore(tmp_path / "audit.sqlite")
    await store.initialize()
    governed = GovernedPersistence(store)
    await governed.persist_contract(contract())
    return store, governed


def test_persistence_event_source_orders_session_claims_and_reader_uses_seq(tmp_path):
    async def run():
        store, governed = await make_governed(tmp_path)
        source = PersistenceEventSource(governed, retry_backoff_s=0)
        await source.start()
        await governed.append(event(1))
        await governed.append(event(2))

        first = await source.receive(4, 0)
        assert [d.action.seq for d in first] == [0]
        await source.ack(first[0].delivery_id)
        second = await source.receive(4, 0)
        assert [d.action.seq for d in second] == [1]

        reader = PersistenceTrajectoryReader(governed)
        assert [a.seq for a in await reader.session("sess-adapter", up_to_seq=0)] == [0]
        assert (await reader.get("event-adapter-2")).seq == 1
        assert (await reader.contract("sess-adapter")).contract_id == "ctr-adapter"

        await source.ack(second[0].delivery_id)
        assert source.is_idle()
        await store.close()

    asyncio.run(run())


def test_nack_redelivery_and_finding_sink_are_idempotent_and_sanitized(tmp_path):
    async def run():
        store, governed = await make_governed(tmp_path)
        source = PersistenceEventSource(governed, retry_backoff_s=0)
        await source.start()
        await governed.append(event(1))
        first = (await source.receive(1, 0))[0]
        await source.nack(first.delivery_id, reason="retry", retry_after_s=0)
        replay = (await source.receive(1, 0))[0]
        assert replay.action.event_id == first.action.event_id
        assert replay.attempt == 2

        finding = Finding(
            finding_id="fnd_opaque1", rule_id="test.rule", severity="high", summary="contains untrusted name",
            evidence_event_ids=(replay.action.event_id,), plugin="test-plugin", plugin_version="1",
            method="deterministic", run_id="run-adapter", session_id="sess-adapter", agent_id="kyc-agent",
            case_id="APP-0001", trigger_event_id=replay.action.event_id, policy_version="policy-v1",
            created_at=datetime(2026, 10, 3, tzinfo=timezone.utc),
        )
        sink = PersistenceFindingSink(governed)
        await sink.write([finding])
        await sink.write([finding])
        stored = await sink.findings("sess-adapter")
        assert len(stored) == 1
        assert "summary" not in stored[0]

        await source.ack(replay.delivery_id)
        assert source.is_idle()
        await store.close()

    asyncio.run(run())
