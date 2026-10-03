"""EventStore implementation using standard library sqlite3 and asyncio.to_thread.

Provides an append-optimized, indexed relational store for action envelopes,
security alerts, audit action records, and dead letter queue messages.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from persistence.models import (
    ActionEventEnvelope,
    AlertEvent,
    AuditActionRecord,
    DeadLetterEnvelope,
    Severity,
)
from persistence.privacy import sanitize_event, sanitize_alert, sanitize_audit, sanitize_dead_letter


class ConflictingRecordError(ValueError):
    """An existing immutable record ID was reused with different evidence."""


class AuditBackpressureError(RuntimeError):
    """The bounded durable outbox is full; callers must pause critical dispatch."""


class EventStore:
    """Asynchronous SQLite event store with WAL mode and threadpool offloading."""

    def __init__(self, db_path: Union[str, Path] = ":memory:", *, outbox_maxsize: int = 10000) -> None:
        if isinstance(outbox_maxsize, bool) or not isinstance(outbox_maxsize, int) or outbox_maxsize <= 0:
            raise ValueError("outbox_maxsize must be positive")
        self.outbox_maxsize = outbox_maxsize
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
        previous = conn.execute(
            f"SELECT {', '.join(columns)} FROM {table} WHERE {key} = ?", (values[0],)
        ).fetchone()
        if previous is not None:
            if tuple(previous) != tuple(values):
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

        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row

        # Concurrency & Performance PRAGMAs
        if self.db_path != ":memory:":
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA busy_timeout=5000")

        # Schema: events
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                trace_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                case_id TEXT,
                agent_id TEXT NOT NULL,
                action_type TEXT NOT NULL,
                status TEXT NOT NULL,
                ts TEXT NOT NULL,
                total_latency_ms REAL NOT NULL DEFAULT 0.0,
                payload_json TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_trace_ts ON events(trace_id, ts)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_session_ts ON events(session_id, ts)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_case_ts ON events(case_id, ts)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_type_status ON events(action_type, status)"
        )

        # Schema: alerts
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS alerts (
                alert_id TEXT PRIMARY KEY,
                ts TEXT NOT NULL,
                severity TEXT NOT NULL,
                rule TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                case_id TEXT,
                action_taken TEXT NOT NULL,
                evidence_json TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_alerts_session_ts ON alerts(session_id, ts)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_alerts_case_ts ON alerts(case_id, ts)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts)"
        )

        # Schema: audit_actions
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_actions (
                action_id TEXT PRIMARY KEY,
                ts TEXT NOT NULL,
                run_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                target_id TEXT,
                side_effect_class TEXT NOT NULL,
                status TEXT NOT NULL,
                details_json TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_run_session ON audit_actions(run_id, session_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_session_ts ON audit_actions(session_id, ts)"
        )

        # Schema: dead_letter_queue
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dead_letter_queue (
                dlq_id TEXT PRIMARY KEY,
                failed_at TEXT NOT NULL,
                event_id TEXT,
                consumer_name TEXT NOT NULL,
                error_message TEXT NOT NULL,
                retry_count INTEGER NOT NULL DEFAULT 0,
                payload_json TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_dlq_consumer ON dead_letter_queue(consumer_name)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_dlq_failed_at ON dead_letter_queue(failed_at)"
        )

        conn.execute("CREATE TABLE IF NOT EXISTS consumers (name TEXT PRIMARY KEY)")
        conn.execute("""CREATE TABLE IF NOT EXISTS run_order (
            run_id TEXT NOT NULL,
            action_index INTEGER NOT NULL,
            event_id TEXT NOT NULL UNIQUE REFERENCES events(event_id) ON DELETE CASCADE,
            PRIMARY KEY (run_id, action_index)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS outbox (
            event_id TEXT NOT NULL REFERENCES events(event_id),
            consumer_name TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            available_at REAL NOT NULL DEFAULT 0,
            lease_id TEXT,
            lease_until REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (event_id, consumer_name)
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_outbox_due ON outbox(available_at, lease_until)")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.commit()
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

    def _insert_event_row(self, row):
        inserted = self._insert_immutable("events", (
            "event_id", "trace_id", "session_id", "case_id", "agent_id",
            "action_type", "status", "ts", "total_latency_ms", "payload_json",
        ), row)
        if inserted:
            context = json.loads(row[-1])["context"]
            if context["run_id"] is not None:
                previous = self._get_connection().execute(
                    "SELECT event_id FROM run_order WHERE run_id = ? AND action_index = ?",
                    (context["run_id"], context["action_index"])).fetchone()
                if previous is not None:
                    raise ConflictingRecordError("Run action index already recorded")
                self._get_connection().execute(
                    "INSERT INTO run_order(run_id, action_index, event_id) VALUES (?, ?, ?)",
                    (context["run_id"], context["action_index"], row[0]))
        return inserted

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
            consumers = [r[0] for r in conn.execute("SELECT name FROM consumers")]
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
                WHERE o.consumer_name IN ({placeholders})
                  AND o.available_at <= ? AND o.lease_until <= ?
                ORDER BY e.rowid, o.consumer_name LIMIT 1""", [*names, now, now]).fetchone()
            if row is None:
                return None
            event = sanitize_event(ActionEventEnvelope.from_json(row["payload_json"]))
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

    def _sync_pending(self, names):
        conn = self._get_connection()
        if names is None:
            return conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
        if not names:
            return 0
        placeholders = ",".join("?" for _ in names)
        return conn.execute(f"SELECT COUNT(*) FROM outbox WHERE consumer_name IN ({placeholders})", names).fetchone()[0]

    async def pending_deliveries(self, names: Optional[List[str]] = None) -> int:
        async with self._lock:
            return await self._offload(self._sync_pending, names)

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
