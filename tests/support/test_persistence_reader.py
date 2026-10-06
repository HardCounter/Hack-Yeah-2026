"""Tests for scoped bounded read/export APIs, pagination, and evidence holds."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from functools import wraps
import json
import os
from pathlib import Path
import pytest

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
from persistence.reader import (
    AuditCursor,
    AuditReader,
    EvidenceExpiredError,
    ReadScope,
)
from persistence.settings import PersistenceSettings
from persistence.store import AuditBackpressureError, EventStore
from persistence.writer import BoundAuditWriter


def async_test(fn):
    @wraps(fn)
    def run(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))
    return run


async def setup_test_run(
    store: EventStore,
    run_id: str,
    principal_id: str,
    event_count: int = 5,
) -> BoundAuditWriter:
    writer = BoundAuditWriter(store)
    binding = RunBinding(
        run_id=run_id,
        contract_id=f"contract-{run_id}",
        session_id=f"session-{run_id}",
        principal_id=principal_id,
        agent_id=f"agent-{run_id}",
        policy_version="policy-v1",
        policy_hash="a" * 64,
        feed_version="feed-v1",
    )
    await writer.bind_run(binding)

    for i in range(event_count):
        await writer.append(
            run_id=run_id,
            event_id=f"{run_id}-ev-{i}",
            action_id=f"{run_id}-act-{i}",
            details=ActionDetails(name=f"tool_{i}"),
            metadata=InterceptionMetadata(
                verdict=AuditorVerdict.ALLOWED, policy_version="policy-v1"
            ),
            status=ActionStatus.EXECUTED,
        )
    return writer


@async_test
async def test_foreign_run_is_denied(tmp_path: Path):
    db_path = tmp_path / "reader_auth.db"
    store = EventStore(db_path)
    await store.initialize()
    reader = AuditReader(store)

    await setup_test_run(store, "run-1", "principal-owner", event_count=2)

    # Foreign principal without delegation must fail
    with pytest.raises(PermissionError, match="not authorized"):
        await reader.page(ReadScope("principal-other"), "run-1")

    # Claiming delegation without persisted grant must also fail
    with pytest.raises(PermissionError, match="grant.*not found"):
        await reader.page(
            ReadScope("principal-other", frozenset(["run-1"])), "run-1"
        )

    await store.close()


@async_test
async def test_delegated_read_grant_and_revocation(tmp_path: Path):
    db_path = tmp_path / "delegation.db"
    store = EventStore(db_path)
    await store.initialize()
    reader = AuditReader(store)

    await setup_test_run(store, "run-delegated", "principal-alice", event_count=3)

    # Grant Bob read access under an intervention
    await reader.grant_read("run-delegated", "principal-bob", "INT-GRANT-1")

    # Bob can now read the run
    page = await reader.page(
        ReadScope("principal-bob", frozenset(["run-delegated"])),
        "run-delegated",
    )
    assert len(page.events) == 3

    # Revoke Bob's read access
    await reader.revoke_read("run-delegated", "principal-bob", "INT-REVOKE-1")

    # Access is now denied
    with pytest.raises(PermissionError):
        await reader.page(
            ReadScope("principal-bob", frozenset(["run-delegated"])),
            "run-delegated",
        )

    await store.close()


@async_test
async def test_keyset_pagination_and_high_watermark(tmp_path: Path):
    db_path = tmp_path / "pagination.db"
    store = EventStore(db_path)
    await store.initialize()
    reader = AuditReader(store)

    await setup_test_run(store, "run-pages", "principal-alice", event_count=5)
    scope = ReadScope("principal-alice")

    # Page 1: limit 2
    page1 = await reader.page(scope, "run-pages", limit=2)
    assert len(page1.events) == 2
    assert page1.has_more is True
    assert page1.cursor.after_offset > 0
    hw = page1.cursor.high_watermark

    # Page 2: pass cursor
    page2 = await reader.page(scope, "run-pages", cursor=page1.cursor, limit=2)
    assert len(page2.events) == 2
    assert page2.has_more is True
    assert page2.cursor.high_watermark == hw

    # Page 3: last page
    page3 = await reader.page(scope, "run-pages", cursor=page2.cursor, limit=2)
    assert len(page3.events) == 1
    assert page3.has_more is False

    await store.close()


@async_test
async def test_page_hold_preserves_snapshot_during_retention_and_releases_at_end(tmp_path):
    from persistence.maintenance import maintain
    store = EventStore(tmp_path / "held-pages.db", settings=PersistenceSettings(terminal_retention_seconds=1))
    await store.initialize()
    try:
        writer = await setup_test_run(store, "run-held", "principal-alice", event_count=3)
        await writer.seal_run("run-held")
        reader = AuditReader(store)
        scope = ReadScope("principal-alice")
        first = await reader.page(scope, "run-held", limit=1)
        assert first.cursor.hold_id and first.has_more
        report = await maintain(store, datetime.now(timezone.utc) + timedelta(seconds=2))
        assert report.events_pruned == 0
        second = await reader.page(scope, "run-held", first.cursor, limit=2)
        assert len(second.events) == 2 and not second.has_more
        assert store._get_connection().execute("SELECT COUNT(*) FROM audit_export_holds").fetchone()[0] == 0
        report = await maintain(store, datetime.now(timezone.utc) + timedelta(seconds=2))
        assert report.events_pruned == 3
    finally:
        await store.close()


@async_test
async def test_page_rejects_expired_or_mismatched_retention_hold(tmp_path):
    store = EventStore(tmp_path / "expired-pages.db")
    await store.initialize()
    try:
        await setup_test_run(store, "run-pages", "principal-alice", event_count=3)
        reader, scope = AuditReader(store), ReadScope("principal-alice")
        first = await reader.page(scope, "run-pages", limit=1)
        for cursor in (replace(first.cursor, hold_id="forged"),
                       replace(first.cursor, high_watermark=first.cursor.high_watermark + 1)):
            with pytest.raises(ValueError, match="retention hold"):
                await reader.page(scope, "run-pages", cursor, limit=1)
        with store._get_connection() as conn:
            conn.execute("UPDATE audit_export_holds SET expires_at = '2000-01-01T00:00:00+00:00'")
        with pytest.raises(ValueError, match="retention hold"):
            await reader.page(scope, "run-pages", first.cursor, limit=1)
    finally:
        await store.close()


@async_test
async def test_rotated_epoch_rejects_old_cursor(tmp_path: Path):
    db_path = tmp_path / "epoch.db"
    store = EventStore(db_path)
    await store.initialize()
    reader = AuditReader(store)

    await setup_test_run(store, "run-epoch", "principal-alice", event_count=3)
    scope = ReadScope("principal-alice")

    page1 = await reader.page(scope, "run-epoch", limit=1)
    old_cursor = page1.cursor

    # Forge or rotate epoch in cursor
    bogus_cursor = AuditCursor(
        epoch="old-invalid-epoch",
        after_offset=old_cursor.after_offset,
        high_watermark=old_cursor.high_watermark,
    )

    with pytest.raises(ValueError, match="Cursor epoch mismatch"):
        await reader.page(scope, "run-epoch", cursor=bogus_cursor, limit=1)

    await store.close()


@async_test
async def test_export_jsonl_with_retention_hold_and_quota(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "export.db"
    export_file = tmp_path / "exported.jsonl"
    real_fsync = os.fsync
    flushed = []
    def require_writable(fd):
        os.write(fd, b"")  # Reject reopening the completed export with a read-only descriptor.
        flushed.append(fd)
        return real_fsync(fd)
    monkeypatch.setattr(os, "fsync", require_writable)
    store = EventStore(db_path)
    await store.initialize()
    reader = AuditReader(store)

    await setup_test_run(store, "run-export", "principal-alice", event_count=4)
    scope = ReadScope("principal-alice")

    # Export to jsonl
    count = await reader.export_jsonl(scope, "run-export", export_file)
    assert count == 4
    assert export_file.exists()
    assert len(flushed) == 1
    assert export_file.read_bytes().count(b"\n") == 4
    assert b"\r\n" not in export_file.read_bytes()

    lines = export_file.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 4
    for line in lines:
        data = json.loads(line)
        assert data["schema_version"] == "2.0"
        assert data["context"]["run_id"] == "run-export"

    # Close initial store before opening small_store on same database
    await store.close()

    # Export quota enforcement: test exceeding row quota
    small_quota_settings = PersistenceSettings(max_export_rows=2)
    small_store = EventStore(db_path, settings=small_quota_settings)
    await small_store.initialize()
    small_reader = AuditReader(small_store)

    with pytest.raises(AuditBackpressureError, match="maximum row quota"):
        await small_reader.export_jsonl(
            scope, "run-export", tmp_path / "exceeded.jsonl"
        )

    await small_store.close()


@async_test
async def test_expired_run_raises_evidence_expired_error(tmp_path: Path):
    db_path = tmp_path / "expired_read.db"
    store = EventStore(db_path)
    await store.initialize()
    reader = AuditReader(store)
    writer = await setup_test_run(store, "run-expired", "principal-alice", event_count=1)

    # Manually transition run to EXPIRED in database
    conn = store._get_connection()
    with conn:
        conn.execute(
            "UPDATE audit_runs SET lifecycle = 'EXPIRED', expired_at = '2026-10-03T12:00:00Z' WHERE run_id = 'run-expired'"
        )

    scope = ReadScope("principal-alice")
    with pytest.raises(EvidenceExpiredError, match="expired and was pruned"):
        await reader.page(scope, "run-expired")

    with pytest.raises(EvidenceExpiredError, match="expired and was pruned"):
        await reader.export_jsonl(scope, "run-expired", tmp_path / "dummy.jsonl")

    await store.close()
