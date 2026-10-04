"""Failure-boundary tests using only synthetic data and disposable SQLite files."""

import asyncio
from dataclasses import replace
from functools import wraps
import json
import subprocess
import sys
import threading

import pytest

from persistence import (
    ActionDetails, ActionEventEnvelope, ActionStatus, ActionType, AlertEvent,
    AuditActionRecord, AuditContext, AuditorDecision, AuditorVerdict,
    DeadLetterEnvelope, EventStore, InterceptionMetadata, PersistenceEngine,
    Severity, AuditBackpressureError, ConflictingRecordError,
)


def async_test(fn):
    @wraps(fn)
    def run(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))
    return run


def event(event_id="event-1", **kwargs):
    return ActionEventEnvelope(
        event_id=event_id, trace_id="trace-1", session_id="session-1", agent_id="agent-1",
        action_type=ActionType.TOOL_CALL, status=ActionStatus.BLOCKED,
        action_details=ActionDetails(name="create_client", parameters={"application_id": "APP-0001"}),
        interception_metadata=InterceptionMetadata(verdict=AuditorVerdict.BLOCKED, policy_version="policy-1"),
        **kwargs,
    )


@pytest.mark.parametrize("payload", [{}, {"verdict": "UNKNOWN"}, {"verdict": None}])
def test_malformed_or_missing_verdict_never_becomes_allowed(payload):
    with pytest.raises(ValueError):
        InterceptionMetadata.from_dict(payload)


@async_test
async def test_immutable_ids_exact_retry_and_atomic_conflict_rollback(tmp_path):
    store = EventStore(tmp_path / "audit.db")
    await store.initialize()
    original = event()
    await store.insert_event(original)
    assert await store.insert_events_batch([original]) == 0
    conflict = replace(original, status=ActionStatus.EXECUTED)
    with pytest.raises(ConflictingRecordError):
        await store.insert_events_batch([event("new-event"), conflict])
    assert await store.get_event("new-event") is None
    assert (await store.get_event(original.event_id)).status == ActionStatus.BLOCKED
    await store.close()


@async_test
async def test_all_persistence_surfaces_omit_raw_payloads(tmp_path):
    marker = "synthetic-private-content@example.test"
    ev = event()
    ev.action_details.parameters.update(name=marker, password=marker, nested={"secret": marker})
    ev.action_details.result = {"client_id": "CLI-1", "raw": marker}
    ev.action_details.error = marker
    ev.interception_metadata.auditor_decisions = [
        AuditorDecision("guard", AuditorVerdict.BLOCKED, reason=marker, modifications={"raw": marker})]
    async with PersistenceEngine(tmp_path / "privacy.db") as engine:
        feed = engine.subscribe_live_feed()
        await engine.emit_action(ev)
        await engine.emit_alert(AlertEvent(Severity.HIGH, "RULE-1", "agent-1", "session-1", "BLOCK", evidence={"raw": marker}))
        await engine.emit_audit_action(AuditActionRecord("run-1", "session-1", "agent-1", "create_client", details={"raw": marker}))
        await engine.store.insert_dead_letter(DeadLetterEnvelope(ev, "consumer-1", marker))
        await engine.worker.flush()
        raw = [dict(row) for table in ("events", "alerts", "audit_actions", "dead_letter_queue")
               for row in engine.store._conn.execute(f"SELECT * FROM {table}")]
        assert marker not in json.dumps(raw)
        live = await feed.get()
        assert marker not in live.to_json()
        assert live.action_details.parameters == {"application_id": "APP-0001"}
        assert live.interception_metadata.policy_version == "policy-1"
        # Sanitization must not mutate the caller's original evidence.
        assert ev.action_details.error == marker


@async_test
async def test_known_sensitive_ids_rejected_without_persistence(tmp_path):
    async with PersistenceEngine(tmp_path / "privacy.db") as engine:
        ev = event()
        ev.action_details.parameters["client_id"] = "123-45-6789"
        with pytest.raises(ValueError):
            await engine.emit_action(ev)
        assert (await engine.get_stats())["total_events"] == 0


