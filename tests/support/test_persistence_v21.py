from __future__ import annotations

from dataclasses import replace
import asyncio
from functools import wraps

import pytest

from contracts import Budget, TaskContract
from contracts.wire import decode_event
from persistence.governed import GovernedPersistence
from persistence.models import (
    ActionDetails, ActionEventEnvelope, ActionStatus, ActionType, AuditContext,
    AuditorDecision, AuditorVerdict, InterceptionMetadata,
)
from persistence.store import EventStore
from persistence.adapters.consumer_v21 import to_consumer_v21


def async_test(fn):
    @wraps(fn)
    def run(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))
    return run


def _event(*, action_type=ActionType.TOOL_CALL, status=ActionStatus.EXECUTED,
           verdict=AuditorVerdict.ALLOWED, details=None, event_id="evt_1", session="sess_1"):
    return ActionEventEnvelope(
        trace_id="trace_1", session_id=session, agent_id="agent_1",
        action_type=action_type, status=status,
        action_details=details or ActionDetails(name="read_application", parameters={"app_id": "APP-0001"}),
        interception_metadata=InterceptionMetadata(
            verdict=verdict,
            auditor_decisions=[AuditorDecision("allowlist", verdict, rule="tool_allowed", latency_ms=0.1)],
            policy_version="policy_v1", total_latency_ms=1.2,
        ), event_id=event_id,
        context=AuditContext(action_id="act_1"),
    )


@async_test
async def test_content_and_events_share_transactional_byte_quota(tmp_path, monkeypatch):
    from persistence.settings import PersistenceSettings
    from persistence.store import AuditBackpressureError
    from persistence.maintenance import maintain
    store = EventStore(tmp_path / "content-quota.db", settings=PersistenceSettings(
        max_retained_logical_bytes=2048, max_export_bytes=1024,
    ))
    await store.initialize()
    governed = GovernedPersistence(store)
    try:
        first = await governed.put_content(b"a" * 1000, redacted=True)
        assert await governed.put_content(b"a" * 1000, redacted=True) == first
        from persistence import governed as governed_module
        from types import SimpleNamespace
        with monkeypatch.context() as disk:
            disk.setattr(governed_module.shutil, "disk_usage", lambda _: SimpleNamespace(free=0))
            assert await governed.put_content(b"a" * 1000, redacted=True) == first
            with pytest.raises(AuditBackpressureError, match="disk free space"):
                await governed.put_content(b"new", redacted=True)
        assert (await maintain(store)).logical_bytes == 1000
        with pytest.raises(AuditBackpressureError, match="body exceeds"):
            await governed.put_content(b"b" * 1025, redacted=True)
        await governed.put_content(b"b" * 1000, redacted=True)
        with pytest.raises(AuditBackpressureError, match="quota"):
            await governed.put_content(b"c" * 100, redacted=True)
        with pytest.raises(AuditBackpressureError, match="quota"):
            await store.append_with_outbox([_event()])
        with pytest.raises(AuditBackpressureError, match="quota"):
            await store.insert_events_batch([_event()])
        assert await store.get_event("evt_1") is None
        assert store._sync_logical_bytes() == 2000
    finally:
        await store.close()


@async_test
async def test_exact_retries_do_not_charge_event_bytes_twice(tmp_path):
    from persistence.settings import PersistenceSettings
    from persistence.store import AuditBackpressureError
    store = EventStore(tmp_path / "retry-quota.db")
    await store.initialize()
    try:
        event = _event()
        await store.append_with_outbox([event])
        used = store._sync_logical_bytes()
        store.settings = PersistenceSettings(max_retained_logical_bytes=max(1024, used))
        assert await store.append_with_outbox([event]) == 0
        assert await store.insert_events_batch([event]) == 0
        with pytest.raises(AuditBackpressureError, match="quota"):
            await store.append_with_outbox([replace(event, event_id="overflow")])
        assert await store.get_event("overflow") is None
        assert store._sync_logical_bytes() == used
    finally:
        await store.close()


