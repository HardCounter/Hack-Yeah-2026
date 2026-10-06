"""Scoped bounded read and export APIs for governed audit storage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Dict, List, Optional, Tuple, Union
import uuid

from persistence.models import ActionEventEnvelope
from persistence.privacy import sanitize_event
from persistence.store import AuditBackpressureError, EventStore


class EvidenceExpiredError(RuntimeError):
    """Raised when querying a run whose evidence has expired and been pruned."""


@dataclass(frozen=True)
class ReadScope:
    """Server-side authenticated read scope. Never deserialized from client body."""

    principal_id: str
    delegated_run_ids: frozenset[str] = frozenset()


@dataclass(frozen=True)
class AuditCursor:
    """Keyset pagination cursor with high watermark snapshot and hold linkage."""

    epoch: str
    after_offset: int
    high_watermark: int
    hold_id: str = ""


@dataclass(frozen=True)
class AuditPage:
    """Bounded page of immutable audit action envelopes."""

    events: Tuple[ActionEventEnvelope, ...]
    cursor: AuditCursor
    has_more: bool


class AuditReader:
    """Governed reader providing scoped, bounded keyset pagination and sanitized exports."""

    def __init__(self, store: EventStore) -> None:
        self.store = store

    def _sync_check_authorization(self, scope: ReadScope, run_id: str) -> Tuple[str, str]:
        conn = self.store._get_connection()
        row = conn.execute(
            "SELECT binding_json, lifecycle FROM audit_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Unknown or unbound run_id: '{run_id}'")

        binding_data = json.loads(row[0])
        lifecycle = row[1]

        if lifecycle == "EXPIRED":
            raise EvidenceExpiredError(f"Evidence for run '{run_id}' has expired and was pruned")

        owner_principal = binding_data.get("principal_id")
        if owner_principal != scope.principal_id:
            if run_id not in scope.delegated_run_ids:
                raise PermissionError(
                    f"Principal '{scope.principal_id}' is not authorized to read run '{run_id}'"
                )
            grant = conn.execute(
                "SELECT 1 FROM audit_read_grants WHERE run_id = ? AND principal_id = ?",
                (run_id, scope.principal_id),
            ).fetchone()
            if grant is None:
                raise PermissionError(
                    f"Delegated read grant for run '{run_id}' not found for principal '{scope.principal_id}'"
                )

        return owner_principal, lifecycle

    async def grant_read(self, run_id: str, principal_id: str, intervention_id: str) -> None:
        """Grant a principal delegated read access to a specific run under an intervention ID."""
        if not run_id or not principal_id or not intervention_id:
            raise ValueError("run_id, principal_id, and intervention_id are required")

        def _sync_grant():
            conn = self.store._get_connection()
            with conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO audit_read_grants (run_id, principal_id, intervention_id)
                    VALUES (?, ?, ?)
                    """,
                    (run_id, principal_id, intervention_id),
                )

        async with self.store._lock:
            await self.store._offload(_sync_grant)

    async def revoke_read(self, run_id: str, principal_id: str, intervention_id: str) -> None:
        """Revoke a principal's delegated read access to a specific run."""
        def _sync_revoke():
            conn = self.store._get_connection()
            with conn:
                conn.execute(
                    "DELETE FROM audit_read_grants WHERE run_id = ? AND principal_id = ?",
                    (run_id, principal_id),
                )

        async with self.store._lock:
            await self.store._offload(_sync_revoke)

    async def page(
        self,
        scope: ReadScope,
        run_id: str,
        cursor: Optional[AuditCursor] = None,
        limit: int = 100,
    ) -> AuditPage:
        """Fetch a bounded page of audit envelopes using keyset pagination."""
        if not (1 <= limit <= 1000):
            raise ValueError("limit must be between 1 and 1000")

        def _sync_page() -> AuditPage:
            conn = self.store._get_connection()
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                return _sync_page_transaction(conn)

        def _sync_page_transaction(conn) -> AuditPage:
            self._sync_check_authorization(scope, run_id)

            now = datetime.now(timezone.utc)
            expires_at = (now + timedelta(seconds=self.store.settings.export_hold_seconds)).isoformat()
            conn.execute("DELETE FROM audit_export_holds WHERE expires_at <= ?", (now.isoformat(),))

            meta = conn.execute(
                "SELECT epoch, next_offset FROM store_metadata WHERE singleton = 1"
            ).fetchone()
            current_epoch = meta[0]
            current_next_offset = meta[1]

            if cursor is not None:
                if cursor.epoch != current_epoch:
                    raise ValueError(
                        f"Cursor epoch mismatch: store epoch has rotated from '{cursor.epoch}' to '{current_epoch}'"
                    )
                after_offset = cursor.after_offset
                high_watermark = cursor.high_watermark
                hold_id = cursor.hold_id
                hold = conn.execute(
                    "SELECT run_id, principal_id, high_watermark FROM audit_export_holds WHERE hold_id = ?",
                    (hold_id,),
                ).fetchone()
                if hold is None or tuple(hold) != (run_id, scope.principal_id, high_watermark):
                    raise ValueError("Cursor retention hold is expired or does not match the read scope")
                if not 0 <= after_offset <= high_watermark:
                    raise ValueError("Invalid cursor offset")
                conn.execute("UPDATE audit_export_holds SET expires_at = ? WHERE hold_id = ?", (expires_at, hold_id))
            else:
                after_offset = 0
                high_watermark = current_next_offset - 1
                hold_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO audit_export_holds (hold_id, run_id, principal_id, high_watermark, expires_at) VALUES (?, ?, ?, ?, ?)",
                    (hold_id, run_id, scope.principal_id, high_watermark, expires_at),
                )

            cursor_db = conn.execute(
                """
                SELECT payload_json, ingest_offset FROM events
                WHERE ingest_offset > ? AND ingest_offset <= ?
                  AND json_extract(payload_json, '$.context.run_id') = ?
                ORDER BY ingest_offset ASC
                LIMIT ?
                """,
                (after_offset, high_watermark, run_id, limit),
            )
            rows = cursor_db.fetchall()

            events = [
                sanitize_event(ActionEventEnvelope.from_json(r[0]))
                for r in rows
            ]

            new_after = rows[-1][1] if rows else after_offset
            # Check if there are further records within high watermark
            has_more = False
            if rows:
                next_check = conn.execute(
                    """
                    SELECT 1 FROM events
                    WHERE ingest_offset > ? AND ingest_offset <= ?
                      AND json_extract(payload_json, '$.context.run_id') = ?
                    LIMIT 1
                    """,
                    (new_after, high_watermark, run_id),
                ).fetchone()
                has_more = next_check is not None

            if not has_more:
                conn.execute("DELETE FROM audit_export_holds WHERE hold_id = ?", (hold_id,))

            new_cursor = AuditCursor(
                epoch=current_epoch,
                after_offset=new_after,
                high_watermark=high_watermark,
                hold_id=hold_id,
            )
            return AuditPage(events=tuple(events), cursor=new_cursor, has_more=has_more)

        async with self.store._lock:
            return await self.store._offload(_sync_page)

    async def export_jsonl(
        self, scope: ReadScope, run_id: str, destination: Union[str, Path]
    ) -> int:
        """Sanitized JSONL export protected by retention hold and central quota."""
        dest = Path(destination).resolve()
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp_dest = dest.with_suffix(".export.tmp")

        hold_id = uuid.uuid4().hex
        settings = self.store.settings

        # 1. Authorize & acquire retention hold
        async with self.store._lock:
            def _sync_acquire_hold():
                conn = self.store._get_connection()
                self._sync_check_authorization(scope, run_id)
                meta = conn.execute(
                    "SELECT next_offset FROM store_metadata WHERE singleton = 1"
                ).fetchone()
                hw = meta[0] - 1
                now_iso = datetime.now(timezone.utc)
                expires_at = (now_iso + timedelta(seconds=settings.export_hold_seconds)).isoformat()
                conn.execute(
                    """
                    INSERT INTO audit_export_holds (hold_id, run_id, principal_id, high_watermark, expires_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (hold_id, run_id, scope.principal_id, hw, expires_at),
                )
                return hw

            high_watermark = await self.store._offload(_sync_acquire_hold)

        # 2. Iterate pages and stream sanitized JSONL
        try:
            exported_rows = 0
            exported_bytes = 0
            after_offset = 0

            # Keep LF on every platform so the byte quota describes the actual
            # JSONL file; flush the writable descriptor before atomic publication.
            with open(tmp_dest, "w", encoding="utf-8", newline="\n") as f:
                while True:
                    async with self.store._lock:
                        def _sync_fetch_chunk(after: int):
                            conn = self.store._get_connection()
                            cursor_db = conn.execute(
                                """
                                SELECT payload_json, ingest_offset FROM events
                                WHERE ingest_offset > ? AND ingest_offset <= ?
                                  AND json_extract(payload_json, '$.context.run_id') = ?
                                ORDER BY ingest_offset ASC
                                LIMIT 100
                                """,
                                (after, high_watermark, run_id),
                            )
                            return cursor_db.fetchall()

                        rows = await self.store._offload(_sync_fetch_chunk, after_offset)

                    if not rows:
                        break

                    for r in rows:
                        ev = sanitize_event(ActionEventEnvelope.from_json(r[0]))
                        line = ev.to_json() + "\n"
                        line_bytes = len(line.encode("utf-8"))

                        if exported_rows + 1 > settings.max_export_rows:
                            raise AuditBackpressureError(
                                f"Export exceeded maximum row quota of {settings.max_export_rows}"
                            )
                        if exported_bytes + line_bytes > settings.max_export_bytes:
                            raise AuditBackpressureError(
                                f"Export exceeded maximum byte quota of {settings.max_export_bytes}"
                            )

                        f.write(line)
                        exported_rows += 1
                        exported_bytes += line_bytes

                    after_offset = rows[-1][1]

                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_dest, dest)
            return exported_rows

        finally:
            if tmp_dest.exists():
                tmp_dest.unlink()
            # Release hold
            async with self.store._lock:
                def _sync_release_hold():
                    conn = self.store._get_connection()
                    conn.execute("DELETE FROM audit_export_holds WHERE hold_id = ?", (hold_id,))

                await self.store._offload(_sync_release_hold)

    async def get_run_report(self, scope: ReadScope, run_id: str) -> Dict[str, Any]:
        """Produce diagnostic and regulatory evidence summary for an authorized run."""
        async with self.store._lock:
            def _sync_report() -> Dict[str, Any]:
                conn = self.store._get_connection()
                self._sync_check_authorization(scope, run_id)
                row = conn.execute(
                    "SELECT binding_json, next_index, lifecycle, verification_status, sealed_at, expired_at FROM audit_runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                binding = json.loads(row[0])
                total_events = conn.execute(
                    "SELECT COUNT(*) FROM events WHERE json_extract(payload_json, '$.context.run_id') = ?",
                    (run_id,),
                ).fetchone()[0]

                return {
                    "run_id": run_id,
                    "binding": binding,
                    "lifecycle": row[2],
                    "verification_status": row[3],
                    "sealed_at": row[4],
                    "expired_at": row[5],
                    "total_events": total_events,
                }

            return await self.store._offload(_sync_report)