@async_test
async def test_context_preserved_and_missing_binding_not_fabricated(tmp_path):
    ctx = AuditContext(contract_id="contract-1", run_id="run-1", action_id="action-1",
        principal_id="principal-1", action_index=0, policy_hash="a" * 64,
        feed_version="feed-1", approval_id="approval-1", effect_receipt_id="receipt-1",
        verification_status="VERIFICATION_INCOMPLETE", reserved_usage={"reserved_tokens": 25})
    async with PersistenceEngine(tmp_path / "audit.db") as engine:
        await engine.emit_action(event(context=ctx))
        stored = await engine.store.get_event("event-1")
        assert stored.context == ctx
        assert InterceptionMetadata(AuditorVerdict.ERROR).policy_version is None
        assert event().context.contract_id is None


@async_test
async def test_durable_emit_survives_abrupt_process_exit(tmp_path):
    path = tmp_path / "crash.db"
    script = """
import asyncio, os, sys
from persistence import *
async def main():
    engine = PersistenceEngine(sys.argv[1])
    await engine.start()
    await engine.worker.stop()
    ev = ActionEventEnvelope(trace_id='trace-1', session_id='session-1', agent_id='agent-1',
        action_type=ActionType.TOOL_CALL, status=ActionStatus.BLOCKED,
        action_details=ActionDetails(name='create_client'),
        interception_metadata=InterceptionMetadata(AuditorVerdict.BLOCKED), event_id='crash-event')
    await engine.emit_action(ev)
    os._exit(0)
asyncio.run(main())
"""
    result = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", script, str(path)],
                                    capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr.decode()
    store = EventStore(path)
    await store.initialize()
    assert await store.get_event("crash-event") is not None
    # The background live-feed job also survives with the evidence transaction.
    assert await store.pending_deliveries() == 1
    await store.close()
    async with PersistenceEngine(path, poll_timeout=0.005) as engine:
        await engine.worker.flush()
        assert await engine.store.pending_deliveries() == 0


@async_test
async def test_outbox_restart_resume_and_idempotent_emit(tmp_path):
    path = tmp_path / "restart.db"
    store = EventStore(path)
    await store.initialize()
    await store.register_consumer("metrics")
    original = event()
    await store.append_with_outbox([original])
    await store.append_with_outbox([original])
    assert await store.pending_deliveries() == 1
    await store.close()
    received = []
    async def consumer(ev):
        received.append(ev.event_id)
    async with PersistenceEngine(path, poll_timeout=0.005) as engine:
        await engine.register_consumer("metrics", consumer)
        await engine.worker.flush()
        assert received == ["event-1"]
        stored = await engine.store.get_event("event-1")
        await engine.emit_action(stored)
        await engine.worker.flush()
        assert received == ["event-1"]


@async_test
async def test_consumer_retries_exhaust_to_durable_dlq_without_secret_logs(tmp_path, caplog):
    marker = "synthetic-private-exception@example.test"
    calls = 0
    async def fails(ev):
        nonlocal calls
        calls += 1
        raise RuntimeError(marker)
    async with PersistenceEngine(tmp_path / "dlq.db", poll_timeout=0.005) as engine:
        await engine.register_consumer("fails", fails)
        await engine.emit_action(event())
        await engine.worker.flush()
        records = await engine.store.get_dlq_records()
        assert calls == 4
        assert len(records) == 1
        assert records[0].consumer_name == "fails"
        assert records[0].retry_count == 3
        assert marker not in records[0].to_json()
        assert marker not in caplog.text
        assert await engine.store.pending_deliveries() == 0


@async_test
async def test_outbox_capacity_atomic_across_two_connections(tmp_path):
    path = tmp_path / "capacity.db"
    one = EventStore(path, outbox_maxsize=1)
    two = EventStore(path, outbox_maxsize=1)
    await one.initialize()
    await two.initialize()
    await one.register_consumer("offline")
    results = await asyncio.gather(one.append_with_outbox([event("one")]),
                                   two.append_with_outbox([event("two")]), return_exceptions=True)
    assert sum(isinstance(r, AuditBackpressureError) for r in results) == 1
    assert (await one.get_stats())["total_events"] == 1
    assert await one.pending_deliveries() == 1
    await one.close()
    await two.close()


@async_test
async def test_audit_failure_prevents_durable_acceptance(tmp_path, monkeypatch):
    async with PersistenceEngine(tmp_path / "failure.db") as engine:
        async def unavailable(_):
            raise OSError("synthetic audit failure")
        with monkeypatch.context() as patch:
            patch.setattr(engine.store, "append_with_outbox", unavailable)
            with pytest.raises(OSError):
                await engine.emit_action(event())
        assert await engine.store.get_event("event-1") is None


