"""Audit schema versions, transactional migrations, and store metadata."""

from __future__ import annotations

import json
import sqlite3
import uuid

from persistence.settings import PersistenceSettings

CURRENT_SCHEMA_VERSION = 2


def migrate_audit_schema(conn: sqlite3.Connection, settings: PersistenceSettings) -> None:
    """Migrate the SQLite audit store to the current version.

    Runs within an immediate transaction. Rejects unsupported future schema versions.
    Initializes metadata, tables, indexes, and validates writer settings.
    """
    # 1. Inspect existing schema version
    cursor = conn.execute("PRAGMA user_version")
    current_version = cursor.fetchone()[0]

    if current_version > CURRENT_SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported future audit schema version {current_version}; maximum supported is {CURRENT_SCHEMA_VERSION}"
        )

    # Begin transactional migration
    with conn:
        conn.execute("BEGIN IMMEDIATE")

        # 2. Metadata table
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS store_metadata (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                epoch TEXT NOT NULL,
                next_offset INTEGER NOT NULL CHECK (next_offset >= 1),
                settings_json TEXT NOT NULL
            )
            """
        )

        row = conn.execute(
            "SELECT epoch, next_offset, settings_json FROM store_metadata WHERE singleton = 1"
        ).fetchone()

        if row is None:
            initial_epoch = uuid.uuid4().hex
            conn.execute(
                """
                INSERT INTO store_metadata (singleton, epoch, next_offset, settings_json)
                VALUES (1, ?, 1, ?)
                """,
                (initial_epoch, json.dumps(settings.to_dict(), sort_keys=True)),
            )
        else:
            try:
                stored_settings_dict = json.loads(row[2])
            except Exception as exc:
                raise ValueError("Corrupted settings in store metadata") from exc

            # Verify compatibility: outbox_maxsize cannot conflict
            stored_settings = PersistenceSettings.from_policy(stored_settings_dict)
            if stored_settings.outbox_maxsize != settings.outbox_maxsize:
                raise ValueError(
                    f"Conflicting simultaneous writer settings: outbox_maxsize {settings.outbox_maxsize} "
                    f"conflicts with active store setting {stored_settings.outbox_maxsize}"
                )

        # 3. Base audit tables
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
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_trace_ts ON events(trace_id, ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_session_ts ON events(session_id, ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_case_ts ON events(case_id, ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_type_status ON events(action_type, status)")

        # Ingest offset column & unique index on events
        table_info = [col[1] for col in conn.execute("PRAGMA table_info(events)").fetchall()]
        if "ingest_offset" not in table_info:
            conn.execute("ALTER TABLE events ADD COLUMN ingest_offset INTEGER")
            conn.execute("UPDATE events SET ingest_offset = rowid WHERE ingest_offset IS NULL")

        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_events_ingest_offset ON events(ingest_offset)")

        # Ensure next_offset is greater than any existing ingest_offset
        conn.execute(
            """
            UPDATE store_metadata
            SET next_offset = MAX(
                COALESCE((SELECT MAX(ingest_offset) FROM events), 0) + 1,
                next_offset
            )
            WHERE singleton = 1
            """
        )

        # Alerts
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
        conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_session_ts ON alerts(session_id, ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_case_ts ON alerts(case_id, ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts)")

        # Audit actions
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
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_run_session ON audit_actions(run_id, session_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_session_ts ON audit_actions(session_id, ts)")

        # Dead letter queue
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
        conn.execute("CREATE INDEX IF NOT EXISTS idx_dlq_consumer ON dead_letter_queue(consumer_name)")

        # Consumers and Outbox
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS consumers (
                name TEXT PRIMARY KEY,
                registered_at REAL NOT NULL DEFAULT (strftime('%s', 'now')),
                status TEXT NOT NULL DEFAULT 'ACTIVE'
            )
            """
        )
        consumer_cols = [col[1] for col in conn.execute("PRAGMA table_info(consumers)").fetchall()]
        if "status" not in consumer_cols and "name" in consumer_cols:
            conn.execute("ALTER TABLE consumers ADD COLUMN status TEXT NOT NULL DEFAULT 'ACTIVE'")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS consumer_completions (
                consumer_name TEXT NOT NULL,
                event_id TEXT NOT NULL REFERENCES events(event_id),
                completed_at REAL NOT NULL,
                PRIMARY KEY (consumer_name, event_id)
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS outbox (
                event_id TEXT NOT NULL REFERENCES events(event_id),
                consumer_name TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                available_at REAL NOT NULL DEFAULT 0,
                lease_id TEXT,
                lease_until REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (event_id, consumer_name)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_outbox_due ON outbox(available_at, lease_until)")

        # Run order table
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS run_order (
                run_id TEXT NOT NULL,
                action_index INTEGER NOT NULL,
                event_id TEXT NOT NULL UNIQUE REFERENCES events(event_id) ON DELETE CASCADE,
                PRIMARY KEY (run_id, action_index)
            )
            """
        )

        # Run index protection table (from Task 2)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_run_indices (
                run_id TEXT NOT NULL,
                action_index INTEGER NOT NULL,
                event_id TEXT NOT NULL REFERENCES events(event_id),
                PRIMARY KEY (run_id, action_index)
            )
            """
        )

        # Audit runs table (from Task 2)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_runs (
                run_id TEXT PRIMARY KEY,
                binding_json TEXT NOT NULL,
                next_index INTEGER NOT NULL DEFAULT 0,
                lifecycle TEXT NOT NULL CHECK (lifecycle IN ('ACTIVE','SEALED','EXPIRED')),
                verification_status TEXT,
                sealed_at TEXT,
                expired_at TEXT
            )
            """
        )
        audit_run_cols = [col[1] for col in conn.execute("PRAGMA table_info(audit_runs)").fetchall()]
        if "verification_status" not in audit_run_cols and "run_id" in audit_run_cols:
            conn.execute("ALTER TABLE audit_runs ADD COLUMN verification_status TEXT")
        if "sealed_at" not in audit_run_cols and "run_id" in audit_run_cols:
            conn.execute("ALTER TABLE audit_runs ADD COLUMN sealed_at TEXT")
        if "expired_at" not in audit_run_cols and "run_id" in audit_run_cols:
            conn.execute("ALTER TABLE audit_runs ADD COLUMN expired_at TEXT")

        # Audit read grants and export holds (from Tasks 5 & 6)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_read_grants (
                run_id TEXT NOT NULL REFERENCES audit_runs(run_id),
                principal_id TEXT NOT NULL,
                intervention_id TEXT NOT NULL,
                PRIMARY KEY (run_id, principal_id)
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_export_holds (
                hold_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES audit_runs(run_id),
                principal_id TEXT NOT NULL,
                high_watermark INTEGER NOT NULL,
                expires_at TEXT NOT NULL
            )
            """
        )

        # Set user_version to current schema version
        conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
