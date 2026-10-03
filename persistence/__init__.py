"""Layer 2: Persistence Layer for AI Control Layer.

Sanitized immutable audit records and atomic SQLite outbox commits. Durable
analytics are at least once; volatile live feeds and nowait telemetry are
explicitly best effort. Critical dispatch must await the file-backed store.
"""

from persistence.business import EffectReceipt, record_effect, replicate_effects
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
    ConsumerResult,
    DeadLetterEnvelope,
    InterceptionMetadata,
    RunBinding,
    Severity,
    generate_utc_iso_timestamp,
    parse_utc_iso_timestamp,
)
from persistence.queue import ConsumerDispatcher, IngestBuffer
from persistence.settings import PersistenceSettings
from persistence.store import EventStore, AuditBackpressureError, ConflictingRecordError
from persistence.worker import EngineState, PersistenceEngine, PersistenceWorker
from persistence.writer import BoundAuditWriter

__all__ = [
    "ActionDetails",
    "AuditContext",
    "AuditBackpressureError",
    "BoundAuditWriter",
    "ConflictingRecordError",
    "ActionEventEnvelope",
    "ActionStatus",
    "ActionType",
    "AlertEvent",
    "AuditActionRecord",
    "AuditorDecision",
    "AuditorVerdict",
    "ConsumerDispatcher",
    "ConsumerResult",
    "DeadLetterEnvelope",
    "EffectReceipt",
    "EngineState",
    "EventStore",
    "IngestBuffer",
    "InterceptionMetadata",
    "PersistenceEngine",
    "PersistenceSettings",
    "PersistenceWorker",
    "RunBinding",
    "Severity",
    "generate_utc_iso_timestamp",
    "parse_utc_iso_timestamp",
    "record_effect",
    "replicate_effects",
]
