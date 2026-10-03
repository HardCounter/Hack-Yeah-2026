"""Tests for trusted run writer and durable dispatch evidence."""

import asyncio
from functools import wraps
import pytest

from persistence import (
    ActionDetails,
    ActionStatus,
    ActionType,
    AuditorVerdict,
    EventStore,
    InterceptionMetadata,
    RunBinding,
    ConflictingRecordError,
)
from persistence.writer import BoundAuditWriter


def async_test(fn):
    @wraps(fn)
    def run(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))
    return run


def _sample_binding(run_id="run-1", **kwargs):
    defaults = {
        "run_id": run_id,
        "contract_id": "contract-kyc-v1",
        "session_id": "session-1",
        "principal_id": "operator-alice",
        "agent_id": "onboarding-agent",
        "policy_version": "policy-v2",
        "policy_hash": "a" * 64,
        "feed_version": "feed-v1",
    }
    defaults.update(kwargs)
    return RunBinding(**defaults)


@async_test
async def test_audit_failure_prevents_backend_dispatch(tmp_path, monkeypatch):
    store = EventStore(tmp_path / "writer.db")
    await store.initialize()
    writer = BoundAuditWriter(store)
    await writer.bind_run(_sample_binding("run-1"))

    details = ActionDetails(name="create_client", parameters={"application_id": "APP-0001"})
    metadata = InterceptionMetadata(AuditorVerdict.ALLOWED, policy_version="policy-v2")

    reached = []
    async def unavailable(*args, **kwargs):
        raise OSError("synthetic storage outage")

    monkeypatch.setattr(writer, "append", unavailable)
    with pytest.raises(OSError):
        await writer.append("run-1", "event-1", "action-1", details, metadata,
                            ActionStatus.PENDING, "PRE_DISPATCH")
        reached.append("backend")
    assert reached == []
    await store.close()


@async_test
async def test_unknown_run_is_rejected(tmp_path):
    store = EventStore(tmp_path / "unknown.db")
    await store.initialize()
    writer = BoundAuditWriter(store)

    details = ActionDetails(name="create_client")
    metadata = InterceptionMetadata(AuditorVerdict.ALLOWED)

    with pytest.raises(ValueError, match="Unknown or unbound run_id"):
        await writer.append("non-existent-run", "event-1", "action-1", details, metadata, ActionStatus.EXECUTED)

    await store.close()


@async_test
async def test_conflicting_rebind_is_rejected(tmp_path):
    store = EventStore(tmp_path / "rebind.db")
    await store.initialize()
    writer = BoundAuditWriter(store)

    binding1 = _sample_binding("run-1", principal_id="alice")
    await writer.bind_run(binding1)
    # Identical rebind is idempotent
    await writer.bind_run(binding1)

    # Conflicting rebind raises ConflictingRecordError
    binding2 = _sample_binding("run-1", principal_id="bob")
    with pytest.raises(ConflictingRecordError, match="conflicting binding"):
        await writer.bind_run(binding2)

    await store.close()


@async_test
async def test_forged_metadata_policy_version_is_rejected(tmp_path):
    store = EventStore(tmp_path / "forged.db")
    await store.initialize()
    writer = BoundAuditWriter(store)
    await writer.bind_run(_sample_binding("run-1", policy_version="policy-v2"))

    details = ActionDetails(name="read_application", parameters={"application_id": "APP-0001"})
    # Caller attempts to supply forged policy version
    forged_metadata = InterceptionMetadata(AuditorVerdict.ALLOWED, policy_version="policy-FORGED")

    with pytest.raises(ValueError, match="conflicts with run binding"):
        await writer.append("run-1", "event-1", "action-1", details, forged_metadata, ActionStatus.EXECUTED)

    await store.close()


@async_test
async def test_contiguous_action_indexing_and_replay(tmp_path):
    store = EventStore(tmp_path / "indices.db")
    await store.initialize()
    writer = BoundAuditWriter(store)
    await writer.bind_run(_sample_binding("run-1"))

    details1 = ActionDetails(name="read_application", parameters={"application_id": "APP-0001"})
    metadata1 = InterceptionMetadata(AuditorVerdict.ALLOWED)

    details2 = ActionDetails(name="screen_sanctions", parameters={"application_id": "APP-0001"})
    metadata2 = InterceptionMetadata(AuditorVerdict.ALLOWED)

    ev1 = await writer.append("run-1", "event-1", "action-1", details1, metadata1, ActionStatus.EXECUTED)
    ev2 = await writer.append("run-1", "event-2", "action-2", details2, metadata2, ActionStatus.EXECUTED)

    assert ev1.context.action_index == 0
    assert ev2.context.action_index == 1
    assert ev1.context.principal_id == "operator-alice"
    assert ev2.context.principal_id == "operator-alice"

    # Exact replay of event-1 returns stored envelope
    replayed = await writer.append("run-1", "event-1", "action-1", details1, metadata1, ActionStatus.EXECUTED)
    assert replayed.event_id == "event-1"
    assert replayed.context.action_index == 0

    # Conflicting replay raises ConflictingRecordError
    conflicting_details = ActionDetails(name="different_tool")
    with pytest.raises(ConflictingRecordError):
        await writer.append("run-1", "event-1", "action-1", conflicting_details, metadata1, ActionStatus.EXECUTED)

    await store.close()


@async_test
async def test_append_effect_records_executed_receipt(tmp_path):
    store = EventStore(tmp_path / "effect.db")
    await store.initialize()
    writer = BoundAuditWriter(store)
    await writer.bind_run(_sample_binding("run-1"))

    envelope = await writer.append_effect(
        run_id="run-1",
        event_id="eff-event-1",
        action_id="act-create-1",
        receipt_id="rcpt-001",
        application_id="APP-0001",
        client_id="CLI-0001",
    )

    assert envelope.status == ActionStatus.EXECUTED
    assert envelope.context.effect_receipt_id == "rcpt-001"
    assert envelope.context.action_index == 0
    assert envelope.action_details.name == "create_client"

    await store.close()
