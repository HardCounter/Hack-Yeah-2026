"""EventStore implementation using standard library sqlite3 and asyncio.to_thread.

Provides an append-optimized, indexed relational store for action envelopes,
security alerts, audit action records, and dead letter queue messages.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import shutil
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Union

from persistence.models import (
    ActionEventEnvelope,
    AlertEvent,
    AuditActionRecord,
    ConsumerResult,
    DeadLetterEnvelope,
    Severity,
)
from persistence.privacy import sanitize_event, sanitize_alert, sanitize_audit, sanitize_dead_letter
from persistence.schema import migrate_audit_schema
from persistence.settings import PersistenceSettings


class ConflictingRecordError(ValueError):
    """An existing immutable record ID was reused with different evidence."""


class AuditBackpressureError(RuntimeError):
    """The bounded durable outbox is full; callers must pause critical dispatch."""


class EventStore:
    """Asynchronous SQLite event store with WAL mode and threadpool offloading."""

    def __init__(
        self,
        db_path: Union[str, Path] = ":memory:",
        *,
        settings: Optional[PersistenceSettings] = None,
        outbox_maxsize: Optional[int] = None,
    ) -> None:
        if settings is not None:
            if not isinstance(settings, PersistenceSettings):
                raise ValueError("settings must be a PersistenceSettings instance")
            if outbox_maxsize is not None and settings.outbox_maxsize != outbox_maxsize:
                raise ValueError("conflicting outbox_maxsize and settings")
            self.settings = settings
        else:
            if outbox_maxsize is not None:
                self.settings = PersistenceSettings(outbox_maxsize=outbox_maxsize)
            else:
                self.settings = PersistenceSettings()

        self.outbox_maxsize = self.settings.outbox_maxsize
        self.db_path = str(db_path)
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = asyncio.Lock()
        self._initialized = False

    async def _offload(self, fn, *args):
        """Keep the connection lock until a thread finishes, even on cancellation.

        Cancelling asyncio.to_thread does not stop SQLite in the underlying thread.
        Releasing the lock early would race another transaction or close().
        """
        task = asyncio.create_task(asyncio.to_thread(fn, *args))
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        result = task.result()
        if cancelled:
            raise asyncio.CancelledError
        return result

    def _insert_immutable(self, table, columns, values) -> bool:
        conn = self._get_connection()
        key = columns[0]
        compare_cols = [c for c in columns if c != "ingest_offset"]
        placeholders = ', '.join(compare_cols)
        previous = conn.execute(
            f"SELECT {placeholders} FROM {table} WHERE {key} = ?", (values[0],)
        ).fetchone()
        if previous is not None:
            expected = tuple(v for c, v in zip(columns, values) if c != "ingest_offset")
            if tuple(previous) != expected:
                raise ConflictingRecordError("Conflicting immutable audit record")
            return False
        conn.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
            values,
        )
        return True

    def _get_connection(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("EventStore is not initialized. Call initialize() first.")
        return self._conn

    def _sync_initialize(self) -> None:
        if self.db_path != ":memory:":
            db_dir = os.path.dirname(os.path.abspath(self.db_path))
            if db_dir:
                os.makedirs(db_dir, exist_ok=True)

        conn = sqlite3.connect(self.db_path, timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row

        try:
            conn.execute("PRAGMA busy_timeout=30000")
            # Concurrency & Performance PRAGMAs
            if self.db_path != ":memory:":
                for attempt in range(50):
                    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
                    if mode.lower() == "wal":
                        break
                    try:
                        conn.execute("PRAGMA journal_mode=WAL")
                        break
                    except sqlite3.OperationalError as exc:
                        if "locked" in str(exc).lower() and attempt < 49:
                            time.sleep(0.01 * (1.5 ** min(attempt, 6)))
                        else:
                            raise
            conn.execute("PRAGMA synchronous=FULL")

            migrate_audit_schema(conn, self.settings)
            conn.execute("PRAGMA foreign_keys=ON")
        except Exception:
            conn.close()
            raise

        self._conn = conn
        self._initialized = True

    async def initialize(self) -> None:
        """Initialize SQLite tables and indexes."""
        async with self._lock:
            if not self._initialized:
                await self._offload(self._sync_initialize)

    def _sync_close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
            self._initialized = False

    async def close(self) -> None:
        """Close SQLite database connection."""
        async with self._lock:
            if self._conn is not None:
                await self._offload(self._sync_close)

    def _event_row(self, event):
        event = sanitize_event(event)
        payload = json.dumps(event.to_dict(), sort_keys=True, separators=(",", ":"))
        if len(payload.encode("utf-8")) > 65536:
            raise ValueError("Sanitized event exceeds 64 KiB limit")
        return (
            event.event_id, event.trace_id, event.session_id, event.case_id,
            event.agent_id, event.action_type.value, event.status.value, event.ts,
            event.interception_metadata.total_latency_ms, payload,
        )

    def _insert_event_row(self, row, *, assign_seq: bool = True):
        conn = self._get_connection()
        previous = conn.execute(
            "SELECT payload_json, seq FROM events WHERE event_id = ?", (row[0],)
        ).fetchone()
        if previous is not None:
            incoming = json.loads(row[-1])
            incoming["seq"] = previous[1]
            if json.dumps(incoming, sort_keys=True, separators=(",", ":")) != previous[0]:
                raise ConflictingRecordError("Conflicting immutable audit record")
            return False

        session_id = row[2]
        seq = None
        if assign_seq:
            conn.execute(
                "INSERT OR IGNORE INTO session_sequences(session_id,next_seq) VALUES(?,0)",
                (session_id,),
            )
            seq = conn.execute(
                "SELECT next_seq FROM session_sequences WHERE session_id = ?", (session_id,)
            ).fetchone()[0]
        supplied = json.loads(row[-1])
        if supplied.get("seq") is not None and not assign_seq:
            raise ValueError("Layer 2 owns event seq; caller-supplied seq is forbidden")
        supplied["seq"] = seq
        payload = json.dumps(supplied, sort_keys=True, separators=(",", ":"))
        row = row[:-1] + (payload,)
        if assign_seq:
            conn.execute(
                "UPDATE session_sequences SET next_seq = next_seq + 1 WHERE session_id = ?",
                (session_id,),
            )

        # Allocate monotonic ingest_offset from store_metadata
        offset_row = conn.execute(
            "SELECT next_offset FROM store_metadata WHERE singleton = 1"
        ).fetchone()
        if offset_row is not None:
            offset = offset_row[0]
            conn.execute(
                "UPDATE store_metadata SET next_offset = next_offset + 1 WHERE singleton = 1"
            )
        else:
            offset = 1

        full_row = row[:9] + (seq, row[9], offset)
        self._insert_immutable("events", (
            "event_id", "trace_id", "session_id", "case_id", "agent_id",
            "action_type", "status", "ts", "total_latency_ms", "seq", "payload_json", "ingest_offset",
        ), full_row)

        context = json.loads(row[-1])["context"]
        if context["run_id"] is not None and context["action_index"] is not None:
            prev = conn.execute(
                "SELECT event_id FROM run_order WHERE run_id = ? AND action_index = ?",
                (context["run_id"], context["action_index"])).fetchone()
            if prev is not None:
                raise ConflictingRecordError("Run action index already recorded")
            conn.execute(
                "INSERT INTO run_order(run_id, action_index, event_id) VALUES (?, ?, ?)",
                (context["run_id"], context["action_index"], row[0]))
        return True

    def _sync_insert_events_batch(self, events):
        rows = [self._event_row(event) for event in events]
        conn = self._get_connection()
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            return sum(self._insert_event_row(row) for row in rows)

    async def insert_event(self, event: ActionEventEnvelope) -> None:
        """Append sanitized immutable evidence; identical retries are idempotent."""
        await self.insert_events_batch([event])

    async def insert_events_batch(self, events: List[ActionEventEnvelope]) -> int:
        """Atomically append a batch; conflicting IDs roll the entire batch back."""
        events = [sanitize_event(event) for event in events]
        async with self._lock:
            return await self._offload(self._sync_insert_events_batch, events)

    def _sync_insert_alert(self, alert):
        alert = sanitize_alert(alert)
        conn = self._get_connection()
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            self._insert_immutable("alerts", (
                "alert_id", "ts", "severity", "rule", "agent_id", "session_id",
                "case_id", "action_taken", "evidence_json",
            ), (alert.alert_id, alert.ts, alert.severity.value, alert.rule,
                alert.agent_id, alert.session_id, alert.case_id, alert.action_taken,
                json.dumps(alert.evidence, sort_keys=True)))

    async def insert_alert(self, alert: AlertEvent) -> None:
        alert = sanitize_alert(alert)
        async with self._lock:
            await self._offload(self._sync_insert_alert, alert)

    def _sync_insert_audit_action(self, action):
        action = sanitize_audit(action)
        conn = self._get_connection()
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            self._insert_immutable("audit_actions", (
                "action_id", "ts", "run_id", "session_id", "agent_id", "tool_name",
                "target_id", "side_effect_class", "status", "details_json",
            ), (action.action_id, action.ts, action.run_id, action.session_id,
                action.agent_id, action.tool_name, action.target_id,
                action.side_effect_class, action.status,
                json.dumps(action.details, sort_keys=True)))

    async def insert_audit_action(self, action: AuditActionRecord) -> None:
        action = sanitize_audit(action)
        async with self._lock:
            await self._offload(self._sync_insert_audit_action, action)

    def _insert_dead_letter(self, dlq):
        dlq = sanitize_dead_letter(dlq)
        event_id = dlq.event.event_id if isinstance(dlq.event, ActionEventEnvelope) else dlq.event.get("event_id")
        self._insert_immutable("dead_letter_queue", (
            "dlq_id", "failed_at", "event_id", "consumer_name", "error_message",
            "retry_count", "payload_json",
        ), (dlq.dlq_id, dlq.failed_at, event_id, dlq.consumer_name,
            dlq.error_message, dlq.retry_count, dlq.to_json()))

    def _sync_insert_dead_letter(self, dlq):
        conn = self._get_connection()
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            self._insert_dead_letter(dlq)

    async def insert_dead_letter(self, dlq: DeadLetterEnvelope) -> None:
        dlq = sanitize_dead_letter(dlq)
        async with self._lock:
            await self._offload(self._sync_insert_dead_letter, dlq)

    def _sync_register_consumer(self, name, replay):
        from persistence.privacy import token
        token(name, required=True)
        conn = self._get_connection()
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM consumers WHERE name = ?", (name,)).fetchone():
                return
            if replay:
                count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
                pending = conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
                if count + pending > self.outbox_maxsize:
                    raise AuditBackpressureError("Replay would exceed durable outbox capacity")
            conn.execute("INSERT INTO consumers(name) VALUES (?)", (name,))
            if replay:
                conn.execute("INSERT INTO outbox(event_id, consumer_name) SELECT event_id, ? FROM events", (name,))

    async def register_consumer(self, name: str, *, replay: bool = False) -> None:
        """Persist consumer identity. Optional replay is only on first registration."""
        async with self._lock:
            await self._offload(self._sync_register_consumer, name, replay)

    def _sync_append_with_outbox(self, events):
        rows = [self._event_row(event) for event in events]
        conn = self._get_connection()
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            incoming_bytes = sum(len(row[9]) for row in rows)
            curr_bytes = conn.execute(
                "SELECT COALESCE(SUM(LENGTH(payload_json)), 0) FROM events"
            ).fetchone()[0]
            if curr_bytes + incoming_bytes > self.settings.max_retained_logical_bytes:
                raise AuditBackpressureError("Logical payload quota exhausted")

            if self.db_path != ":memory:":
                try:
                    db_dir = os.path.dirname(os.path.abspath(self.db_path))
                    free_bytes = shutil.disk_usage(db_dir).free
                    if free_bytes < self.settings.min_free_bytes:
                        raise AuditBackpressureError("Insufficient disk free space")
                except OSError:
                    pass

            consumers = [r[0] for r in conn.execute("SELECT name FROM consumers WHERE status != 'RETIRED'")]
            pending = conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
            inserted = 0
            for row in rows:
                if self._insert_event_row(row):
                    pending += len(consumers)
                    if pending > self.outbox_maxsize:
                        raise AuditBackpressureError("Durable audit outbox capacity exhausted")
                    conn.executemany("INSERT INTO outbox(event_id, consumer_name) VALUES (?, ?)",
                                     [(row[0], name) for name in consumers])
                    inserted += 1
            return inserted

    def _sync_seal_run(self, run_id: str, verification_status: Optional[str] = None) -> None:
        conn = self._get_connection()
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT lifecycle FROM audit_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown run_id: '{run_id}'")
            if row[0] == "EXPIRED":
                raise ValueError(f"Cannot seal expired run: '{run_id}'")
            now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            conn.execute(
                "UPDATE audit_runs SET lifecycle = 'SEALED', verification_status = ?, sealed_at = ? WHERE run_id = ?",
                (verification_status, now_iso, run_id),
            )

    async def seal_run(self, run_id: str, verification_status: Optional[str] = None) -> None:
        """Seal an active run, fixing verification status and closing it to new appends."""
        async with self._lock:
            await self._offload(self._sync_seal_run, run_id, verification_status)

    async def append_with_outbox(self, events: List[ActionEventEnvelope]) -> int:
        """Commit evidence and all registered analytics deliveries atomically.

        Only a file-backed store survives restart. A failure must prevent a
        caller's critical dispatch; this store cannot enforce that for callers.
        """
        if self.db_path in (":memory:", ""):
            raise ValueError("Durable evidence requires a file-backed database")
        events = [sanitize_event(event) for event in events]
        async with self._lock:
            return await self._offload(self._sync_append_with_outbox, events)

    def _sync_claim_delivery(self, names, lease_seconds, max_retries):
        if not names:
            return None
        conn = self._get_connection()
        now = time.time()
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            placeholders = ",".join("?" for _ in names)
            row = conn.execute(f"""SELECT o.event_id, o.consumer_name, o.attempts, e.payload_json
                FROM outbox o JOIN events e USING(event_id)
                JOIN consumers c ON c.name = o.consumer_name
                WHERE o.consumer_name IN ({placeholders})
                  AND c.status = 'ACTIVE'
                  AND o.available_at <= ? AND o.lease_until <= ?
                  AND NOT EXISTS (
                    SELECT 1 FROM outbox prior
                    JOIN events pe ON pe.event_id = prior.event_id
                    WHERE prior.consumer_name = o.consumer_name
                      AND pe.session_id = e.session_id AND pe.seq < e.seq
                  )
                ORDER BY e.rowid, o.consumer_name LIMIT 1""", [*names, now, now]).fetchone()
            if row is None:
                return None
            try:
                event = sanitize_event(ActionEventEnvelope.from_json(row["payload_json"]))
            except Exception as e:
                conn.execute("DELETE FROM outbox WHERE event_id = ? AND consumer_name = ?",
                             (row["event_id"], row["consumer_name"]))
                try:
                    self._insert_dead_letter(DeadLetterEnvelope(
                        event={"event_id": row["event_id"], "corrupted_payload": True},
                        consumer_name=row["consumer_name"],
                        error_message="CONSUMER_DELIVERY_FAILED",
                        retry_count=row["attempts"],
                    ))
                except Exception:
                    pass
                return None
            if row["attempts"] > max_retries:
                self._insert_dead_letter(DeadLetterEnvelope(event, row["consumer_name"],
                    "CONSUMER_DELIVERY_FAILED", retry_count=max(0, row["attempts"] - 1)))
                conn.execute("DELETE FROM outbox WHERE event_id = ? AND consumer_name = ?",
                             (row["event_id"], row["consumer_name"]))
                return None
            lease_id = str(uuid.uuid4())
            conn.execute("UPDATE outbox SET attempts = attempts + 1, lease_id = ?, lease_until = ? WHERE event_id = ? AND consumer_name = ?",
                         (lease_id, now + lease_seconds, row["event_id"], row["consumer_name"]))
            return (event,
                    row["consumer_name"], row["attempts"], lease_id)

    async def claim_delivery(self, names: List[str], *, lease_seconds: float = 30, max_retries: int = 3):
        if (isinstance(lease_seconds, bool) or not isinstance(lease_seconds, (int, float))
                or not math.isfinite(lease_seconds) or not 0 < lease_seconds <= 3600):
            raise ValueError("Invalid delivery lease duration")
        if isinstance(max_retries, bool) or not isinstance(max_retries, int) or not 0 <= max_retries <= 100:
            raise ValueError("Invalid delivery retry limit")
        async with self._lock:
            return await self._offload(self._sync_claim_delivery, names, lease_seconds, max_retries)

    def _sync_finish_delivery(self, event, name, lease_id, failed, max_retries, delay):
        conn = self._get_connection()
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT attempts FROM outbox WHERE event_id = ? AND consumer_name = ? AND lease_id = ?",
                               (event.event_id, name, lease_id)).fetchone()
            if row is None:
                return
            if not failed:
                conn.execute("DELETE FROM outbox WHERE event_id = ? AND consumer_name = ? AND lease_id = ?",
                             (event.event_id, name, lease_id))
            elif row[0] > max_retries:
                self._insert_dead_letter(DeadLetterEnvelope(event=event, consumer_name=name,
                    error_message="CONSUMER_DELIVERY_FAILED", retry_count=max(0, row[0] - 1)))
                conn.execute("DELETE FROM outbox WHERE event_id = ? AND consumer_name = ? AND lease_id = ?",
                             (event.event_id, name, lease_id))
            else:
                conn.execute("""UPDATE outbox SET available_at = ?,
                    lease_id = NULL, lease_until = 0
                    WHERE event_id = ? AND consumer_name = ? AND lease_id = ?""",
                    (time.time() + delay, event.event_id, name, lease_id))

    async def finish_delivery(self, event, name, lease_id, *, failed=False, max_retries=3, delay=0.02):
        if (isinstance(max_retries, bool) or not isinstance(max_retries, int) or not 0 <= max_retries <= 100
                or isinstance(delay, bool) or not isinstance(delay, (int, float))
                or not math.isfinite(delay) or not 0 <= delay <= 3600):
            raise ValueError("Invalid delivery retry configuration")
        async with self._lock:
            await self._offload(self._sync_finish_delivery, event, name, lease_id, failed, max_retries, delay)

    def _sync_commit_consumer_result(
        self, event_id: str, consumer_name: str, lease_id: str, result: ConsumerResult
    ) -> bool:
        conn = self._get_connection()
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT attempts FROM outbox WHERE event_id = ? AND consumer_name = ? AND lease_id = ?",
                (event_id, consumer_name, lease_id),
            ).fetchone()
            if row is None:
                return False

            for alert in result.alerts:
                alert = sanitize_alert(alert)
                self._insert_immutable(
                    "alerts",
                    (
                        "alert_id", "ts", "severity", "rule", "agent_id", "session_id",
                        "case_id", "action_taken", "evidence_json",
                    ),
                    (
                        alert.alert_id, alert.ts, alert.severity.value, alert.rule,
                        alert.agent_id, alert.session_id, alert.case_id, alert.action_taken,
                        json.dumps(alert.evidence, sort_keys=True),
                    ),
                )

            conn.execute(
                "INSERT OR REPLACE INTO consumer_completions (consumer_name, event_id, completed_at) VALUES (?, ?, ?)",
                (consumer_name, event_id, time.time()),
            )
            conn.execute(
                "DELETE FROM outbox WHERE event_id = ? AND consumer_name = ? AND lease_id = ?",
                (event_id, consumer_name, lease_id),
            )
            return True

    async def commit_consumer_result(
        self, event_id: str, consumer_name: str, lease_id: str, result: ConsumerResult
    ) -> bool:
        """Atomically commit consumer derived alerts and mark delivery completed."""
        async with self._lock:
            return await self._offload(
                self._sync_commit_consumer_result, event_id, consumer_name, lease_id, result
            )

    def _sync_pause_consumer(self, name: str) -> None:
        conn = self._get_connection()
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE consumers SET status = 'PAUSED' WHERE name = ?", (name,))

    async def pause_consumer(self, name: str) -> None:
        """Pause scheduling new deliveries for a consumer."""
        async with self._lock:
            await self._offload(self._sync_pause_consumer, name)

    def _sync_resume_consumer(self, name: str) -> None:
        conn = self._get_connection()
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE consumers SET status = 'ACTIVE' WHERE name = ?", (name,))

    async def resume_consumer(self, name: str) -> None:
        """Resume scheduling deliveries for a paused consumer."""
        async with self._lock:
            await self._offload(self._sync_resume_consumer, name)

    def _sync_retire_consumer(self, name: str, intervention_id: str) -> None:
        conn = self._get_connection()
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE consumers SET status = 'RETIRED' WHERE name = ?", (name,))
            pending_rows = conn.execute(
                "SELECT o.event_id, e.payload_json FROM outbox o JOIN events e USING(event_id) WHERE o.consumer_name = ?",
                (name,),
            ).fetchall()
            for r in pending_rows:
                event = sanitize_event(ActionEventEnvelope.from_json(r[1]))
                dlq = DeadLetterEnvelope(
                    event=event,
                    consumer_name=name,
                    error_message=f"CONSUMER_RETIRED: {intervention_id}",
                    retry_count=0,
                )
                self._insert_dead_letter(dlq)
            conn.execute("DELETE FROM outbox WHERE consumer_name = ?", (name,))

    async def retire_consumer(self, name: str, intervention_id: str) -> None:
        """Retire a consumer, transferring all remaining pending deliveries to the DLQ."""
        async with self._lock:
            await self._offload(self._sync_retire_consumer, name, intervention_id)

    def _sync_pending(self, names, active_only: bool = False):
        conn = self._get_connection()
        if names is None:
            if active_only:
                return conn.execute(
                    "SELECT COUNT(*) FROM outbox o JOIN consumers c ON o.consumer_name = c.name WHERE c.status = 'ACTIVE'"
                ).fetchone()[0]
            return conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
        if not names:
            return 0
        placeholders = ",".join("?" for _ in names)
        if active_only:
            return conn.execute(
                f"SELECT COUNT(*) FROM outbox o JOIN consumers c ON o.consumer_name = c.name WHERE o.consumer_name IN ({placeholders}) AND c.status = 'ACTIVE'",
                names,
            ).fetchone()[0]
        return conn.execute(f"SELECT COUNT(*) FROM outbox WHERE consumer_name IN ({placeholders})", names).fetchone()[0]

    async def pending_deliveries(
        self, names: Optional[List[str]] = None, active_only: bool = False
    ) -> int:
        async with self._lock:
            return await self._offload(self._sync_pending, names, active_only)

    def _sync_get_event(self, event_id: str) -> Optional[ActionEventEnvelope]:
        conn = self._get_connection()
        cursor = conn.execute(
            "SELECT payload_json FROM events WHERE event_id = ?",
            (event_id,),
        )
        row = cursor.fetchone()
        if not row:
            return None
        return sanitize_event(ActionEventEnvelope.from_json(row["payload_json"]))

    async def get_event(self, event_id: str) -> Optional[ActionEventEnvelope]:
        """Fetch a single action event by ID."""
        async with self._lock:
            return await self._offload(self._sync_get_event, event_id)

    def _sync_get_events_by_column(
        self, column: str, value: str
    ) -> List[ActionEventEnvelope]:
        conn = self._get_connection()
        cursor = conn.execute(
            f"SELECT payload_json FROM events WHERE {column} = ? ORDER BY ts ASC, rowid ASC",
            (value,),
        )
        rows = cursor.fetchall()
        return [sanitize_event(ActionEventEnvelope.from_json(r["payload_json"])) for r in rows]

    async def get_events_by_case(self, case_id: str) -> List[ActionEventEnvelope]:
        """Retrieve all events linked to a specific case_id ordered by timestamp."""
        async with self._lock:
            return await self._offload(
                self._sync_get_events_by_column, "case_id", case_id
            )

    async def get_events_by_trace(self, trace_id: str) -> List[ActionEventEnvelope]:
        """Retrieve all events linked to a specific trace_id ordered by timestamp."""
        async with self._lock:
            return await self._offload(
                self._sync_get_events_by_column, "trace_id", trace_id
            )

    async def get_events_by_session(self, session_id: str) -> List[ActionEventEnvelope]:
        """Retrieve all events linked to a specific session_id ordered by timestamp."""
        async with self._lock:
            return await self._offload(
                self._sync_get_events_by_column, "session_id", session_id
            )

    async def get_events_by_session_seq(self, session_id: str) -> List[ActionEventEnvelope]:
        """Read persisted session history in Layer 2 sequence order."""
        async with self._lock:
            def read():
                rows = self._get_connection().execute(
                    "SELECT payload_json FROM events WHERE session_id=? ORDER BY seq ASC",
                    (session_id,),
                ).fetchall()
                return [sanitize_event(ActionEventEnvelope.from_json(r[0])) for r in rows]
            return await self._offload(read)

    @staticmethod
    def _finding_projection(payload: Mapping[str, Any]) -> dict[str, Any]:
        """Persist only structured finding metadata; discard free-text summaries/details."""
        from persistence.privacy import number, token
        safe = {}
        for key in ("rule_id", "plugin", "plugin_version", "method", "run_id", "session_id",
                    "agent_id", "case_id", "trigger_event_id", "policy_version"):
            if payload.get(key) is not None:
                safe[key] = token(payload[key], required=True)
        severity = payload.get("severity")
        if severity not in {"low", "medium", "high", "critical"}:
            raise ValueError("Invalid finding severity")
        safe["severity"] = severity
        ids = payload.get("evidence_event_ids", ())
        if not isinstance(ids, (list, tuple)):
            raise ValueError("Finding evidence_event_ids must be a sequence")
        safe["evidence_event_ids"] = [token(x, required=True) for x in ids]
        confidence = payload.get("confidence")
        if confidence is not None:
            confidence = number(confidence)
            if confidence > 1:
                raise ValueError("Finding confidence must be between 0 and 1")
            safe["confidence"] = confidence
        return safe

    async def write_consumer_finding(self, finding_id: str, payload: Mapping[str, Any]) -> bool:
        """Store a privacy-projected finding idempotently; return True on first insert."""
        from persistence.privacy import token
        finding_id = token(finding_id, required=True)
        safe = self._finding_projection(payload)
        session_id = safe.get("session_id")
        if not session_id:
            raise ValueError("Finding requires session_id")
        raw = json.dumps(safe, sort_keys=True, separators=(",", ":"))
        async with self._lock:
            def write():
                conn = self._get_connection()
                with conn:
                    conn.execute("BEGIN IMMEDIATE")
                    old = conn.execute("SELECT session_id,payload_json FROM consumer_findings WHERE finding_id=?", (finding_id,)).fetchone()
                    if old:
                        if (old[0], old[1]) != (session_id, raw):
                            raise ConflictingRecordError("Conflicting finding ID")
                        return False
                    conn.execute("INSERT INTO consumer_findings VALUES(?,?,?)", (finding_id,session_id,raw))
                    return True
            return await self._offload(write)

    async def list_consumer_findings(self, session_id: str) -> list[dict[str, Any]]:
        async with self._lock:
            def read():
                rows = self._get_connection().execute(
                    "SELECT payload_json FROM consumer_findings WHERE session_id=? ORDER BY rowid",
                    (session_id,),
                ).fetchall()
                return [json.loads(r[0]) for r in rows]
            return await self._offload(read)

    @staticmethod
    def _verification_projection(payload: Mapping[str, Any]) -> dict[str, Any]:
        from persistence.privacy import token
        status = payload.get("verification_status")
        if status not in {"VERIFIED_SUCCESS", "FAILED_POSTCONDITIONS", "VERIFICATION_INCOMPLETE"}:
            raise ValueError("Invalid verification_status")
        checks = []
        for check in payload.get("checks", ()):
            if not isinstance(check, Mapping):
                raise ValueError("Verification checks must be objects")
            check_status = check.get("status")
            if check_status not in {"PASS", "FAIL", "INCOMPLETE"}:
                raise ValueError("Invalid verification check status")
            item = {"id": token(check.get("id"), required=True), "status": check_status}
            if check.get("detail") is not None:
                # Verifier details must be fixed opaque reason codes. This
                # rejects prose that could contain PII or raw exception text.
                item["detail"] = token(check["detail"], required=True)
            if check.get("evidence_source") is not None:
                item["evidence_source"] = token(check["evidence_source"], required=True)
            checks.append(item)
        return {"verification_status": status, "checks": checks}

    async def write_verification(self, session_id: str, payload: Mapping[str, Any]) -> bool:
        from persistence.privacy import token
        session_id = token(session_id, required=True)
        safe = self._verification_projection(payload)
        raw = json.dumps(safe, sort_keys=True, separators=(",", ":"))
        async with self._lock:
            def write():
                conn = self._get_connection()
                with conn:
                    conn.execute("BEGIN IMMEDIATE")
                    old = conn.execute("SELECT payload_json FROM verification_results WHERE session_id=?", (session_id,)).fetchone()
                    if old:
                        if old[0] != raw:
                            raise ConflictingRecordError("Conflicting verification result for session")
                        return False
                    conn.execute("INSERT INTO verification_results VALUES(?,?)", (session_id,raw))
                    return True
            return await self._offload(write)

    async def get_verification(self, session_id: str) -> dict[str, Any] | None:
        async with self._lock:
            def read():
                row = self._get_connection().execute(
                    "SELECT payload_json FROM verification_results WHERE session_id=?", (session_id,)
                ).fetchone()
                return json.loads(row[0]) if row else None
            return await self._offload(read)

    def _sync_get_run_events(self, run_id):
        rows = self._get_connection().execute("""SELECT e.payload_json
            FROM run_order r JOIN events e USING(event_id)
            WHERE r.run_id = ? ORDER BY r.action_index""", (run_id,)).fetchall()
        return [sanitize_event(ActionEventEnvelope.from_json(row[0])) for row in rows]

    async def get_run_events(self, run_id: str) -> List[ActionEventEnvelope]:
        """Read trusted gateway-supplied event order, independent of wall-clock time.

        The gateway assigns a unique index per event (intent/result have separate
        indices and share action_id). This API does not authenticate that gateway.
        """
        async with self._lock:
            return await self._offload(self._sync_get_run_events, run_id)

    def _sync_get_recent_alerts(self, limit: int) -> List[AlertEvent]:
        conn = self._get_connection()
        cursor = conn.execute(
            """
            SELECT alert_id, ts, severity, rule, agent_id, session_id,
                   case_id, action_taken, evidence_json
            FROM alerts
            ORDER BY ts DESC, rowid DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = cursor.fetchall()
        results: List[AlertEvent] = []
        for r in rows:
            evidence = json.loads(r["evidence_json"]) if r["evidence_json"] else {}
            results.append(
                AlertEvent(
                    alert_id=r["alert_id"],
                    ts=r["ts"],
                    severity=Severity(r["severity"]),
                    rule=r["rule"],
                    agent_id=r["agent_id"],
                    session_id=r["session_id"],
                    case_id=r["case_id"],
                    action_taken=r["action_taken"],
                    evidence=evidence,
                )
            )
        return [sanitize_alert(alert) for alert in results]

    async def get_recent_alerts(self, limit: int = 50) -> List[AlertEvent]:
        """Retrieve recent alerts ordered by timestamp descending."""
        self._validate_page(limit)
        async with self._lock:
            return await self._offload(self._sync_get_recent_alerts, limit)

    def _sync_get_audit_actions(
        self, run_id: Optional[str] = None, session_id: Optional[str] = None
    ) -> List[AuditActionRecord]:
        conn = self._get_connection()
        clauses = []
        params = []
        if run_id:
            clauses.append("run_id = ?")
            params.append(run_id)
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"""
            SELECT action_id, ts, run_id, session_id, agent_id, tool_name,
                   target_id, side_effect_class, status, details_json
            FROM audit_actions
            {where}
            ORDER BY ts ASC, rowid ASC
        """
        cursor = conn.execute(query, params)
        rows = cursor.fetchall()
        results: List[AuditActionRecord] = []
        for r in rows:
            details = json.loads(r["details_json"]) if r["details_json"] else {}
            results.append(
                AuditActionRecord(
                    action_id=r["action_id"],
                    ts=r["ts"],
                    run_id=r["run_id"],
                    session_id=r["session_id"],
                    agent_id=r["agent_id"],
                    tool_name=r["tool_name"],
                    target_id=r["target_id"],
                    side_effect_class=r["side_effect_class"],
                    status=r["status"],
                    details=details,
                )
            )
        return [sanitize_audit(action) for action in results]

    async def get_audit_actions(
        self, run_id: Optional[str] = None, session_id: Optional[str] = None
    ) -> List[AuditActionRecord]:
        """Retrieve audit actions filtered optionally by run_id or session_id."""
        async with self._lock:
            return await self._offload(self._sync_get_audit_actions, run_id, session_id)

    def _sync_get_dlq_records(self, limit: int) -> List[DeadLetterEnvelope]:
        conn = self._get_connection()
        cursor = conn.execute(
            """
            SELECT payload_json
            FROM dead_letter_queue
            ORDER BY failed_at DESC, rowid DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = cursor.fetchall()
        return [sanitize_dead_letter(DeadLetterEnvelope.from_json(r["payload_json"])) for r in rows]

    async def get_dlq_records(self, limit: int = 50) -> List[DeadLetterEnvelope]:
        """Retrieve recent dead-letter records."""
        self._validate_page(limit)
        async with self._lock:
            return await self._offload(self._sync_get_dlq_records, limit)

    def _sync_query_events(
        self,
        filter_dict: Optional[Dict[str, Any]],
        limit: int,
        offset: int,
    ) -> List[ActionEventEnvelope]:
        conn = self._get_connection()
        clauses = []
        params: List[Any] = []

        if filter_dict:
            allowed_cols = {
                "trace_id",
                "session_id",
                "case_id",
                "agent_id",
                "action_type",
                "status",
            }
            for k, v in filter_dict.items():
                if k in allowed_cols and v is not None:
                    clauses.append(f"{k} = ?")
                    # Handle Enum values
                    val = v.value if hasattr(v, "value") else str(v)
                    params.append(val)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"""
            SELECT payload_json
            FROM events
            {where}
            ORDER BY ts ASC, rowid ASC
            LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])
        cursor = conn.execute(query, params)
        rows = cursor.fetchall()
        return [sanitize_event(ActionEventEnvelope.from_json(r["payload_json"])) for r in rows]

    async def query_events(
        self,
        filter_dict: Optional[Dict[str, Any]] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[ActionEventEnvelope]:
        """Flexible query on events with optional filters, limit, and offset."""
        self._validate_page(limit, offset)
        if filter_dict and set(filter_dict) - {"trace_id", "session_id", "case_id", "agent_id", "action_type", "status"}:
            raise ValueError("Unsupported event filter")
        async with self._lock:
            return await self._offload(
                self._sync_query_events, filter_dict, limit, offset
            )

    @staticmethod
    def _validate_page(limit, offset=0):
        if (not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10000
                or not isinstance(offset, int) or isinstance(offset, bool) or offset < 0):
            raise ValueError("Invalid pagination bounds")

    def _sync_prune(self, before):
        from persistence.privacy import timestamp
        before = timestamp(before)
        conn = self._get_connection()
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""DELETE FROM consumer_completions WHERE event_id IN
                (SELECT event_id FROM events WHERE ts < ? AND NOT EXISTS
                    (SELECT 1 FROM outbox WHERE outbox.event_id = events.event_id))""", (before,))
            deleted = conn.execute("""DELETE FROM events WHERE ts < ? AND NOT EXISTS
                (SELECT 1 FROM outbox WHERE outbox.event_id = events.event_id)""", (before,)).rowcount
            conn.execute("DELETE FROM alerts WHERE ts < ?", (before,))
            conn.execute("DELETE FROM audit_actions WHERE ts < ?", (before,))
            conn.execute("DELETE FROM dead_letter_queue WHERE failed_at < ?", (before,))
            return deleted

    async def prune_before(self, before: str) -> int:
        """Apply an operator-selected retention cutoff; protect pending deliveries.

        This trusted storage API has no public/admin endpoint. Integrations must
        authorize retention/export access separately. No automatic deletion of
        evidence occurs by default.
        """
        async with self._lock:
            return await self._offload(self._sync_prune, before)

    def _sync_get_stats(self) -> Dict[str, Any]:
        conn = self._get_connection()

        # Total counts
        events_count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        alerts_count = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
        audit_count = conn.execute("SELECT COUNT(*) FROM audit_actions").fetchone()[0]
        dlq_count = conn.execute("SELECT COUNT(*) FROM dead_letter_queue").fetchone()[0]

        # Events by action_type
        type_cursor = conn.execute(
            "SELECT action_type, COUNT(*) as cnt FROM events GROUP BY action_type"
        )
        events_by_type = {r["action_type"]: r["cnt"] for r in type_cursor.fetchall()}

        # Events by status
        status_cursor = conn.execute(
            "SELECT status, COUNT(*) as cnt FROM events GROUP BY status"
        )
        events_by_status = {r["status"]: r["cnt"] for r in status_cursor.fetchall()}

        # Alerts by severity
        sev_cursor = conn.execute(
            "SELECT severity, COUNT(*) as cnt FROM alerts GROUP BY severity"
        )
        alerts_by_severity = {r["severity"]: r["cnt"] for r in sev_cursor.fetchall()}

        # Average latency
        avg_lat_row = conn.execute("SELECT AVG(total_latency_ms) FROM events").fetchone()
        avg_latency = float(avg_lat_row[0]) if avg_lat_row and avg_lat_row[0] is not None else 0.0

        return {
            "total_events": events_count,
            "total_alerts": alerts_count,
            "total_audit_actions": audit_count,
            "total_dlq_records": dlq_count,
            "events_by_type": events_by_type,
            "events_by_status": events_by_status,
            "alerts_by_severity": alerts_by_severity,
            "average_latency_ms": round(avg_latency, 3),
        }

    async def get_stats(self) -> Dict[str, Any]:
        """Collect aggregate statistics across stored events, alerts, and audit actions."""
        async with self._lock:
            return await self._offload(self._sync_get_stats)