@async_test
async def test_v21_adapter_maps_tool_decision_status_and_persisted_seq(tmp_path):
    store = EventStore(tmp_path / "v21.db")
    await store.initialize()
    first = _event(event_id="evt_1")
    second = _event(event_id="evt_2", status=ActionStatus.BLOCKED,
                    verdict=AuditorVerdict.BLOCKED)
    await store.insert_events_batch([first, second])
    wire = to_consumer_v21(await store.get_event("evt_1"))
    blocked = to_consumer_v21(await store.get_event("evt_2"))
    assert (wire["schema_version"], wire["seq"], wire["action_type"], wire["status"]) == ("2.1", 0, "tool_call", "completed")
    assert wire["action_id"] == "act_1"
    assert wire["interception_metadata"]["final_decision"] == "ALLOW"
    assert wire["interception_metadata"]["auditor_decisions"][0] == {
        "auditor": "allowlist", "decision": "ALLOW", "rule_id": "tool_allowed", "latency_ms": 0.1,
    }
    assert blocked["seq"] == 1 and blocked["status"] == "blocked"
    assert decode_event(wire).seq == 0
    await store.close()


@async_test
async def test_v21_maps_redaction_approval_and_alert_verdicts(tmp_path):
    store = EventStore(tmp_path / "maps.db")
    await store.initialize()
    samples = [
        _event(event_id="redacted", status=ActionStatus.REDACTED, verdict=AuditorVerdict.REDACTED),
        _event(event_id="approval", status=ActionStatus.ESCALATED, verdict=AuditorVerdict.ESCALATED),
        _event(event_id="alert", status=ActionStatus.EXECUTED, verdict=AuditorVerdict.WARNED),
    ]
    await store.insert_events_batch(samples)
    mapped = [to_consumer_v21(await store.get_event(ev.event_id)) for ev in samples]
    assert [ev["status"] for ev in mapped] == ["redacted", "pending_approval", "completed"]
    assert [ev["interception_metadata"]["final_decision"] for ev in mapped] == ["REDACT", "REQUIRE_APPROVAL", "ALERT"]
    await store.close()


@async_test
async def test_v21_maps_lifecycle_control_and_contentref(tmp_path):
    store = EventStore(tmp_path / "lifecycle.db")
    await store.initialize()
    governed = GovernedPersistence(store)
    content = await governed.put_content(b"already redacted", redacted=True)
    session = _event(
        event_id="session_evt", action_type=ActionType.SESSION,
        details=ActionDetails(name="session", wire_details={
            "phase": "ended", "contract_id": "contract_1", "policy_version": "policy_v1", "end_reason": "completed",
        }),
    )
    tool = _event(event_id="tool_evt", details=ActionDetails(name="screen_sanctions",
        side_effect="read", transport="inproc", result=content))
    control = _event(event_id="control_evt", action_type=ActionType.CONTROL,
        details=ActionDetails(name="control", wire_details={
            "change": "adjustment_applied", "signal_id": "sig_1", "policy_version": "policy_v1",
        }))
    await store.insert_events_batch([session, tool, control])
    session_wire = to_consumer_v21(await store.get_event("session_evt"))
    tool_wire = to_consumer_v21(await store.get_event("tool_evt"))
    control_wire = to_consumer_v21(await store.get_event("control_evt"))
    assert session_wire["action_details"]["phase"] == "ended"
    assert "interception_metadata" not in session_wire
    assert tool_wire["action_details"]["result"] == content
    assert await governed.content(content) == b"already redacted"
    assert control_wire["action_type"] == "control"
    for event in (session_wire, tool_wire, control_wire):
        decode_event(event)
    await store.close()


@async_test
async def test_contract_bound_append_assigns_order_and_roundtrips(tmp_path):
    store = EventStore(tmp_path / "contract.db")
    await store.initialize()
    governed = GovernedPersistence(store)
    contract = TaskContract(
        contract_id="contract_1", session_id="sess_1", agent_id="agent_1",
        role="KYC analyst", objective="Process APP-0001", target_ids=frozenset({"APP-0001"}),
        allowed_tools=frozenset({"read_application"}), postconditions=("ONB-P1",),
        budget=Budget(tokens=1000, tool_calls=10), policy_version="policy_v1",
        run_id="run_1", principal_id="principal_1", case_id="APP-0001",
        policy_hash="a" * 64, feed_version="feed_v1",
    )
    await governed.persist_contract(contract)
    assert (await governed.contract("sess_1")).to_dict() == contract.to_dict()
    candidate = replace(_event(event_id="bound_evt"), context=AuditContext(run_id="run_1", action_id="act_1"))
    persisted = await governed.append(candidate)
    assert persisted.seq == 0
    assert persisted.context.action_index == 0
    assert persisted.context.principal_id == "principal_1"
    assert decode_event(await governed.wire_event("bound_evt")).run_id == "run_1"
    # Retrying identical event IDs is idempotent and does not consume order.
    retried = await governed.append(candidate)
    assert retried.seq == 0
    await store.close()