@async_test
async def test_volatile_batch_retained_on_transient_write_failure(tmp_path, monkeypatch):
    async with PersistenceEngine(tmp_path / "retry.db", poll_timeout=0.005) as engine:
        original = engine.store.append_with_outbox
        calls = 0
        async def flaky(batch):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("synthetic failure")
            return await original(batch)
        monkeypatch.setattr(engine.store, "append_with_outbox", flaky)
        assert engine.emit_action_nowait(event())
        await engine.worker.flush()
        assert calls == 2
        assert await engine.store.get_event("event-1") is not None


@async_test
async def test_flush_waits_for_commit_and_shutdown_timeout_is_explicit(tmp_path, monkeypatch):
    engine = PersistenceEngine(tmp_path / "flush.db", poll_timeout=0.005)
    await engine.start()
    begun, release = asyncio.Event(), asyncio.Event()
    original = engine.store.append_with_outbox
    async def stalled(batch):
        begun.set()
        await release.wait()
        return await original(batch)
    monkeypatch.setattr(engine.store, "append_with_outbox", stalled)
    engine.emit_action_nowait(event())
    await asyncio.wait_for(begun.wait(), 1)
    assert engine.ingest_buffer.empty()
    with pytest.raises(TimeoutError):
        await engine.worker.flush(timeout=0.02)
    with pytest.raises(TimeoutError):
        await engine.stop(timeout=0.02)
    assert engine.worker._running
    release.set()
    await engine.stop()
    store = EventStore(tmp_path / "flush.db")
    await store.initialize()
    assert await store.get_event("event-1") is not None
    await store.close()


@async_test
async def test_thread_cancellation_does_not_release_connection_lock_early(tmp_path, monkeypatch):
    store = EventStore(tmp_path / "cancel.db")
    await store.initialize()
    begun, release = threading.Event(), threading.Event()
    original = store._sync_insert_events_batch
    def stalled(events):
        begun.set()
        assert release.wait(2)
        return original(events)
    monkeypatch.setattr(store, "_sync_insert_events_batch", stalled)
    write = asyncio.create_task(store.insert_event(event()))
    assert await asyncio.to_thread(begun.wait, 1)
    write.cancel()
    close = asyncio.create_task(store.close())
    await asyncio.sleep(0.02)
    assert not close.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await write
    await close
    await store.initialize()
    assert await store.get_event("event-1") is not None
    await store.close()


@async_test
async def test_retention_keeps_pending_evidence_and_replay_is_bounded(tmp_path):
    store = EventStore(tmp_path / "retention.db", outbox_maxsize=1)
    await store.initialize()
    await store.register_consumer("offline")
    await store.append_with_outbox([event(ts="2026-01-01T00:00:00Z")])
    assert await store.prune_before("2026-02-01T00:00:00Z") == 0
    with pytest.raises(AuditBackpressureError):
        await store.register_consumer("replay", replay=True)
    delivery = await store.claim_delivery(["offline"])
    ev, name, _, lease = delivery
    await store.finish_delivery(ev, name, lease)
    assert await store.prune_before("2026-02-01T00:00:00Z") == 1
    await store.close()


@async_test
async def test_equal_timestamps_have_stable_order_and_offsets_normalize(tmp_path):
    store = EventStore(tmp_path / "order.db")
    await store.initialize()
    first = event("first", ts="2026-10-03T12:00:00+02:00")
    second = event("second", ts="2026-10-03T10:00:00Z")
    await store.insert_events_batch([first, second])
    assert [ev.event_id for ev in await store.query_events()] == ["first", "second"]
    assert (await store.get_event("first")).ts == "2026-10-03T10:00:00.000000Z"
    with pytest.raises(ValueError):
        await store.query_events(limit=-1)
    with pytest.raises(ValueError):
        await store.query_events({"unsupported": "anything"})
    await store.close()


