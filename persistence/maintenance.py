"""Retention, capacity, and backup/restore operations for governed audit storage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sqlite3
from typing import Optional, Union
import uuid

from persistence.schema import CURRENT_SCHEMA_VERSION
from persistence.store import EventStore


@dataclass(frozen=True)
class MaintenanceReport:
    """Diagnostic evidence and outcome of a maintenance pass."""

    events_pruned: int
    pending_jobs: int
    logical_bytes: int
    database_bytes: int
    wal_bytes: int
    reason_code: str


async def maintain(store: EventStore, now: Optional[datetime] = None) -> MaintenanceReport:
    """Execute deterministic maintenance: prune terminal runs and report capacity pressure."""
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    now_ts = now.isoformat()
    cutoff_dt = now - timedelta(seconds=store.settings.terminal_retention_seconds)
    cutoff_ts = cutoff_dt.isoformat()

    def _sync_maintain() -> MaintenanceReport:
        conn = store._get_connection()
        with conn:
            conn.execute("BEGIN IMMEDIATE")

            # 1. Prune expired events belonging to SEALED runs that have no pending deliveries or active holds
            cursor = conn.execute(
                """
                SELECT e.event_id, json_extract(e.payload_json, '$.context.run_id') AS run_id
                FROM events e
                JOIN audit_runs r ON r.run_id = json_extract(e.payload_json, '$.context.run_id')
                WHERE r.lifecycle = 'SEALED'
                  AND e.ts < ?
                  AND NOT EXISTS (SELECT 1 FROM outbox o WHERE o.event_id = e.event_id)
                  AND NOT EXISTS (
                      SELECT 1 FROM audit_export_holds h
                      WHERE h.run_id = r.run_id AND h.expires_at > ?
                  )
                """,
                (cutoff_ts, now_ts),
            )
            rows = cursor.fetchall()
            pruned_event_ids = [r[0] for r in rows]
            impacted_run_ids = list(set(r[1] for r in rows if r[1]))

            if pruned_event_ids:
                for i in range(0, len(pruned_event_ids), 500):
                    batch = pruned_event_ids[i : i + 500]
                    ph = ",".join("?" for _ in batch)
                    conn.execute(f"DELETE FROM audit_run_indices WHERE event_id IN ({ph})", batch)
                    conn.execute(f"DELETE FROM run_order WHERE event_id IN ({ph})", batch)
                    conn.execute(f"DELETE FROM consumer_completions WHERE event_id IN ({ph})", batch)
                    conn.execute(f"DELETE FROM events WHERE event_id IN ({ph})", batch)

            # Prune alerts older than cutoff
            conn.execute("DELETE FROM alerts WHERE ts < ?", (cutoff_ts,))

            # Prune dead letters older than cutoff
            conn.execute("DELETE FROM dead_letter_queue WHERE failed_at < ?", (cutoff_ts,))

            # 2. Transition sealed runs without remaining events to EXPIRED
            for r_id in impacted_run_ids:
                rem = conn.execute(
                    "SELECT 1 FROM events WHERE json_extract(payload_json, '$.context.run_id') = ? LIMIT 1",
                    (r_id,),
                ).fetchone()
                if rem is None:
                    conn.execute(
                        "UPDATE audit_runs SET lifecycle = 'EXPIRED', expired_at = ? WHERE run_id = ? AND lifecycle = 'SEALED'",
                        (now_ts, r_id),
                    )

            pending_jobs = conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
            logical_bytes = store._sync_logical_bytes()

        # Disk/file measurements
        db_bytes = 0
        wal_bytes = 0
        if store.db_path != ":memory:":
            p = Path(store.db_path)
            if p.exists():
                db_bytes = p.stat().st_size
            wal_p = Path(f"{store.db_path}-wal")
            if wal_p.exists():
                wal_bytes = wal_p.stat().st_size

        reason = "OK"
        if len(pruned_event_ids) > 0:
            reason = "PRUNED"
        if logical_bytes > store.settings.max_retained_logical_bytes * 0.9:
            reason = "CAPACITY_PRESSURE"

        return MaintenanceReport(
            events_pruned=len(pruned_event_ids),
            pending_jobs=pending_jobs,
            logical_bytes=logical_bytes,
            database_bytes=db_bytes,
            wal_bytes=wal_bytes,
            reason_code=reason,
        )

    async with store._lock:
        return await store._offload(_sync_maintain)


async def backup_store(store: EventStore, destination: Union[str, Path]) -> None:
    """Create a consistent SQLite backup under serialized access with integrity check."""
    dest = Path(destination).resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)

    def _sync_backup() -> None:
        tmp_dest = dest.with_suffix(".backup.tmp")
        if tmp_dest.exists():
            tmp_dest.unlink()

        dest_conn = sqlite3.connect(str(tmp_dest))
        try:
            with dest_conn:
                store._get_connection().backup(dest_conn)
            qc = dest_conn.execute("PRAGMA quick_check").fetchall()
            if not qc or qc[0][0] != "ok":
                raise ValueError("Backup integrity verification failed")
        finally:
            dest_conn.close()

        # Windows fsync requires a writable descriptor; SQLite has already closed
        # the backup, and r+b preserves its verified bytes without truncation.
        with open(tmp_dest, "r+b") as f:
            os.fsync(f.fileno())
        os.replace(tmp_dest, dest)

    async with store._lock:
        await store._offload(_sync_backup)


def restore_store(source: Union[str, Path], destination: Union[str, Path]) -> None:
    """Atomically restore a consistent backup, rotate cursor epoch, and fsync destination."""
    src = Path(source).resolve()
    dest = Path(destination).resolve()
    if not src.exists():
        raise FileNotFoundError(f"Source backup file not found: {src}")

    # Validate source integrity in read-only mode
    src_uri = f"{src.as_uri()}?mode=ro"
    src_conn = sqlite3.connect(src_uri, uri=True)
    try:
        qc = src_conn.execute("PRAGMA quick_check").fetchall()
        if not qc or qc[0][0] != "ok":
            raise ValueError("Corrupted source backup store: quick_check failed")
        v = src_conn.execute("PRAGMA user_version").fetchone()[0]
        if v > CURRENT_SCHEMA_VERSION:
            raise ValueError(f"Incompatible backup schema version: {v}")
    finally:
        src_conn.close()

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_dest = dest.with_suffix(".restore.tmp")
    if tmp_dest.exists():
        tmp_dest.unlink()

    tmp_conn = sqlite3.connect(str(tmp_dest))
    try:
        src_conn = sqlite3.connect(src_uri, uri=True)
        try:
            with tmp_conn:
                src_conn.backup(tmp_conn)
        finally:
            src_conn.close()

        # Rotate cursor epoch to invalidate prior read cursors
        new_epoch = uuid.uuid4().hex
        with tmp_conn:
            tmp_conn.execute(
                "UPDATE store_metadata SET epoch = ? WHERE singleton = 1", (new_epoch,)
            )
        qc = tmp_conn.execute("PRAGMA quick_check").fetchall()
        if not qc or qc[0][0] != "ok":
            raise ValueError("Restored database integrity check failed")
    finally:
        tmp_conn.close()

    with open(tmp_dest, "r+b") as f:
        os.fsync(f.fileno())
    os.replace(tmp_dest, dest)