@async_test
async def test_intent_has_no_consumer_seq_and_caller_cannot_supply_seq(tmp_path):
    store = EventStore(tmp_path / "intent.db")
    await store.initialize()
    governed = GovernedPersistence(store)
    contract = TaskContract(
        contract_id="contract_1", session_id="sess_1", agent_id="agent_1",
        role="analyst", objective="Process APP-1", target_ids=frozenset({"APP-1"}),
        allowed_tools=frozenset({"create_client"}), postconditions=(), budget=Budget(),
        policy_version="policy_v1", run_id="run_1", principal_id="principal_1",
        case_id="APP-1", policy_hash="b" * 64, feed_version="feed_v1",
    )
    await governed.persist_contract(contract)
    pending = replace(_event(event_id="intent_evt", status=ActionStatus.PENDING), context=AuditContext(run_id="run_1", action_id="act_1"))
    await governed.intent(pending)
    final = replace(_event(event_id="result_evt", status=ActionStatus.BLOCKED, verdict=AuditorVerdict.BLOCKED), context=AuditContext(run_id="run_1", action_id="act_1"))
    saved = await governed.append(final)
    assert saved.seq == 0
    with pytest.raises(ValueError, match="Layer 2 assigns seq"):
        await governed.append(replace(final, event_id="bad_seq", seq=3))
    await store.close()


def test_unknown_legacy_status_and_error_verdict_fail_loudly():
    with pytest.raises(ValueError, match="Unsupported persistence status"):
        to_consumer_v21(replace(_event(status=ActionStatus.SKIPPED), seq=0))
    with pytest.raises(ValueError, match="Unsupported persistence verdict"):
        to_consumer_v21(replace(_event(verdict=AuditorVerdict.ERROR), seq=0))


def test_v21_maps_all_supported_action_types_and_rejects_unknown():
    values = [
        (ActionType.LLM_INVOCATION, ActionDetails(name="llama3.2", wire_details={
            "model": "llama3.2", "provider": "ollama", "messages": [], "completion": None,
            "tool_calls_requested": [], "stop_reason": "end_turn"}), "llm_call"),
        (ActionType.MCP_TOOL, ActionDetails(name="read_application"), "mcp_tool"),
        (ActionType.EGRESS_HTTP, ActionDetails(name="egress", wire_details={
            "method": "GET", "host": "registry.example", "path": "/v1/check", "status_code": 200,
        }), "egress_http"),
        (ActionType.APPROVAL, ActionDetails(name="approval", wire_details={
            "target_event_id": "evt_target", "decision": "approved", "approver_role": "reviewer",
            "delay_ms": 1.0,
        }), "approval"),
    ]
    for index, (action_type, details, expected) in enumerate(values):
        event = replace(_event(action_type=action_type, details=details, event_id=f"evt_{index}"), seq=index)
        wire = to_consumer_v21(event)
        assert wire["action_type"] == expected
        decode_event(wire)
    with pytest.raises(ValueError, match="Unsupported persistence action type"):
        to_consumer_v21(replace(_event(action_type=ActionType.SYSTEM), seq=0))


@async_test
async def test_per_session_seq_survives_store_restart(tmp_path):
    db = tmp_path / "restart-seq.db"
    store = EventStore(db)
    await store.initialize()
    await store.insert_event(_event(event_id="before_restart"))
    assert (await store.get_event("before_restart")).seq == 0
    await store.close()
    restarted = EventStore(db)
    await restarted.initialize()
    await restarted.insert_event(_event(event_id="after_restart"))
    assert (await restarted.get_event("after_restart")).seq == 1
    await restarted.close()
