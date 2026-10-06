"""Unit and integration tests for the local persistence component.

Tests coverage:
1. TestModels: Dataclass serialization, deserialization (JSON and dict), ISO 8601 UTC timestamps,
   enum conversions, default fields, schema version "2.0".
2. TestEventStore: SQLite WAL mode initialization, single/batch inserts, case/trace/session querying,
   alerts & audit action tables, query filtering/pagination, high-concurrency WAL read/write.
3. TestIngestBuffer: Non-blocking put_nowait, queue depth tracking, buffer overflow boundary,
   get_batch with timeout and batch size limits.
4. TestConsumerDispatcher: Fan-out pub/sub to multiple subscribers, filter predicates,
   dispatch_with_retry transient failure recovery, DLQ routing on retry exhaustion.
5. TestPersistenceWorkerAndEngine: Async context manager lifecycle, worker batch flushing,
   case timeline queries, aggregate stats, graceful shutdown with inflight draining.
6. TestPersistenceBenchmark: Concurrent durable appends of 500 events;
   reports measured latency and checks every persisted event ID.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from functools import wraps
import json
from pathlib import Path
import time
from typing import Any, Callable, Coroutine, Dict, List
import uuid

import pytest

from persistence.models import (
    ActionDetails,
    ActionEventEnvelope,
    ActionStatus,
    ActionType,
    AlertEvent,
    AuditActionRecord,
    AuditorDecision,
    AuditorVerdict,
    DeadLetterEnvelope,
    InterceptionMetadata,
    Severity,
    generate_utc_iso_timestamp,
    parse_utc_iso_timestamp,
)
from persistence.queue import ConsumerDispatcher, IngestBuffer
from persistence.store import EventStore
from persistence.worker import PersistenceEngine, PersistenceWorker


def async_test(coro_fn: Callable[..., Coroutine[Any, Any, Any]]) -> Callable[..., Any]:
    """Decorator to execute an async test within asyncio.run, forwarding pytest fixtures."""
    @wraps(coro_fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return asyncio.run(coro_fn(*args, **kwargs))
    return wrapper


def _create_sample_event(
    event_id: str | None = None,
    trace_id: str = "tr_test_default",
    session_id: str = "sess_test_default",
    case_id: str | None = "APP-0001",
    agent_id: str = "onboarding-agent",
    action_type: ActionType = ActionType.TOOL_CALL,
    status: ActionStatus = ActionStatus.EXECUTED,
    ts: str | None = None,
    tool_name: str = "create_client",
    total_latency_ms: float = 1.25,
) -> ActionEventEnvelope:
    """Helper to construct a fully populated ActionEventEnvelope."""
    return ActionEventEnvelope(
        event_id=event_id or str(uuid.uuid4()),
        schema_version="2.0",
        trace_id=trace_id,
        session_id=session_id,
        case_id=case_id,
        agent_id=agent_id,
        ts=ts or generate_utc_iso_timestamp(),
        action_type=action_type,
        source="gateway_test",
        status=status,
        action_details=ActionDetails(
            name=tool_name,
            parameters={"application_id": case_id or "APP-0001", "name": "Jan Kowalski"},
            result={"status": "created", "client_id": "CLI-999"},
            error=None,
            bytes_returned=256,
        ),
        interception_metadata=InterceptionMetadata(
            verdict=AuditorVerdict.ALLOWED,
            auditor_decisions=[
                AuditorDecision(
                    auditor_name="credential-shield",
                    verdict=AuditorVerdict.ALLOWED,
                    reason="clean input",
                    latency_ms=0.65,
                    rule="RULE-CRED-01",
                )
            ],
            policy_version="2.0.0",
            total_latency_ms=total_latency_ms,
            fault_injected=False,
            sanitized_fields=["pesel", "tax_id"],
        ),
    )


# ============================================================================
# 1. TestModels
# ============================================================================
class TestModels:
    """Serialization, deserialization, timestamp handling, and enum conversion tests."""

    def test_action_event_envelope_serialization_roundtrip(self) -> None:
        """Verify ActionEventEnvelope dict and JSON roundtrip with schema_version='2.0'."""
        original = _create_sample_event()
        assert original.schema_version == "2.0"

        # Dict roundtrip
        d = original.to_dict()
        assert d["schema_version"] == "2.0"
        assert d["action_type"] == "TOOL_CALL"
        assert d["status"] == "EXECUTED"
        assert d["interception_metadata"]["verdict"] == "ALLOWED"
        assert len(d["interception_metadata"]["auditor_decisions"]) == 1

        from_dict_obj = ActionEventEnvelope.from_dict(d)
        assert from_dict_obj.event_id == original.event_id
        assert from_dict_obj.schema_version == "2.0"
        assert from_dict_obj.trace_id == original.trace_id
        assert from_dict_obj.action_type == ActionType.TOOL_CALL
        assert from_dict_obj.status == ActionStatus.EXECUTED
        assert from_dict_obj.action_details.name == original.action_details.name
        assert from_dict_obj.action_details.parameters == original.action_details.parameters
        assert (
            from_dict_obj.interception_metadata.auditor_decisions[0].auditor_name
            == "credential-shield"
        )
        assert from_dict_obj.interception_metadata.sanitized_fields == ["pesel", "tax_id"]

        # JSON roundtrip
        json_str = original.to_json()
        assert isinstance(json_str, str)
        from_json_obj = ActionEventEnvelope.from_json(json_str)
        assert from_json_obj.event_id == original.event_id
        assert from_json_obj.to_dict() == original.to_dict()

    def test_alert_event_serialization_roundtrip(self) -> None:
        """Verify AlertEvent dict and JSON serialization roundtrip."""
        alert = AlertEvent(
            severity=Severity.HIGH,
            rule="ONB-P1",
            agent_id="onboarding-agent",
            session_id="sess_alert_01",
            action_taken="BLOCKED",
            case_id="APP-0002",
            evidence={"forbidden_tool": "send_email", "attempt_count": 3},
        )
        assert alert.severity == Severity.HIGH
        assert alert.alert_id is not None
        assert alert.ts.endswith("Z")

        # Dict roundtrip
        d = alert.to_dict()
        assert d["severity"] == "HIGH"
        assert d["rule"] == "ONB-P1"
        assert d["evidence"]["attempt_count"] == 3

        reconstructed = AlertEvent.from_dict(d)
        assert reconstructed.alert_id == alert.alert_id
        assert reconstructed.severity == Severity.HIGH
        assert reconstructed.evidence == alert.evidence

        # JSON roundtrip
        j = alert.to_json()
        from_json_alert = AlertEvent.from_json(j)
        assert from_json_alert.alert_id == alert.alert_id
        assert from_json_alert.severity == Severity.HIGH

    def test_audit_action_record_serialization_roundtrip(self) -> None:
        """Verify AuditActionRecord dict and JSON serialization roundtrip."""
        record = AuditActionRecord(
            run_id="run_101",
            session_id="sess_101",
            agent_id="agent_101",
            tool_name="create_client",
            target_id="CLI-500",
            side_effect_class="write",
            status="EXECUTED",
            details={"client_id": "CLI-500", "rows_written": 1},
        )
        assert record.action_id is not None
        assert record.ts.endswith("Z")

        # Dict roundtrip
        d = record.to_dict()
        assert d["tool_name"] == "create_client"
        assert d["status"] == "EXECUTED"
        from_dict_rec = AuditActionRecord.from_dict(d)
        assert from_dict_rec.action_id == record.action_id
        assert from_dict_rec.details == record.details

        # JSON roundtrip
        j = record.to_json()
        from_json_rec = AuditActionRecord.from_json(j)
        assert from_json_rec.action_id == record.action_id
        assert from_json_rec.tool_name == record.tool_name

    def test_dead_letter_envelope_serialization_roundtrip(self) -> None:
        """Verify DeadLetterEnvelope with nested ActionEventEnvelope and raw dict."""
        inner_event = _create_sample_event()
        dlq = DeadLetterEnvelope(
            event=inner_event,
            consumer_name="trajectory_risk_grader",
            error_message="Consumer timeout after 3 attempts",
            retry_count=3,
        )
        assert dlq.dlq_id is not None
        assert dlq.failed_at.endswith("Z")

        # Dict roundtrip
        d = dlq.to_dict()
        assert d["consumer_name"] == "trajectory_risk_grader"
        assert d["retry_count"] == 3
        assert isinstance(d["event"], dict)

        from_dict_dlq = DeadLetterEnvelope.from_dict(d)
        assert from_dict_dlq.dlq_id == dlq.dlq_id
        assert from_dict_dlq.consumer_name == dlq.consumer_name
        assert isinstance(from_dict_dlq.event, ActionEventEnvelope)
        assert from_dict_dlq.event.event_id == inner_event.event_id

        # JSON roundtrip
        j = dlq.to_json()
        from_json_dlq = DeadLetterEnvelope.from_json(j)
        assert from_json_dlq.dlq_id == dlq.dlq_id

        # Raw dict payload test
        raw_payload = {"raw_msg": "corrupted frame", "code": 500}
        dlq_raw = DeadLetterEnvelope(
            event=raw_payload,
            consumer_name="raw_worker",
            error_message="Parse error",
            retry_count=1,
        )
        d_raw = dlq_raw.to_dict()
        assert d_raw["event"] == raw_payload
        from_dict_raw = DeadLetterEnvelope.from_dict(d_raw)
        assert from_dict_raw.event == raw_payload

    def test_iso8601_utc_timestamps_and_parsing(self) -> None:
        """Verify timestamp generation adheres to ISO 8601 UTC ending in 'Z'."""
        ts = generate_utc_iso_timestamp()
        assert ts.endswith("Z")
        assert "T" in ts

        parsed_dt = parse_utc_iso_timestamp(ts)
        assert isinstance(parsed_dt, datetime)
        assert parsed_dt.tzinfo == timezone.utc

        # Test ISO string with explicit +00:00 offset
        standard_iso = "2026-10-03T18:00:00.000000+00:00"
        dt_from_offset = parse_utc_iso_timestamp(standard_iso)
        assert dt_from_offset.year == 2026
        assert dt_from_offset.tzinfo == timezone.utc

    def test_enum_conversions_and_default_fields(self) -> None:
        """Verify tolerant enum string parsing and default fields on envelope."""
        # Tolerant verdict parsing
        meta = InterceptionMetadata.from_dict({"verdict": "allow"})
        assert meta.verdict == AuditorVerdict.ALLOWED
        meta_block = InterceptionMetadata.from_dict({"verdict": "block"})
        assert meta_block.verdict == AuditorVerdict.BLOCKED
        meta_escalate = InterceptionMetadata.from_dict({"verdict": "REQUIRE_APPROVAL"})
        assert meta_escalate.verdict == AuditorVerdict.ESCALATED
        meta_redact = InterceptionMetadata.from_dict({"verdict": "redacted"})
        assert meta_redact.verdict == AuditorVerdict.REDACTED
        assert InterceptionMetadata.from_dict({"verdict": "WARN"}).verdict == AuditorVerdict.WARNED
        assert InterceptionMetadata.from_dict({"verdict": "APPROVE"}).verdict == AuditorVerdict.ESCALATED

        # Tolerant severity parsing
        alert = AlertEvent.from_dict(
            {
                "severity": "critical",
                "rule": "RULE-1",
                "agent_id": "a",
                "session_id": "s",
                "action_taken": "BLOCK",
            }
        )
        assert alert.severity == Severity.CRITICAL

        # Tolerant action status parsing
        envelope = ActionEventEnvelope.from_dict(
            {
                "trace_id": "tr_enum",
                "session_id": "sess_enum",
                "agent_id": "agent_enum",
                "action_type": "tool_call",
                "status": "executed",
                "action_details": {"name": "read_tool"},
                "interception_metadata": {"verdict": "ALLOWED"},
            }
        )
        assert envelope.action_type == ActionType.TOOL_CALL
        assert envelope.status == ActionStatus.EXECUTED
        assert envelope.schema_version == "2.0"
        assert envelope.event_id is not None
        assert envelope.ts.endswith("Z")


# ============================================================================
# 2. TestEventStore
# ============================================================================
class TestEventStore:
    """Unit and concurrency tests for SQLite WAL-mode EventStore."""

    @async_test
    async def test_store_initialization_and_close(self, tmp_path: Path) -> None:
        """Verify SQLite database initialization in WAL mode and clean connection close."""
        db_path = tmp_path / "test_init.db"
        store = EventStore(db_path)
        await store.initialize()

        assert store._conn is not None
        cursor = store._conn.execute("PRAGMA journal_mode")
        journal_mode = cursor.fetchone()[0].lower()
        assert journal_mode == "wal"

        await store.close()
        assert store._conn is None

    @async_test
    async def test_single_and_batch_event_insert(self, tmp_path: Path) -> None:
        """Verify single event insert, retrieval by ID, and batch insertion."""
        db_path = tmp_path / "test_inserts.db"
        store = EventStore(db_path)
        await store.initialize()

        # Single insert
        ev1 = _create_sample_event(trace_id="tr_s1", session_id="sess_s1", case_id="CASE-01")
        await store.insert_event(ev1)

        retrieved = await store.get_event(ev1.event_id)
        assert retrieved is not None
        assert retrieved.event_id == ev1.event_id
        assert retrieved.trace_id == "tr_s1"
        assert retrieved.action_details.name == "create_client"

        # Non-existent event returns None
        assert await store.get_event("non_existent_id") is None

        # Batch insert
        batch_events = [
            _create_sample_event(
                trace_id=f"tr_batch_{i}",
                session_id="sess_batch",
                case_id="CASE-BATCH",
            )
            for i in range(25)
        ]
        inserted_count = await store.insert_events_batch(batch_events)
        assert inserted_count == 25

        for ev in batch_events[:5]:
            item = await store.get_event(ev.event_id)
            assert item is not None
            assert item.event_id == ev.event_id

        await store.close()

    @async_test
    async def test_query_by_case_trace_session(self, tmp_path: Path) -> None:
        """Verify exact querying by case_id, trace_id, and session_id with ordering."""
        db_path = tmp_path / "test_queries.db"
        store = EventStore(db_path)
        await store.initialize()

        ev_c1_1 = _create_sample_event(
            trace_id="tr_c1_1", session_id="sess_alpha", case_id="CASE-A",
            ts="2026-10-03T12:00:01.000000Z"
        )
        ev_c1_2 = _create_sample_event(
            trace_id="tr_c1_2", session_id="sess_alpha", case_id="CASE-A",
            ts="2026-10-03T12:00:02.000000Z"
        )
        ev_c2_1 = _create_sample_event(
            trace_id="tr_c2_1", session_id="sess_beta", case_id="CASE-B",
            ts="2026-10-03T12:00:03.000000Z"
        )

        await store.insert_events_batch([ev_c1_1, ev_c1_2, ev_c2_1])

        # Query by case_id
        case_a_events = await store.get_events_by_case("CASE-A")
        assert len(case_a_events) == 2
        assert [e.event_id for e in case_a_events] == [ev_c1_1.event_id, ev_c1_2.event_id]

        case_b_events = await store.get_events_by_case("CASE-B")
        assert len(case_b_events) == 1
        assert case_b_events[0].event_id == ev_c2_1.event_id

        # Query by trace_id
        trace_events = await store.get_events_by_trace("tr_c1_1")
        assert len(trace_events) == 1
        assert trace_events[0].event_id == ev_c1_1.event_id

        # Query by session_id
        session_alpha_events = await store.get_events_by_session("sess_alpha")
        assert len(session_alpha_events) == 2

        await store.close()

    @async_test
    async def test_alerts_audit_actions_and_dlq(self, tmp_path: Path) -> None:
        """Verify inserting and querying security alerts, audit records, and DLQ entries."""
        db_path = tmp_path / "test_aux.db"
        store = EventStore(db_path)
        await store.initialize()

        # Alerts
        alert1 = AlertEvent(
            severity=Severity.MEDIUM,
            rule="ONB-P1",
            agent_id="agent-01",
            session_id="sess-01",
            action_taken="LOGGED",
            ts="2026-10-03T14:00:00.000000Z",
        )
        alert2 = AlertEvent(
            severity=Severity.HIGH,
            rule="ONB-P2",
            agent_id="agent-01",
            session_id="sess-01",
            action_taken="BLOCKED",
            ts="2026-10-03T14:05:00.000000Z",
        )
        await store.insert_alert(alert1)
        await store.insert_alert(alert2)

        alerts = await store.get_recent_alerts(limit=10)
        assert len(alerts) == 2
        # Ordered ts DESC
        assert alerts[0].alert_id == alert2.alert_id
        assert alerts[1].alert_id == alert1.alert_id

        # Audit Actions
        rec1 = AuditActionRecord(
            run_id="run_A",
            session_id="sess_A",
            agent_id="agent_A",
            tool_name="read_application",
            side_effect_class="read",
        )
        rec2 = AuditActionRecord(
            run_id="run_A",
            session_id="sess_B",
            agent_id="agent_A",
            tool_name="create_client",
            side_effect_class="write",
        )
        await store.insert_audit_action(rec1)
        await store.insert_audit_action(rec2)

        audit_run_a = await store.get_audit_actions(run_id="run_A")
        assert len(audit_run_a) == 2
        audit_sess_b = await store.get_audit_actions(session_id="sess_B")
        assert len(audit_sess_b) == 1
        assert audit_sess_b[0].action_id == rec2.action_id

        # DLQ
        dlq_item = DeadLetterEnvelope(
            event=_create_sample_event(),
            consumer_name="risk_scorer",
            error_message="Downstream service unavailable",
            retry_count=3,
        )
        await store.insert_dead_letter(dlq_item)

        dlq_records = await store.get_dlq_records(limit=10)
        assert len(dlq_records) == 1
        assert dlq_records[0].dlq_id == dlq_item.dlq_id
        assert dlq_records[0].consumer_name == "risk_scorer"

        await store.close()

    @async_test
    async def test_query_filtering_and_pagination(self, tmp_path: Path) -> None:
        """Verify query_events supports field filtering and pagination via limit and offset."""
        db_path = tmp_path / "test_filter_page.db"
        store = EventStore(db_path)
        await store.initialize()

        events: List[ActionEventEnvelope] = []
        for i in range(30):
            a_type = ActionType.TOOL_CALL if i < 15 else ActionType.LLM_INVOCATION
            a_status = ActionStatus.EXECUTED if (i % 2 == 0) else ActionStatus.BLOCKED
            ts_str = f"2026-10-03T10:00:{i:02d}.000000Z"
            events.append(
                _create_sample_event(
                    trace_id=f"tr_filter_{i}",
                    session_id="sess_filter",
                    action_type=a_type,
                    status=a_status,
                    ts=ts_str,
                )
            )

        await store.insert_events_batch(events)

        # Filter by action_type
        tool_events = await store.query_events(
            filter_dict={"action_type": ActionType.TOOL_CALL}, limit=50
        )
        assert len(tool_events) == 15
        assert all(e.action_type == ActionType.TOOL_CALL for e in tool_events)

        # Filter by status
        blocked_events = await store.query_events(
            filter_dict={"status": ActionStatus.BLOCKED}, limit=50
        )
        assert len(blocked_events) == 15
        assert all(e.status == ActionStatus.BLOCKED for e in blocked_events)

        # Pagination test: 3 pages of 10 items
        page1 = await store.query_events(limit=10, offset=0)
        page2 = await store.query_events(limit=10, offset=10)
        page3 = await store.query_events(limit=10, offset=20)

        assert len(page1) == 10
        assert len(page2) == 10
        assert len(page3) == 10

        ids_p1 = {e.event_id for e in page1}
        ids_p2 = {e.event_id for e in page2}
        ids_p3 = {e.event_id for e in page3}

        # Check total disjointness across pagination windows
        assert ids_p1.isdisjoint(ids_p2)
        assert ids_p2.isdisjoint(ids_p3)
        assert ids_p1.isdisjoint(ids_p3)

        await store.close()

    @async_test
    async def test_store_stats_aggregation(self, tmp_path: Path) -> None:
        """Verify get_stats aggregates counts, group-bys, and average latency."""
        db_path = tmp_path / "test_stats.db"
        store = EventStore(db_path)
        await store.initialize()

        e1 = _create_sample_event(
            action_type=ActionType.TOOL_CALL,
            status=ActionStatus.EXECUTED,
            total_latency_ms=10.0,
        )
        e2 = _create_sample_event(
            action_type=ActionType.TOOL_CALL,
            status=ActionStatus.BLOCKED,
            total_latency_ms=20.0,
        )
        e3 = _create_sample_event(
            action_type=ActionType.LLM_INVOCATION,
            status=ActionStatus.EXECUTED,
            total_latency_ms=30.0,
        )
        await store.insert_events_batch([e1, e2, e3])

        alert = AlertEvent(
            severity=Severity.CRITICAL,
            rule="ONB-P3",
            agent_id="a1",
            session_id="s1",
            action_taken="BLOCK",
        )
        await store.insert_alert(alert)

        audit = AuditActionRecord(
            run_id="r1",
            session_id="s1",
            agent_id="a1",
            tool_name="create_client",
        )
        await store.insert_audit_action(audit)

        stats = await store.get_stats()
        assert stats["total_events"] == 3
        assert stats["total_alerts"] == 1
        assert stats["total_audit_actions"] == 1
        assert stats["total_dlq_records"] == 0
        assert stats["events_by_type"]["TOOL_CALL"] == 2
        assert stats["events_by_type"]["LLM_INVOCATION"] == 1
        assert stats["events_by_status"]["EXECUTED"] == 2
        assert stats["events_by_status"]["BLOCKED"] == 1
        assert stats["alerts_by_severity"]["CRITICAL"] == 1
        assert stats["average_latency_ms"] == 20.0

        await store.close()

    @async_test
    async def test_high_concurrency_wal_read_write(self, tmp_path: Path) -> None:
        """Verify high concurrency concurrent reads and writes execute without lock contention."""
        db_path = tmp_path / "test_concurrency_wal.db"
        store = EventStore(db_path)
        await store.initialize()

        writers_count = 40
        readers_count = 40

        async def writer(idx: int) -> None:
            ev = _create_sample_event(
                trace_id=f"tr_conc_{idx}",
                session_id="sess_conc",
                case_id=f"CASE_{idx % 5}",
            )
            await store.insert_event(ev)

        async def reader(idx: int) -> None:
            # Short yield so writes can begin
            await asyncio.sleep(0.001)
            await store.get_events_by_case(f"CASE_{idx % 5}")

        tasks = [writer(i) for i in range(writers_count)] + [
            reader(i) for i in range(readers_count)
        ]
        await asyncio.gather(*tasks)

        stats = await store.get_stats()
        assert stats["total_events"] == writers_count

        await store.close()


# ============================================================================
# 3. TestIngestBuffer
# ============================================================================
class TestIngestBuffer:
    """Unit tests for bounded, non-blocking Queue 1 IngestBuffer."""

    def test_non_blocking_put_nowait_and_depth(self) -> None:
        """Verify non-blocking put_nowait updates qsize and empty status."""
        buf = IngestBuffer(maxsize=10)
        assert buf.empty() is True
        assert buf.qsize() == 0

        for i in range(5):
            success = buf.put_nowait(_create_sample_event(trace_id=f"tr_{i}"))
            assert success is True

        assert buf.empty() is False
        assert buf.qsize() == 5

    @async_test
    async def test_buffer_overflow_boundary(self) -> None:
        """Verify buffer drops on capacity exceeded and times out on async put."""
        buf = IngestBuffer(maxsize=5)

        for i in range(5):
            assert buf.put_nowait(_create_sample_event(trace_id=f"tr_{i}")) is True
        assert buf.qsize() == 5

        # 6th non-blocking call should fail cleanly without throwing
        overflow_event = _create_sample_event(trace_id="tr_overflow")
        success = buf.put_nowait(overflow_event)
        assert success is False
        assert buf.qsize() == 5

        # Async put with timeout should also return False on a full queue
        timed_success = await buf.put(overflow_event, timeout=0.02)
        assert timed_success is False
        assert buf.qsize() == 5

    @async_test
    async def test_get_batch_limits_and_timeout(self) -> None:
        """Verify get_batch respects max_items and does not block indefinitely when empty."""
        buf = IngestBuffer(maxsize=20)

        for i in range(7):
            buf.put_nowait(_create_sample_event(trace_id=f"tr_batch_{i}"))

        # Batch 1: max_items = 4
        b1 = await buf.get_batch(max_items=4, timeout=0.05)
        assert len(b1) == 4
        assert buf.qsize() == 3

        # Batch 2: max_items = 4 (only 3 remaining)
        b2 = await buf.get_batch(max_items=4, timeout=0.05)
        assert len(b2) == 3
        assert buf.qsize() == 0

        # Batch 3: queue is empty, timeout should cleanly return empty list
        start = time.perf_counter()
        b3 = await buf.get_batch(max_items=4, timeout=0.03)
        elapsed = time.perf_counter() - start

        assert len(b3) == 0
        assert elapsed >= 0.025


# ============================================================================
# 4. TestConsumerDispatcher
# ============================================================================
class TestConsumerDispatcher:
    """Unit tests for Queue 2 fan-out dispatcher, filtering, retry, and DLQ escalation."""

    @async_test
    async def test_fan_out_multiple_subscribers(self) -> None:
        """Verify event is fanned out to all subscribers, and unsubscribe deregisters properly."""
        dispatcher = ConsumerDispatcher()
        q1 = dispatcher.subscribe("consumer_1")
        q2 = dispatcher.subscribe("consumer_2")
        q3 = dispatcher.subscribe("consumer_3")

        assert set(dispatcher.subscribers) == {"consumer_1", "consumer_2", "consumer_3"}

        ev1 = _create_sample_event(trace_id="tr_fanout_1")
        await dispatcher.dispatch(ev1)

        # All 3 queues must have received the event
        assert q1.qsize() == 1
        assert q2.qsize() == 1
        assert q3.qsize() == 1
        assert q1.get_nowait().event_id == ev1.event_id
        assert q2.get_nowait().event_id == ev1.event_id
        assert q3.get_nowait().event_id == ev1.event_id

        # Unsubscribe consumer_2
        dispatcher.unsubscribe("consumer_2")
        assert "consumer_2" not in dispatcher.subscribers

        ev2 = _create_sample_event(trace_id="tr_fanout_2")
        await dispatcher.dispatch(ev2)

        assert q1.qsize() == 1
        assert q3.qsize() == 1
        assert q2.qsize() == 0  # consumer_2 unsubscribed, received nothing

    @async_test
    async def test_filter_function_support_on_subscriber(self) -> None:
        """Verify predicate filtering routes events selectively to matching subscribers."""
        dispatcher = ConsumerDispatcher()

        # Subscriber 1 only receives BLOCKED events
        q_blocked = dispatcher.subscribe(
            "blocked_only",
            filter_fn=lambda e: e.status == ActionStatus.BLOCKED,
        )
        # Subscriber 2 receives all events
        q_all = dispatcher.subscribe("all_events")

        ev_ok = _create_sample_event(status=ActionStatus.EXECUTED)
        ev_blocked = _create_sample_event(status=ActionStatus.BLOCKED)

        await dispatcher.dispatch(ev_ok)
        await dispatcher.dispatch(ev_blocked)

        assert q_all.qsize() == 2
        assert q_blocked.qsize() == 1
        assert q_blocked.get_nowait().event_id == ev_blocked.event_id

    @async_test
    async def test_dispatch_with_retry_transient_failure(self) -> None:
        """Verify transient consumer failure retries with exponential backoff and succeeds."""
        dispatcher = ConsumerDispatcher()
        attempts = 0

        async def flaky_consumer(event: ActionEventEnvelope) -> None:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise ConnectionResetError("Transient network failure")

        ev = _create_sample_event()
        success = await dispatcher.dispatch_with_retry(
            consumer_name="flaky_service",
            consumer_fn=flaky_consumer,
            event=ev,
            max_retries=3,
            base_delay=0.01,
        )

        assert success is True
        assert attempts == 3

    @async_test
    async def test_dispatch_with_retry_exhausted_to_dlq(self, tmp_path: Path) -> None:
        """Verify exhausted retries report failure and can be persisted to Dead Letter Queue."""
        dispatcher = ConsumerDispatcher()
        attempts = 0

        async def failing_consumer(event: ActionEventEnvelope) -> None:
            nonlocal attempts
            attempts += 1
            raise RuntimeError("Permanent downstream crash")

        ev = _create_sample_event()
        success = await dispatcher.dispatch_with_retry(
            consumer_name="permanent_failing_service",
            consumer_fn=failing_consumer,
            event=ev,
            max_retries=2,
            base_delay=0.01,
        )

        assert success is False
        assert attempts == 3  # Initial attempt + 2 retries

        # Route to DLQ in EventStore
        store = EventStore(tmp_path / "dlq_exhausted.db")
        await store.initialize()

        dlq_entry = DeadLetterEnvelope(
            event=ev,
            consumer_name="permanent_failing_service",
            error_message="Permanent downstream crash",
            retry_count=2,
        )
        await store.insert_dead_letter(dlq_entry)

        records = await store.get_dlq_records(limit=10)
        assert len(records) == 1
        assert records[0].consumer_name == "permanent_failing_service"
        assert records[0].retry_count == 2
        assert records[0].error_message == "CONSUMER_DELIVERY_FAILED"

        await store.close()


# ============================================================================
# 5. TestPersistenceWorkerAndEngine
# ============================================================================
class TestPersistenceWorkerAndEngine:
    """Integration tests for background worker loop, PersistenceEngine facade, and shutdown."""

    @async_test
    async def test_engine_context_manager_lifecycle(self, tmp_path: Path) -> None:
        """Verify PersistenceEngine starts worker on enter and cleans up on exit."""
        db_path = tmp_path / "engine_lifecycle.db"

        async with PersistenceEngine(db_path, poll_timeout=0.02) as engine:
            assert engine._started is True
            assert engine.worker._running is True
            assert engine.store._conn is not None

        assert engine._started is False
        assert engine.worker._running is False
        assert engine.store._conn is None

    @async_test
    async def test_emit_action_and_worker_batch_flush(self, tmp_path: Path) -> None:
        """Verify emitted actions, alerts, and audit actions flush to disk and dispatch to subscribers."""
        db_path = tmp_path / "engine_emit.db"

        async with PersistenceEngine(db_path, poll_timeout=0.01) as engine:
            # Subscribe to real-time feed
            live_feed = engine.subscribe_live_feed("test_live_feed")

            # 1. Emit action using kwargs
            await engine.emit_action(
                trace_id="tr_eng_1",
                session_id="sess_eng_1",
                case_id="CASE-ENG-1",
                agent_id="onboarding-agent",
                action_type="TOOL_CALL",
                status="EXECUTED",
                action_details={"name": "read_application", "parameters": {"id": "APP-1"}},
                interception_metadata={"verdict": "ALLOWED", "total_latency_ms": 1.1},
            )

            # 2. Emit action using ActionEventEnvelope instance
            ev2 = _create_sample_event(
                trace_id="tr_eng_2",
                session_id="sess_eng_1",
                case_id="CASE-ENG-1",
            )
            await engine.emit_action(ev2)

            # 3. Emit alert
            await engine.emit_alert(
                severity="HIGH",
                rule="ONB-P2",
                agent_id="onboarding-agent",
                session_id="sess_eng_1",
                action_taken="BLOCKED",
                case_id="CASE-ENG-1",
                evidence={"tamper": "unauthorized parameter change"},
            )

            # 4. Emit audit action
            await engine.emit_audit_action(
                run_id="run_eng_1",
                session_id="sess_eng_1",
                agent_id="onboarding-agent",
                tool_name="create_client",
                target_id="CLI-ENG-1",
                side_effect_class="write",
                status="EXECUTED",
            )

            # Flush worker
            await engine.worker.flush(timeout=3.0)

            # Check live feed received both action events
            assert live_feed.qsize() == 2

            # Check database persistence
            stored_events = await engine.store.get_events_by_case("CASE-ENG-1")
            assert len(stored_events) == 2

            stored_alerts = await engine.store.get_recent_alerts()
            assert len(stored_alerts) == 1
            assert stored_alerts[0].rule == "ONB-P2"

            stored_audits = await engine.store.get_audit_actions(run_id="run_eng_1")
            assert len(stored_audits) == 1
            assert stored_audits[0].tool_name == "create_client"

    @async_test
    async def test_case_timeline_and_stats(self, tmp_path: Path) -> None:
        """Verify timeline retrieval ordered chronologically and enriched stats telemetry."""
        db_path = tmp_path / "engine_timeline.db"

        async with PersistenceEngine(db_path, poll_timeout=0.01) as engine:
            ts1 = "2026-10-03T10:00:00.000000Z"
            ts2 = "2026-10-03T10:01:00.000000Z"
            ts3 = "2026-10-03T10:02:00.000000Z"

            ev1 = _create_sample_event(case_id="CASE-TL", ts=ts1, tool_name="read_application")
            ev2 = _create_sample_event(case_id="CASE-TL", ts=ts2, tool_name="verify_identity")
            ev3 = _create_sample_event(case_id="CASE-TL", ts=ts3, tool_name="create_client")

            # Out of order emit
            await engine.emit_action(ev2)
            await engine.emit_action(ev1)
            await engine.emit_action(ev3)

            await engine.worker.flush(timeout=3.0)

            timeline = await engine.get_case_timeline("CASE-TL")
            assert len(timeline) == 3
            # Must be chronologically sorted ASC by ts
            assert [e.action_details.name for e in timeline] == [
                "read_application",
                "verify_identity",
                "create_client",
            ]

            stats = await engine.get_stats()
            assert stats["total_events"] == 3
            assert "ingest_buffer_qsize" in stats
            assert stats["ingest_buffer_qsize"] == 0
            assert "active_subscribers" in stats

    @async_test
    async def test_graceful_shutdown_drains_inflight_events(self, tmp_path: Path) -> None:
        """Verify stop drains in-flight items in IngestBuffer before terminating."""
        db_path = tmp_path / "engine_shutdown.db"

        engine = PersistenceEngine(db_path, poll_timeout=0.1)
        await engine.start()

        # Enqueue 50 items non-blockingly without waiting for background worker to flush
        for i in range(50):
            ev = _create_sample_event(trace_id=f"tr_inflight_{i}", case_id="CASE-DRAIN")
            engine.emit_action_nowait(ev)

        assert engine.ingest_buffer.qsize() > 0

        # Terminate immediately - stop() must drain buffer into SQLite
        await engine.stop()

        # Open fresh connection to inspect persistent storage on disk
        fresh_store = EventStore(db_path)
        await fresh_store.initialize()

        stored_events = await fresh_store.get_events_by_case("CASE-DRAIN")
        assert len(stored_events) == 50
        await fresh_store.close()


# ============================================================================
# 6. Concurrency / Performance Smoke Benchmark
# ============================================================================
class TestPersistenceBenchmark:
    """Measured local throughput and durable append integrity; no hardware SLA."""

    @async_test
    async def test_concurrency_smoke_benchmark_500_events(self, tmp_path: Path) -> None:
        """Append 500 events concurrently and verify persisted identity/integrity."""
        db_path = tmp_path / "benchmark_500.db"
        total_events_count = 500

        async with PersistenceEngine(
            db_path,
            buffer_maxsize=1000,
            batch_size=100,
            poll_timeout=0.01,
        ) as engine:
            events = [
                _create_sample_event(
                    event_id=f"evt_bench_{i:04d}",
                    trace_id=f"tr_bench_{i:04d}",
                    session_id=f"sess_bench_{i % 10}",
                    case_id=f"CASE_BENCH_{i % 5}",
                )
                for i in range(total_events_count)
            ]

            # Measure enqueue latency
            t_start = time.perf_counter()
            enqueue_results = await asyncio.gather(
                *[engine.emit_action(ev) for ev in events]
            )
            t_elapsed = time.perf_counter() - t_start

            # Verify all enqueued successfully
            assert all(enqueue_results)

            # Measure durable append throughput without a hardware-dependent SLA.
            mean_latency_ms = (t_elapsed / total_events_count) * 1000.0
            print(
                f"\n[Benchmark] Committed {total_events_count} events in {t_elapsed*1000:.2f}ms "
                f"({mean_latency_ms:.4f}ms/call)"
            )

            # Durable outbox delivery performs additional FULL-synchronous SQLite
            # transactions. Its completion has no hardware SLA in this integrity
            # benchmark; deadline/recovery behavior has separate lifecycle tests.
            await engine.worker.flush(timeout=None)

            # Verify in SQLite database
            stats = await engine.get_stats()
            assert stats["total_events"] == total_events_count
            assert stats["pending_deliveries"] == 0
            assert not engine.worker._active_consumers

            # Query all events and verify 0 corruption / 0 dropped events
            persisted_events = await engine.store.query_events(limit=600)
            assert len(persisted_events) == total_events_count

            persisted_ids = {e.event_id for e in persisted_events}
            expected_ids = {e.event_id for e in events}
            assert persisted_ids == expected_ids
