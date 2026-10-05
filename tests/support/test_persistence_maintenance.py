"""Tests for retention pruning, capacity limits, and backup/restore operations."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from functools import wraps
import os
from pathlib import Path
import sqlite3
import pytest

from persistence.maintenance import (
    MaintenanceReport,
    backup_store,
    maintain,
    restore_store,
)
from persistence.models import (
    ActionDetails,
    ActionEventEnvelope,
    ActionStatus,
    ActionType,
    AuditorVerdict,
    InterceptionMetadata,
    RunBinding,
)
from persistence.settings import PersistenceSettings
from persistence.store import AuditBackpressureError, EventStore
from persistence.writer import BoundAuditWriter


def async_test(fn):
    @wraps(fn)
    def run(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))
    return run


from persistence.models import (
    ActionDetails,
    ActionEventEnvelope,
    ActionStatus,
    ActionType,
    AuditContext,
    AuditorVerdict,
    InterceptionMetadata,
    RunBinding,
)


def sample_envelope(
    event_id: str,
    run_id: Optional[str] = None,
    ts: str = "2026-10-03T12:00:00Z",
    action_index: int = 0,
) -> ActionEventEnvelope:
    context = (
        AuditContext(run_id=run_id, action_index=action_index)
        if run_id is not None
        else AuditContext()
    )
    return ActionEventEnvelope(
        event_id=event_id,
        trace_id=f"trace-{event_id}",
        session_id="session-1",
        case_id="case-1",
        agent_id="agent-1",
        action_type=ActionType.TOOL_CALL,
        status=ActionStatus.EXECUTED,
        action_details=ActionDetails(name="test_tool"),
        interception_metadata=InterceptionMetadata(
            verdict=AuditorVerdict.ALLOWED, policy_version="policy-v1"
        ),
        context=context,
        ts=ts,
    )


@async_test
async def test_expired_sealed_runs_are_pruned(tmp_path: Path):
    db_path = tmp_path / "prune.db"
    settings = PersistenceSettings(terminal_retention_seconds=3600.0)  # 1 hour
    store = EventStore(db_path, settings=settings)
    await store.initialize()
    writer = BoundAuditWriter(store)

    binding1 = RunBinding(
        run_id="run-old",
        contract_id="contract-1",
        session_id="session-1",
        principal_id="principal-1",
        agent_id="agent-1",
        policy_version="policy-v1",
        policy_hash="a" * 64,
        feed_version="feed-v1",
    )
    await writer.bind_run(binding1)

    now = datetime(2026, 10, 3, 12, 0, 0, tzinfo=timezone.utc)
    old_ts = (now - timedelta(hours=2)).isoformat()

    await store.register_consumer("live-feed")

    ev1 = sample_envelope("ev-old-1", "run-old", old_ts)
    await store.append_with_outbox([ev1])

    # Finish outbox deliveries so no pending jobs remain
    delivery = await store.claim_delivery(["live-feed"])
    assert delivery is not None
    await store.finish_delivery(ev1, "live-feed", delivery[3], failed=False)

    # Seal the run
    await writer.seal_run("run-old", verification_status="VERIFIED_SUCCESS")

    # Run maintenance at time `now`
    report = await maintain(store, now=now)
    assert report.events_pruned == 1
    assert report.pending_jobs == 0
    assert report.reason_code == "PRUNED"

    # Event is pruned from store
    assert await store.get_event("ev-old-1") is None

    # Check terminal run summary preserved with EXPIRED lifecycle
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT lifecycle, verification_status, expired_at FROM audit_runs WHERE run_id = ?",
            ("run-old",),
        ).fetchone()
        assert row is not None
        assert row[0] == "EXPIRED"
        assert row[1] == "VERIFIED_SUCCESS"
        assert row[2] is not None

    # Re-appending or binding to expired run must fail
    with pytest.raises(ValueError, match="not active"):
        await writer.append(
            run_id="run-old",
            event_id="ev-new",
            action_id="act-new",
            details=ActionDetails(name="test"),
            metadata=InterceptionMetadata(
                verdict=AuditorVerdict.ALLOWED, policy_version="policy-v1"
            ),
            status=ActionStatus.EXECUTED,
        )

    await store.close()


@async_test
async def test_active_runs_and_pending_jobs_are_protected(tmp_path: Path):
    db_path = tmp_path / "protected.db"
    settings = PersistenceSettings(terminal_retention_seconds=3600.0)
    store = EventStore(db_path, settings=settings)
    await store.initialize()
    writer = BoundAuditWriter(store)

    binding_active = RunBinding(
        run_id="run-active",
        contract_id="contract-1",
        session_id="session-1",
        principal_id="principal-1",
        agent_id="agent-1",
        policy_version="policy-v1",
        policy_hash="a" * 64,
        feed_version="feed-v1",
    )
    await writer.bind_run(binding_active)

    now = datetime(2026, 10, 3, 12, 0, 0, tzinfo=timezone.utc)
    old_ts = (now - timedelta(hours=2)).isoformat()

    ev_active = sample_envelope("ev-active", "run-active", old_ts)
    await store.append_with_outbox([ev_active])

    # Unsealed active run: event should NOT be pruned
    report = await maintain(store, now=now)
    assert report.events_pruned == 0
    assert await store.get_event("ev-active") is not None

    await store.close()


@async_test
async def test_active_export_holds_protect_run_from_pruning(tmp_path: Path):
    db_path = tmp_path / "holds.db"
    settings = PersistenceSettings(terminal_retention_seconds=3600.0)
    store = EventStore(db_path, settings=settings)
    await store.initialize()
    writer = BoundAuditWriter(store)

    binding = RunBinding(
        run_id="run-held",
        contract_id="contract-1",
        session_id="session-1",
        principal_id="principal-1",
        agent_id="agent-1",
        policy_version="policy-v1",
        policy_hash="a" * 64,
        feed_version="feed-v1",
    )
    await writer.bind_run(binding)

    now = datetime(2026, 10, 3, 12, 0, 0, tzinfo=timezone.utc)
    old_ts = (now - timedelta(hours=2)).isoformat()

    await store.register_consumer("live-feed")

    ev = sample_envelope("ev-held", "run-held", old_ts)
    await store.append_with_outbox([ev])

    # Finish outbox deliveries
    delivery = await store.claim_delivery(["live-feed"])
    if delivery:
        await store.finish_delivery(ev, "live-feed", delivery[3], failed=False)

    await writer.seal_run("run-held", verification_status="VERIFIED_SUCCESS")

    # Insert an active hold that expires in the future
    future_ts = (now + timedelta(minutes=5)).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO audit_export_holds (hold_id, run_id, principal_id, high_watermark, expires_at) VALUES (?, ?, ?, ?, ?)",
            ("hold-1", "run-held", "principal-1", 100, future_ts),
        )

    # Maintain while hold is active
    report = await maintain(store, now=now)
    assert report.events_pruned == 0
    assert await store.get_event("ev-held") is not None

    # Expire the hold and maintain again
    past_now = now + timedelta(minutes=10)
    report2 = await maintain(store, now=past_now)
    assert report2.events_pruned == 1
    assert await store.get_event("ev-held") is None

    await store.close()


@async_test
async def test_logical_payload_capacity_enforced_fail_closed(tmp_path: Path):
    db_path = tmp_path / "capacity.db"
    # Set a 1024 bytes capacity
    settings = PersistenceSettings(max_retained_logical_bytes=1024)
    store = EventStore(db_path, settings=settings)
    await store.initialize()

    # Create an event with ~1.5 KiB payload
    ev1 = sample_envelope("ev-cap-1", "run-cap", "2026-10-03T12:00:00Z")
    assert await store.append_with_outbox([ev1]) == 1

    # Second event exceeding 2048 bytes must raise AuditBackpressureError
    ev2_large = ActionEventEnvelope(
        event_id="ev-cap-2",
        trace_id="t2",
        session_id="s2",
        agent_id="a2",
        action_type=ActionType.TOOL_CALL,
        status=ActionStatus.EXECUTED,
        action_details=ActionDetails(name="large_tool", parameters={"data": "x" * 2000}),
        interception_metadata=InterceptionMetadata(
            verdict=AuditorVerdict.ALLOWED, policy_version="v1"
        ),
    )
    with pytest.raises(AuditBackpressureError, match="Logical payload quota exhausted"):
        await store.append_with_outbox([ev2_large])

    await store.close()


@async_test
async def test_backup_and_restore_with_epoch_rotation(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "original.db"
    backup_path = tmp_path / "backup.db"
    restored_path = tmp_path / "restored.db"

    real_fsync = os.fsync
    flushed = []
    def require_writable(fd):
        os.write(fd, b"")  # Portable regression: read-only descriptors cannot be flushed on Windows.
        flushed.append(fd)
        return real_fsync(fd)
    monkeypatch.setattr(os, "fsync", require_writable)

    store = EventStore(db_path)
    await store.initialize()

    ev = sample_envelope("ev-bk", "run-bk", "2026-10-03T12:00:00Z")
    await store.append_with_outbox([ev])

    # Record initial epoch
    with sqlite3.connect(db_path) as conn:
        initial_epoch = conn.execute("SELECT epoch FROM store_metadata WHERE singleton=1").fetchone()[0]

    # Create backup
    await backup_store(store, backup_path)
    assert backup_path.exists()

    # Restore to a new location
    restore_store(backup_path, restored_path)
    assert restored_path.exists()
    assert len(flushed) == 2

    # Validate restored store
    restored_store = EventStore(restored_path)
    await restored_store.initialize()

    # Restored event exists
    assert await restored_store.get_event("ev-bk") is not None

    # Epoch must be rotated to a fresh UUID
    with sqlite3.connect(restored_path) as conn:
        restored_epoch = conn.execute("SELECT epoch FROM store_metadata WHERE singleton=1").fetchone()[0]
        assert restored_epoch != initial_epoch
        assert len(restored_epoch) == 32

    await store.close()
    await restored_store.close()


@async_test
async def test_maintain_prunes_with_audit_run_indices(tmp_path: Path):
    db_path = tmp_path / "maintain_idx.db"
    settings = PersistenceSettings(terminal_retention_seconds=3600.0)
    store = EventStore(db_path, settings=settings)
    await store.initialize()
    writer = BoundAuditWriter(store)

    binding = RunBinding(
        run_id="run-maint-idx",
        contract_id="contract-1",
        session_id="session-1",
        principal_id="principal-1",
        agent_id="agent-1",
        policy_version="policy-v1",
        policy_hash="a" * 64,
        feed_version="feed-v1",
    )
    await writer.bind_run(binding)
    now = datetime(2026, 10, 3, 12, 0, 0, tzinfo=timezone.utc)
    old_ts = (now - timedelta(hours=2)).isoformat()

    ev = sample_envelope("ev-maint-1", "run-maint-idx", old_ts)
    await store.append_with_outbox([ev])
    with store._conn:
        store._conn.execute(
            "INSERT INTO audit_run_indices (run_id, action_index, event_id) VALUES ('run-maint-idx', 0, 'ev-maint-1')"
        )
    # Drain outbox
    await store.claim_delivery(["default"])
    await store.finish_delivery(ev, "default", "lease-1", failed=False)

    await writer.seal_run("run-maint-idx", verification_status="VERIFIED_SUCCESS")

    report = await maintain(store, now=now)
    assert report.events_pruned == 1
    assert await store.get_event("ev-maint-1") is None
    with sqlite3.connect(db_path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM audit_run_indices WHERE event_id = 'ev-maint-1'"
        ).fetchone()[0]
        assert count == 0
    await store.close()
