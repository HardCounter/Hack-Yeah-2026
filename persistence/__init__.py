"""Layer 2: Persistence Layer for AI Control Layer.

Sanitized immutable audit records and atomic SQLite outbox commits. Durable
analytics are at least once; volatile live feeds and nowait telemetry are
explicitly best effort. Critical dispatch must await the file-backed store.
"""

from persistence.models import (
    ActionDetails,
    AuditContext,
    ActionEventEnvelope,
    ActionStatus,
    ActionType,
    AlertEvent,
    AuditActionRecord,
    AuditorDecision,
    AuditorVerdict,
    DeadLetterEnvelope,
    InterceptionMetadata,
    Severity,
    generate_utc_iso_timestamp,
    parse_utc_iso_timestamp,
)
from persistence.queue import ConsumerDispatcher, IngestBuffer
from persistence.store import EventStore, AuditBackpressureError, ConflictingRecordError
from persistence.worker import PersistenceEngine, PersistenceWorker

__all__ = [
    "ActionDetails",
    "AuditContext",
    "AuditBackpressureError",
    "ConflictingRecordError",
    "ActionEventEnvelope",
    "ActionStatus",
    "ActionType",
    "AlertEvent",
    "AuditActionRecord",
    "AuditorDecision",
    "AuditorVerdict",
    "ConsumerDispatcher",
    "DeadLetterEnvelope",
    "EventStore",
    "IngestBuffer",
    "InterceptionMetadata",
    "PersistenceEngine",
    "PersistenceWorker",
    "Severity",
    "generate_utc_iso_timestamp",
    "parse_utc_iso_timestamp",
]
