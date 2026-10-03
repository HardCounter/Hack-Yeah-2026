"""Tests for PersistenceEngine lifecycle, consumer results, and worker fairness."""

from __future__ import annotations

import asyncio
from pathlib import Path
import pytest

from persistence.models import (
    ActionDetails,
    ActionEventEnvelope,
    ActionStatus,
    ActionType,
    AlertEvent,
    AuditorVerdict,
    ConsumerResult,
    InterceptionMetadata,
    Severity,
)
from persistence.worker import EngineState, PersistenceEngine
from functools import wraps


def async_test(fn):
    @wraps(fn)
    def run(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))
    return run


def create_sample_event(event_id: str, case_id: str = "case-1") -> ActionEventEnvelope:
    return ActionEventEnvelope(
        event_id=event_id,
        trace_id=f"trace-{event_id}",
        session_id=f"session-{event_id}",
        case_id=case_id,
        agent_id="test-agent",
        action_type=ActionType.TOOL_CALL,
        status=ActionStatus.EXECUTED,
        action_details=ActionDetails(name="test_tool"),
        interception_metadata=InterceptionMetadata(verdict=AuditorVerdict.ALLOWED),
    )


@async_test
async def test_callback_returns_derived_alert_during_shutdown_without_deadlock(tmp_path: Path):
    db_path = tmp_path / "lifecycle.db"
    engine = PersistenceEngine(db_path, poll_timeout=0.01)
    await engine.start()

    shutdown_begun = asyncio.Event()

    async def callback(ev: ActionEventEnvelope) -> ConsumerResult:
        # Signal that callback is running, then wait briefly to ensure shutdown has entered QUIESCING
        shutdown_begun.set()
        await asyncio.sleep(0.05)
        return ConsumerResult(
            alerts=(
                AlertEvent(
                    alert_id=f"derived-{ev.event_id}",
                    severity=Severity.HIGH,
                    rule="RULE-DERIVED",
                    agent_id=ev.agent_id,
                    session_id=ev.session_id,
                    action_taken="LOGGED",
                    evidence={"source_event": ev.event_id},
                ),
            )
        )

    await engine.register_consumer("analytics-1", callback)

    # Emit an event
    ev = create_sample_event("event-1")
    await engine.emit_action(ev)

    # Wait until callback starts executing
    await asyncio.wait_for(shutdown_begun.wait(), timeout=2.0)

    # Stop engine while callback is still running
    await engine.stop(timeout=5.0)

    assert engine.state == EngineState.STOPPED

    # Re-open store to verify derived alert was committed
    await engine.store.initialize()
    stats = await engine.store.get_stats()
    assert stats["total_alerts"] == 1
    pending = await engine.store.pending_deliveries()
    assert pending == 0
    await engine.store.close()


@async_test
async def test_quiescing_engine_rejects_recursive_emit_alert_immediately(tmp_path: Path):
    db_path = tmp_path / "recursive.db"
    engine = PersistenceEngine(db_path, poll_timeout=0.01)
    await engine.start()

    callback_started = asyncio.Event()
    rejection_observed = asyncio.Event()

    async def recursive_callback(ev: ActionEventEnvelope):
        callback_started.set()
        # Wait until engine is quiescing
        while engine.state != EngineState.QUIESCING:
            await asyncio.sleep(0.005)

        # Attempt to call engine.emit_alert directly during quiescing
        try:
            await engine.emit_alert(
                severity=Severity.HIGH,
                rule="RULE-ILLEGAL",
                agent_id=ev.agent_id,
                session_id=ev.session_id,
                action_taken="REJECTED",
            )
        except RuntimeError as exc:
            if "QUIESCING" in str(exc):
                rejection_observed.set()
        return None

    await engine.register_consumer("bad-consumer", recursive_callback)

    ev = create_sample_event("event-recursive")
    await engine.emit_action(ev)

    await asyncio.wait_for(callback_started.wait(), timeout=2.0)

    # Shutdown should complete cleanly and rejection must be observed without deadlocking
    await engine.stop(timeout=5.0)
    assert rejection_observed.is_set()
    assert engine.state == EngineState.STOPPED


@async_test
async def test_stalled_consumer_does_not_block_healthy_consumer(tmp_path: Path):
    db_path = tmp_path / "fairness.db"
    engine = PersistenceEngine(db_path, poll_timeout=0.01)
    await engine.start()

    stall_event = asyncio.Event()
    healthy_processed = asyncio.Event()

    async def stalled_consumer(ev: ActionEventEnvelope):
        await stall_event.wait()
        return None

    async def healthy_consumer(ev: ActionEventEnvelope):
        healthy_processed.set()
        return None

    await engine.register_consumer("slow", stalled_consumer)
    await engine.register_consumer("fast", healthy_consumer)

    # Emit an event
    ev1 = create_sample_event("event-fairness-1")
    await engine.emit_action(ev1)

    # Healthy consumer must finish processing its delivery even while "slow" is stalled
    await asyncio.wait_for(healthy_processed.wait(), timeout=2.0)

    # Critical emits must also continue to succeed while "slow" is stalled
    ev2 = create_sample_event("event-fairness-2")
    assert await engine.emit_action(ev2) is True

    # Release stalled consumer and stop
    stall_event.set()
    await engine.stop(timeout=5.0)


