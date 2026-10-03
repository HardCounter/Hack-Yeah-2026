"""EventStore implementation using standard library sqlite3 and asyncio.to_thread.

Provides an append-optimized, indexed relational store for action envelopes,
security alerts, audit action records, and dead letter queue messages.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from persistence.models import (
    ActionEventEnvelope,
    AlertEvent,
    AuditActionRecord,
    DeadLetterEnvelope,
    Severity,
)


class EventStore:
    """Asynchronous SQLite event store with WAL mode and threadpool offloading."""

    def __init__(self, db_path: Union[str, Path] = ":memory:") -> None:
        self.db_path = str(db_path)
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = asyncio.Lock()
        self._initialized = False

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
        conn.execute("PRAGMA synchronous=NORMAL")
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

        conn.commit()
        self._conn = conn
        self._initialized = True

    async def initialize(self) -> None:
        """Initialize SQLite tables and indexes."""
        async with self._lock:
            if not self._initialized:
                await asyncio.to_thread(self._sync_initialize)

    def _sync_close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
            self._initialized = False

    async def close(self) -> None:
        """Close SQLite database connection."""
        async with self._lock:
            if self._conn is not None:
                await asyncio.to_thread(self._sync_close)

    def _sync_insert_event(self, event: ActionEventEnvelope) -> None:
        conn = self._get_connection()
        total_latency = event.interception_metadata.total_latency_ms
        payload = event.to_json()
        with conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO events (
                    event_id, trace_id, session_id, case_id, agent_id,
                    action_type, status, ts, total_latency_ms, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.trace_id,
                    event.session_id,
                    event.case_id,
                    event.agent_id,
                    event.action_type.value,
                    event.status.value,
                    event.ts,
                    total_latency,
                    payload,
                ),
            )

    async def insert_event(self, event: ActionEventEnvelope) -> None:
        """Insert a single action event envelope."""
        async with self._lock:
            await asyncio.to_thread(self._sync_insert_event, event)

    def _sync_insert_events_batch(self, events: List[ActionEventEnvelope]) -> int:
        if not events:
            return 0
        conn = self._get_connection()
        rows = [
            (
                ev.event_id,
                ev.trace_id,
                ev.session_id,
                ev.case_id,
                ev.agent_id,
                ev.action_type.value,
                ev.status.value,
                ev.ts,
                ev.interception_metadata.total_latency_ms,
                ev.to_json(),
            )
            for ev in events
        ]
        with conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO events (
                    event_id, trace_id, session_id, case_id, agent_id,
                    action_type, status, ts, total_latency_ms, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        return len(events)

    async def insert_events_batch(self, events: List[ActionEventEnvelope]) -> int:
        """Batch insert multiple action events in a single transaction."""
        async with self._lock:
            return await asyncio.to_thread(self._sync_insert_events_batch, events)

    def _sync_insert_alert(self, alert: AlertEvent) -> None:
        conn = self._get_connection()
        evidence_json = json.dumps(alert.evidence)
        with conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO alerts (
                    alert_id, ts, severity, rule, agent_id, session_id, case_id,
                    action_taken, evidence_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    alert.alert_id,
                    alert.ts,
                    alert.severity.value,
                    alert.rule,
                    alert.agent_id,
                    alert.session_id,
                    alert.case_id,
                    alert.action_taken,
                    evidence_json,
                ),
            )

    async def insert_alert(self, alert: AlertEvent) -> None:
        """Insert a security alert."""
        async with self._lock:
            await asyncio.to_thread(self._sync_insert_alert, alert)

    def _sync_insert_audit_action(self, action: AuditActionRecord) -> None:
        conn = self._get_connection()
        details_json = json.dumps(action.details)
        with conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO audit_actions (
                    action_id, ts, run_id, session_id, agent_id, tool_name,
                    target_id, side_effect_class, status, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    action.action_id,
                    action.ts,
                    action.run_id,
                    action.session_id,
                    action.agent_id,
                    action.tool_name,
                    action.target_id,
                    action.side_effect_class,
                    action.status,
                    details_json,
                ),
            )

    async def insert_audit_action(self, action: AuditActionRecord) -> None:
        """Insert an audit action execution record."""
        async with self._lock:
            await asyncio.to_thread(self._sync_insert_audit_action, action)

    def _sync_insert_dead_letter(self, dlq: DeadLetterEnvelope) -> None:
        conn = self._get_connection()
        event_id = None
        if isinstance(dlq.event, ActionEventEnvelope):
            event_id = dlq.event.event_id
        elif isinstance(dlq.event, dict):
            event_id = dlq.event.get("event_id")

        payload_json = dlq.to_json()
        with conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO dead_letter_queue (
                    dlq_id, failed_at, event_id, consumer_name,
                    error_message, retry_count, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    dlq.dlq_id,
                    dlq.failed_at,
                    event_id,
                    dlq.consumer_name,
                    dlq.error_message,
                    dlq.retry_count,
                    payload_json,
                ),
            )

    async def insert_dead_letter(self, dlq: DeadLetterEnvelope) -> None:
        """Insert a dead letter record."""
        async with self._lock:
            await asyncio.to_thread(self._sync_insert_dead_letter, dlq)

    def _sync_get_event(self, event_id: str) -> Optional[ActionEventEnvelope]:
        conn = self._get_connection()
        cursor = conn.execute(
            "SELECT payload_json FROM events WHERE event_id = ?",
            (event_id,),
        )
        row = cursor.fetchone()
        if not row:
            return None
        return ActionEventEnvelope.from_json(row["payload_json"])

    async def get_event(self, event_id: str) -> Optional[ActionEventEnvelope]:
        """Fetch a single action event by ID."""
        async with self._lock:
            return await asyncio.to_thread(self._sync_get_event, event_id)

    def _sync_get_events_by_column(
        self, column: str, value: str
    ) -> List[ActionEventEnvelope]:
        conn = self._get_connection()
        cursor = conn.execute(
            f"SELECT payload_json FROM events WHERE {column} = ? ORDER BY ts ASC",
            (value,),
        )
        rows = cursor.fetchall()
        return [ActionEventEnvelope.from_json(r["payload_json"]) for r in rows]

    async def get_events_by_case(self, case_id: str) -> List[ActionEventEnvelope]:
        """Retrieve all events linked to a specific case_id ordered by timestamp."""
        async with self._lock:
            return await asyncio.to_thread(
                self._sync_get_events_by_column, "case_id", case_id
            )

    async def get_events_by_trace(self, trace_id: str) -> List[ActionEventEnvelope]:
        """Retrieve all events linked to a specific trace_id ordered by timestamp."""
        async with self._lock:
            return await asyncio.to_thread(
                self._sync_get_events_by_column, "trace_id", trace_id
            )

    async def get_events_by_session(self, session_id: str) -> List[ActionEventEnvelope]:
        """Retrieve all events linked to a specific session_id ordered by timestamp."""
        async with self._lock:
            return await asyncio.to_thread(
                self._sync_get_events_by_column, "session_id", session_id
            )

    def _sync_get_recent_alerts(self, limit: int) -> List[AlertEvent]:
        conn = self._get_connection()
        cursor = conn.execute(
            """
            SELECT alert_id, ts, severity, rule, agent_id, session_id,
                   case_id, action_taken, evidence_json
            FROM alerts
            ORDER BY ts DESC
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
        return results

    async def get_recent_alerts(self, limit: int = 50) -> List[AlertEvent]:
        """Retrieve recent alerts ordered by timestamp descending."""
        async with self._lock:
            return await asyncio.to_thread(self._sync_get_recent_alerts, limit)

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
            ORDER BY ts ASC
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
        return results

    async def get_audit_actions(
        self, run_id: Optional[str] = None, session_id: Optional[str] = None
    ) -> List[AuditActionRecord]:
        """Retrieve audit actions filtered optionally by run_id or session_id."""
        async with self._lock:
            return await asyncio.to_thread(self._sync_get_audit_actions, run_id, session_id)

    def _sync_get_dlq_records(self, limit: int) -> List[DeadLetterEnvelope]:
        conn = self._get_connection()
        cursor = conn.execute(
            """
            SELECT payload_json
            FROM dead_letter_queue
            ORDER BY failed_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = cursor.fetchall()
        return [DeadLetterEnvelope.from_json(r["payload_json"]) for r in rows]

    async def get_dlq_records(self, limit: int = 50) -> List[DeadLetterEnvelope]:
        """Retrieve recent dead-letter records."""
        async with self._lock:
            return await asyncio.to_thread(self._sync_get_dlq_records, limit)

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
            ORDER BY ts ASC
            LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])
        cursor = conn.execute(query, params)
        rows = cursor.fetchall()
        return [ActionEventEnvelope.from_json(r["payload_json"]) for r in rows]

    async def query_events(
        self,
        filter_dict: Optional[Dict[str, Any]] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[ActionEventEnvelope]:
        """Flexible query on events with optional filters, limit, and offset."""
        async with self._lock:
            return await asyncio.to_thread(
                self._sync_query_events, filter_dict, limit, offset
            )

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
            return await asyncio.to_thread(self._sync_get_stats)