@async_test
async def test_live_feed_overflow_visible_and_subscribers_get_owned_snapshots(tmp_path):
    async with PersistenceEngine(tmp_path / "feed.db", poll_timeout=0.005) as engine:
        first = engine.subscribe_live_feed("first", maxsize=1)
        second = engine.subscribe_live_feed("second", maxsize=2)
        await engine.emit_action(event("one"))
        await engine.emit_action(event("two"))
        await engine.worker.flush()
        assert (await engine.get_stats())["dropped_live_deliveries"] == 1
        a, b = first.get_nowait(), second.get_nowait()
        a.action_details.name = "changed"
        assert b.action_details.name == "create_client"
        assert (await engine.store.get_event("one")).action_details.name == "create_client"
        assert len(await engine.store.query_events()) == 2


@async_test
async def test_emit_outside_lifecycle_and_missing_decision_rejected(tmp_path):
    engine = PersistenceEngine(tmp_path / "lifecycle.db")
    with pytest.raises(RuntimeError):
        await engine.emit_action(event())
    await engine.start()
    with pytest.raises(ValueError):
        await engine.emit_action(trace_id="trace-1", session_id="session-1", agent_id="agent-1")
    await engine.stop()
    with pytest.raises(RuntimeError):
        engine.emit_action_nowait(event())


@pytest.mark.parametrize("path", [":memory:", ""])
def test_critical_engine_rejects_ephemeral_database(path):
    with pytest.raises(ValueError):
        PersistenceEngine(path)


@async_test
async def test_low_level_outbox_also_rejects_memory_only_acceptance():
    store = EventStore()
    await store.initialize()
    with pytest.raises(ValueError):
        await store.append_with_outbox([event()])
    await store.close()


@async_test
async def test_run_order_unique_even_when_events_arrive_out_of_order(tmp_path):
    async with PersistenceEngine(tmp_path / "order.db") as engine:
        one = event("one", context=AuditContext(run_id="run-1", action_index=1))
        zero = event("zero", context=AuditContext(run_id="run-1", action_index=0))
        await engine.emit_action(one)
        await engine.emit_action(zero)
        assert [ev.event_id for ev in await engine.store.get_run_events("run-1")] == ["zero", "one"]
        with pytest.raises(ConflictingRecordError):
            await engine.emit_action(event("conflict", context=AuditContext(run_id="run-1", action_index=0)))
        assert await engine.store.get_event("conflict") is None
        with pytest.raises(ValueError):
            await engine.emit_action(event("missing-index", context=AuditContext(run_id="run-1")))


@pytest.mark.parametrize("lease", [0, -1, float("nan"), float("inf"), 3601])
@async_test
async def test_invalid_lease_cannot_claim_delivery(tmp_path, lease):
    store = EventStore(tmp_path / "lease.db")
    await store.initialize()
    with pytest.raises(ValueError):
        await store.claim_delivery(["consumer"], lease_seconds=lease)
    await store.close()


@async_test
async def test_exclusive_claim_and_expired_lease_replay(tmp_path):
    path = tmp_path / "leases.db"
    one, two = EventStore(path), EventStore(path)
    await one.initialize()
    await two.initialize()
    await one.register_consumer("consumer")
    await one.append_with_outbox([event()])
    claim = await one.claim_delivery(["consumer"], lease_seconds=0.03)
    assert claim is not None
    assert await two.claim_delivery(["consumer"]) is None
    await asyncio.sleep(0.04)
    reclaim = await two.claim_delivery(["consumer"])
    assert reclaim is not None
    assert reclaim[0].event_id == claim[0].event_id
    assert reclaim[3] != claim[3]
    # An old worker may not ACK another worker's current lease.
    await one.finish_delivery(claim[0], claim[1], claim[3])
    assert await one.pending_deliveries() == 1
    await two.finish_delivery(reclaim[0], reclaim[1], reclaim[3])
    assert await two.pending_deliveries() == 0
    await one.close()
    await two.close()


@async_test
async def test_repeated_unacknowledged_crashes_also_exhaust_retry_budget(tmp_path):
    store = EventStore(tmp_path / "crash-retry.db")
    await store.initialize()
    await store.register_consumer("crashing")
    await store.append_with_outbox([event()])
    assert await store.claim_delivery(["crashing"], lease_seconds=0.01, max_retries=0) is not None
    await asyncio.sleep(0.02)
    # No ACK: a process died after the claim. Initial attempt is still charged.
    assert await store.claim_delivery(["crashing"], max_retries=0) is None
    assert await store.pending_deliveries() == 0
    assert len(await store.get_dlq_records()) == 1
    await store.close()