@async_test
async def test_poison_telemetry_batch_does_not_prevent_outbox_progress(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "poison.db"
    engine = PersistenceEngine(db_path, poll_timeout=0.01)
    await engine.start()

    delivered = asyncio.Event()

    async def consumer(ev: ActionEventEnvelope):
        if ev.event_id == "durable-1":
            delivered.set()
        return None

    await engine.register_consumer("durable-consumer", consumer)

    # Patch ingest buffer to yield a poison batch that raises an exception during _process_batch
    original_process = engine.worker._process_batch
    poison_attempted = asyncio.Event()

    async def failing_process(batch):
        poison_attempted.set()
        raise OSError("synthetic poison batch failure")

    monkeypatch.setattr(engine.worker, "_process_batch", failing_process)

    # Put a volatile event into the buffer
    engine.emit_action_nowait(create_sample_event("poison-volatile"))
    await asyncio.wait_for(poison_attempted.wait(), timeout=2.0)

    # Directly emit a durable event to outbox
    await engine.store.append_with_outbox([create_sample_event("durable-1")])

    # Durable delivery must still complete despite failing ingestion loop
    await asyncio.wait_for(delivered.wait(), timeout=2.0)

    # Restore process batch to allow clean exit
    monkeypatch.setattr(engine.worker, "_process_batch", original_process)
    await engine.worker.flush(timeout=3.0)
    await engine.stop(timeout=5.0)


@async_test
async def test_pause_and_resume_consumer_across_restart(tmp_path: Path):
    db_path = tmp_path / "pause_resume.db"

    # Session 1: Register and pause
    engine1 = PersistenceEngine(db_path, poll_timeout=0.01)
    await engine1.start()

    received1 = []

    async def cb1(ev):
        received1.append(ev.event_id)

    await engine1.register_consumer("worker-pause", cb1)
    await engine1.pause_consumer("worker-pause")

    # Emit event while paused
    await engine1.emit_action(create_sample_event("event-pause-1"))
    await asyncio.sleep(0.05)
    # Shouldn't be delivered while paused
    assert len(received1) == 0

    await engine1.stop()

    # Session 2: Reattach on restart; status remains paused until explicit resume
    engine2 = PersistenceEngine(db_path, poll_timeout=0.01)
    await engine2.start()

    received2 = []
    delivered_event = asyncio.Event()

    async def cb2(ev: ActionEventEnvelope):
        received2.append(ev.event_id)
        delivered_event.set()

    await engine2.register_consumer("worker-pause", cb2)

    await asyncio.sleep(0.05)
    assert len(received2) == 0

    # Explicitly resume
    await engine2.resume_consumer("worker-pause")

    # Now it should be delivered
    await asyncio.wait_for(delivered_event.wait(), timeout=2.0)
    assert "event-pause-1" in received2

    await engine2.stop()


@async_test
async def test_retire_consumer_transfers_pending_jobs_to_dlq(tmp_path: Path):
    db_path = tmp_path / "retire.db"
    engine = PersistenceEngine(db_path, poll_timeout=0.01)
    await engine.start()

    # Register and pause so jobs accumulate in outbox
    async def dummy_retire(ev):
        return None

    await engine.register_consumer("to-retire", dummy_retire)
    await engine.pause_consumer("to-retire")

    ev1 = create_sample_event("event-retire-1")
    ev2 = create_sample_event("event-retire-2")
    await engine.emit_action(ev1)
    await engine.emit_action(ev2)

    stats = await engine.get_stats()
    assert stats["pending_deliveries"] >= 2

    # Retire consumer
    await engine.retire_consumer("to-retire", intervention_id="INT-AUTH-42")

    stats = await engine.get_stats()
    # Dead letter queue must now contain the retired events
    assert stats["total_dlq_records"] >= 2

    # Check error message in DLQ
    dlqs = await engine.store.get_dlq_records()
    retired_dlqs = [item for item in dlqs if item.consumer_name == "to-retire"]
    assert len(retired_dlqs) == 2
    for item in retired_dlqs:
        assert item.error_message == "CONSUMER_RETIRED: INT-AUTH-42"

    await engine.stop()


@async_test
async def test_shutdown_timeout_leaves_recoverable_state(tmp_path: Path):
    db_path = tmp_path / "shutdown_timeout.db"
    engine = PersistenceEngine(db_path, poll_timeout=0.01)
    await engine.start()

    stall_forever = asyncio.Event()

    async def hanging_consumer(ev: ActionEventEnvelope):
        await stall_forever.wait()

    await engine.register_consumer("hang", hanging_consumer)
    await engine.emit_action(create_sample_event("event-hang"))

    # Stop with very short timeout should time out
    with pytest.raises((TimeoutError, asyncio.TimeoutError)):
        await engine.stop(timeout=0.02)

    # Worker must still be running and state remains QUIESCING
    assert engine.worker._running is True
    assert engine.state == EngineState.QUIESCING

    # Release hang and stop again
    stall_forever.set()
    await engine.stop(timeout=5.0)
    assert engine.state == EngineState.STOPPED