@async_test
async def test_legacy_raw_record_is_sanitized_on_read_without_claiming_disk_erasure(tmp_path):
    store = EventStore(tmp_path / "legacy.db")
    await store.initialize()
    ev = event()
    await store.insert_event(ev)
    ev.action_details.error = "synthetic-old-secret@example.test"
    with store._conn:
        store._conn.execute("UPDATE events SET payload_json = ? WHERE event_id = ?", (ev.to_json(), ev.event_id))
    assert "synthetic-old-secret" not in (await store.get_event(ev.event_id)).to_json()
    assert "synthetic-old-secret" not in (await store.query_events())[0].to_json()
    # Existing bytes are not claimed to be erased by safe projection on read.
    assert "synthetic-old-secret" in store._conn.execute("SELECT payload_json FROM events").fetchone()[0]
    await store.close()


@async_test
async def test_direct_store_owns_snapshot_before_waiting_for_connection(tmp_path):
    store = EventStore(tmp_path / "snapshot.db")
    await store.initialize()
    ev = event()
    await store._lock.acquire()
    pending = asyncio.create_task(store.insert_event(ev))
    await asyncio.sleep(0)
    ev.status = ActionStatus.EXECUTED
    store._lock.release()
    await pending
    assert (await store.get_event("event-1")).status == ActionStatus.BLOCKED
    await store.close()


@async_test
async def test_unknown_schema_and_false_string_are_not_valid_evidence(tmp_path):
    async with PersistenceEngine(tmp_path / "schema.db") as engine:
        with pytest.raises(ValueError):
            await engine.emit_action(event(schema_version="unknown"))
        with pytest.raises(ValueError):
            InterceptionMetadata.from_dict({"verdict": "ALLOWED", "fault_injected": "false"})
        assert (await engine.get_stats())["total_events"] == 0


@async_test
async def test_corrupted_outbox_payload_is_routed_to_dlq_without_crashing_worker(tmp_path):
    store = EventStore(tmp_path / "corrupt.db")
    await store.initialize()
    ev = event("ev-corrupt")
    await store.append_with_outbox([ev])
    with store._conn:
        store._conn.execute("INSERT OR REPLACE INTO consumers (name, status) VALUES ('test-cons', 'ACTIVE')")
        store._conn.execute("INSERT INTO outbox (event_id, consumer_name) VALUES ('ev-corrupt', 'test-cons')")
        # Corrupt the payload_json in events table
        store._conn.execute("UPDATE events SET payload_json = 'INVALID_JSON{{' WHERE event_id = 'ev-corrupt'")

    # claim_delivery should cleanly handle corrupted json and unblock outbox without raising
    claimed = await store.claim_delivery(["test-cons"])
    assert claimed is None
    # Outbox job should be removed
    pending = store._conn.execute("SELECT COUNT(*) FROM outbox WHERE event_id = 'ev-corrupt'").fetchone()[0]
    assert pending == 0
    # DLQ should contain corrupted payload record
    dlqs = await store.get_dlq_records()
    assert any(d.error_message == "CONSUMER_DELIVERY_FAILED" and d.consumer_name == "test-cons" for d in dlqs)
    await store.close()


@async_test
async def test_prune_before_cleans_referencing_consumer_completions(tmp_path):
    store = EventStore(tmp_path / "prune.db")
    await store.initialize()
    ev = event("ev-prune")
    await store.append_with_outbox([ev])
    with store._conn:
        store._conn.execute("INSERT OR REPLACE INTO consumers (name, status) VALUES ('c1', 'ACTIVE')")
        store._conn.execute("INSERT INTO consumer_completions (consumer_name, event_id, completed_at) VALUES ('c1', 'ev-prune', '2020-01-01T00:00:00Z')")
        # Ensure outbox is empty for this event so it can be pruned
        store._conn.execute("DELETE FROM outbox WHERE event_id = 'ev-prune'")
        store._conn.execute("UPDATE events SET ts = '2020-01-01T00:00:00Z' WHERE event_id = 'ev-prune'")

    deleted = await store.prune_before("2021-01-01T00:00:00Z")
    assert deleted == 1
    # Check consumer_completions was also pruned
    remaining = store._conn.execute("SELECT COUNT(*) FROM consumer_completions WHERE event_id = 'ev-prune'").fetchone()[0]
    assert remaining == 0
    await store.close()

