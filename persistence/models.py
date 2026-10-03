"""Data models and serialization for Layer 2 Persistence Layer.

Provides structured envelopes and audit records representing events, alerts,
decisions, and dead-letter payloads with ISO 8601 UTC timestamps.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Union


def generate_utc_iso_timestamp() -> str:
    """Generate an ISO 8601 UTC timestamp ending in Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_utc_iso_timestamp(ts: str) -> datetime:
    """Parse an ISO 8601 UTC timestamp into a datetime object."""
    clean_ts = ts.replace("Z", "+00:00")
    dt = datetime.fromisoformat(clean_ts)
    if dt.tzinfo is None:
        raise ValueError("Audit timestamp requires an explicit timezone")
    return dt.astimezone(timezone.utc)


class ActionType(str, Enum):
    """Classification of an observed action in the control layer."""
    TOOL_CALL = "TOOL_CALL"
    LLM_INVOCATION = "LLM_INVOCATION"
    GATEWAY_CHECK = "GATEWAY_CHECK"
    DECISION = "DECISION"
    AUDIT = "AUDIT"
    ALERT = "ALERT"
    USER_INPUT = "USER_INPUT"
    SYSTEM = "SYSTEM"


class AuditorVerdict(str, Enum):
    """Decision verdict produced by an auditor or interception gate."""
    ALLOWED = "ALLOWED"
    WARNED = "WARNED"
    BLOCKED = "BLOCKED"
    REDACTED = "REDACTED"
    ESCALATED = "ESCALATED"
    ERROR = "ERROR"


class Severity(str, Enum):
    """Alert and security severity levels."""
    INFO = "INFO"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ActionStatus(str, Enum):
    """Execution status of an action."""
    PENDING = "PENDING"
    EXECUTED = "EXECUTED"
    BLOCKED = "BLOCKED"
    REDACTED = "REDACTED"
    FAILED = "FAILED"
    ESCALATED = "ESCALATED"
    SKIPPED = "SKIPPED"


def _parse_auditor_verdict(val: Any) -> AuditorVerdict:
    if isinstance(val, AuditorVerdict):
        return val
    s = str(val).strip().upper()
    synonyms = {
        "ALLOW": AuditorVerdict.ALLOWED,
        "ALLOWED": AuditorVerdict.ALLOWED,
        "WARN": AuditorVerdict.WARNED,
        "WARNED": AuditorVerdict.WARNED,
        "ALERT": AuditorVerdict.WARNED,
        "BLOCK": AuditorVerdict.BLOCKED,
        "BLOCKED": AuditorVerdict.BLOCKED,
        "REDACT": AuditorVerdict.REDACTED,
        "REDACTED": AuditorVerdict.REDACTED,
        "ESCALATE": AuditorVerdict.ESCALATED,
        "ESCALATED": AuditorVerdict.ESCALATED,
        "REQUIRE_APPROVAL": AuditorVerdict.ESCALATED,
        "APPROVE": AuditorVerdict.ESCALATED,
        "ERROR": AuditorVerdict.ERROR,
    }
    if s not in synonyms:
        raise ValueError("Invalid auditor verdict")
    return synonyms[s]


def _parse_action_type(val: Any) -> ActionType:
    if isinstance(val, ActionType):
        return val
    s = str(val).strip().upper()
    try:
        return ActionType(s)
    except ValueError:
        raise ValueError("Invalid action type") from None


def _parse_severity(val: Any) -> Severity:
    if isinstance(val, Severity):
        return val
    s = str(val).strip().upper()
    try:
        return Severity(s)
    except ValueError:
        raise ValueError("Invalid severity") from None


def _parse_action_status(val: Any) -> ActionStatus:
    if isinstance(val, ActionStatus):
        return val
    s = str(val).strip().upper()
    try:
        return ActionStatus(s)
    except ValueError:
        raise ValueError("Invalid action status") from None


@dataclass
class AuditorDecision:
    """Decision details from an individual policy auditor."""
    auditor_name: str
    verdict: AuditorVerdict
    reason: str = ""
    latency_ms: float = 0.0
    modifications: Optional[Dict[str, Any]] = None
    rule: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "auditor_name": self.auditor_name,
            "verdict": self.verdict.value,
            "reason": self.reason,
            "latency_ms": self.latency_ms,
            "modifications": self.modifications,
            "rule": self.rule,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AuditorDecision":
        return cls(
            auditor_name=data.get("auditor_name", ""),
            verdict=_parse_auditor_verdict(data.get("verdict")),
            reason=data.get("reason", ""),
            latency_ms=float(data.get("latency_ms", 0.0)),
            modifications=data.get("modifications"),
            rule=data.get("rule"),
        )

    def to_json(self, indent: Optional[int] = None) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_json(cls, json_str: str) -> "AuditorDecision":
        return cls.from_dict(json.loads(json_str))


@dataclass
class InterceptionMetadata:
    """Composite interception metadata across all auditors."""
    verdict: AuditorVerdict
    auditor_decisions: List[AuditorDecision] = field(default_factory=list)
    policy_version: Optional[str] = None
    total_latency_ms: float = 0.0
    fault_injected: bool = False
    sanitized_fields: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "auditor_decisions": [d.to_dict() for d in self.auditor_decisions],
            "policy_version": self.policy_version,
            "total_latency_ms": self.total_latency_ms,
            "fault_injected": self.fault_injected,
            "sanitized_fields": list(self.sanitized_fields),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "InterceptionMetadata":
        if not isinstance(data.get("fault_injected", False), bool):
            raise ValueError("fault_injected must be a boolean")
        raw_decisions = data.get("auditor_decisions", [])
        decisions = [
            d if isinstance(d, AuditorDecision) else AuditorDecision.from_dict(d)
            for d in raw_decisions
        ]
        return cls(
            verdict=_parse_auditor_verdict(data.get("verdict")),
            auditor_decisions=decisions,
            policy_version=data.get("policy_version"),
            total_latency_ms=float(data.get("total_latency_ms", 0.0)),
            fault_injected=data.get("fault_injected", False),
            sanitized_fields=list(data.get("sanitized_fields", [])),
        )

    def to_json(self, indent: Optional[int] = None) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_json(cls, json_str: str) -> "InterceptionMetadata":
        return cls.from_dict(json.loads(json_str))


@dataclass
class ActionDetails:
    """Details of the action proposed or executed."""
    name: str
    parameters: Dict[str, Any] = field(default_factory=dict)
    result: Optional[Any] = None
    error: Optional[str] = None
    bytes_returned: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "parameters": self.parameters,
            "result": self.result,
            "error": self.error,
            "bytes_returned": self.bytes_returned,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ActionDetails":
        return cls(
            name=data.get("name", ""),
            parameters=data.get("parameters", {}) or {},
            result=data.get("result"),
            error=data.get("error"),
            bytes_returned=data.get("bytes_returned"),
        )

    def to_json(self, indent: Optional[int] = None) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_json(cls, json_str: str) -> "ActionDetails":
        return cls.from_dict(json.loads(json_str))


@dataclass
class AuditContext:
    """References supplied by a trusted gateway, never evidence of authority alone.

    None denotes unavailable binding; persistence does not invent a contract or
    authenticate its issuer. No raw baseline/objective belongs in general logs.
    """
    contract_id: Optional[str] = None
    run_id: Optional[str] = None
    action_id: Optional[str] = None
    principal_id: Optional[str] = None
    action_index: Optional[int] = None
    policy_hash: Optional[str] = None
    feed_version: Optional[str] = None
    approval_id: Optional[str] = None
    intervention_id: Optional[str] = None
    effect_receipt_id: Optional[str] = None
    verification_status: Optional[str] = None
    semantic_model: Optional[str] = None
    semantic_model_version: Optional[str] = None
    reason_code: Optional[str] = None
    reserved_usage: Dict[str, Any] = field(default_factory=dict)
    actual_usage: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AuditContext":
        return cls(**data)


@dataclass(frozen=True)
class RunBinding:
    """Server-side authenticated binding for a governed workflow run."""
    run_id: str
    contract_id: str
    session_id: str
    principal_id: str
    agent_id: str
    policy_version: str
    policy_hash: str
    feed_version: str

    def __post_init__(self) -> None:
        for f_name in self.__dataclass_fields__:
            val = getattr(self, f_name)
            if not isinstance(val, str) or not val.strip():
                raise ValueError(f"RunBinding.{f_name} must be a non-empty string")

    def to_dict(self) -> Dict[str, str]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RunBinding":
        return cls(**data)


@dataclass
class ActionEventEnvelope:
    """Canonical envelope for all persisted action events."""
    trace_id: str
    session_id: str
    agent_id: str
    action_type: ActionType
    status: ActionStatus
    action_details: ActionDetails
    interception_metadata: InterceptionMetadata
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    schema_version: str = "2.0"
    case_id: Optional[str] = None
    ts: str = field(default_factory=generate_utc_iso_timestamp)
    source: str = "gateway"
    context: AuditContext = field(default_factory=AuditContext)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "schema_version": self.schema_version,
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "case_id": self.case_id,
            "agent_id": self.agent_id,
            "ts": self.ts,
            "action_type": self.action_type.value,
            "source": self.source,
            "status": self.status.value,
            "action_details": self.action_details.to_dict(),
            "interception_metadata": self.interception_metadata.to_dict(),
            "context": self.context.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ActionEventEnvelope":
        raw_details = data.get("action_details", {})
        action_details = (
            raw_details
            if isinstance(raw_details, ActionDetails)
            else ActionDetails.from_dict(raw_details)
        )

        raw_meta = data.get("interception_metadata", {})
        interception_metadata = (
            raw_meta
            if isinstance(raw_meta, InterceptionMetadata)
            else InterceptionMetadata.from_dict(raw_meta)
        )

        return cls(
            event_id=data.get("event_id") or str(uuid.uuid4()),
            schema_version=data.get("schema_version", "2.0"),
            trace_id=data.get("trace_id", ""),
            session_id=data.get("session_id", ""),
            case_id=data.get("case_id"),
            agent_id=data.get("agent_id", ""),
            ts=data.get("ts") or generate_utc_iso_timestamp(),
            action_type=_parse_action_type(data.get("action_type", ActionType.TOOL_CALL)),
            source=data.get("source", "gateway"),
            status=_parse_action_status(data.get("status", ActionStatus.PENDING)),
            action_details=action_details,
            interception_metadata=interception_metadata,
            context=AuditContext.from_dict(data.get("context", {})),
        )

    def to_json(self, indent: Optional[int] = None) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_json(cls, json_str: str) -> "ActionEventEnvelope":
        return cls.from_dict(json.loads(json_str))


@dataclass
class AlertEvent:
    """Security alert emitted during policy interception or consumer risk evaluation."""
    severity: Severity
    rule: str
    agent_id: str
    session_id: str
    action_taken: str
    alert_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    ts: str = field(default_factory=generate_utc_iso_timestamp)
    case_id: Optional[str] = None
    evidence: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "alert_id": self.alert_id,
            "ts": self.ts,
            "severity": self.severity.value,
            "rule": self.rule,
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "case_id": self.case_id,
            "action_taken": self.action_taken,
            "evidence": self.evidence,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AlertEvent":
        return cls(
            alert_id=data.get("alert_id") or str(uuid.uuid4()),
            ts=data.get("ts") or generate_utc_iso_timestamp(),
            severity=_parse_severity(data.get("severity", Severity.INFO)),
            rule=data.get("rule", ""),
            agent_id=data.get("agent_id", ""),
            session_id=data.get("session_id", ""),
            case_id=data.get("case_id"),
            action_taken=data.get("action_taken", ""),
            evidence=data.get("evidence", {}) or {},
        )

    def to_json(self, indent: Optional[int] = None) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_json(cls, json_str: str) -> "AlertEvent":
        return cls.from_dict(json.loads(json_str))


@dataclass(frozen=True)
class ConsumerResult:
    """Result returned by an analytics consumer callback."""
    alerts: tuple[AlertEvent, ...] = ()

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ConsumerResult":
        raw_alerts = data.get("alerts", ())
        alerts = tuple(
            a if isinstance(a, AlertEvent) else AlertEvent.from_dict(a)
            for a in raw_alerts
        )
        return cls(alerts=alerts)

    def to_dict(self) -> Dict[str, Any]:
        return {"alerts": [a.to_dict() for a in self.alerts]}


@dataclass
class AuditActionRecord:
    """Audit record capturing an executed tool call or side-effecting operation."""
    run_id: str
    session_id: str
    agent_id: str
    tool_name: str
    action_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    ts: str = field(default_factory=generate_utc_iso_timestamp)
    target_id: Optional[str] = None
    side_effect_class: str = "read"
    status: str = "EXECUTED"
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action_id": self.action_id,
            "ts": self.ts,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "tool_name": self.tool_name,
            "target_id": self.target_id,
            "side_effect_class": self.side_effect_class,
            "status": self.status,
            "details": self.details,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AuditActionRecord":
        return cls(
            action_id=data.get("action_id") or str(uuid.uuid4()),
            ts=data.get("ts") or generate_utc_iso_timestamp(),
            run_id=data.get("run_id", ""),
            session_id=data.get("session_id", ""),
            agent_id=data.get("agent_id", ""),
            tool_name=data.get("tool_name", ""),
            target_id=data.get("target_id"),
            side_effect_class=data.get("side_effect_class", "read"),
            status=data.get("status", "EXECUTED"),
            details=data.get("details", {}) or {},
        )

    def to_json(self, indent: Optional[int] = None) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_json(cls, json_str: str) -> "AuditActionRecord":
        return cls.from_dict(json.loads(json_str))


@dataclass
class DeadLetterEnvelope:
    """Envelope for events that repeatedly failed consumer delivery."""
    event: Union[ActionEventEnvelope, Dict[str, Any]]
    consumer_name: str
    error_message: str
    dlq_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    failed_at: str = field(default_factory=generate_utc_iso_timestamp)
    retry_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        event_dict = (
            self.event.to_dict()
            if isinstance(self.event, ActionEventEnvelope)
            else self.event
        )
        return {
            "dlq_id": self.dlq_id,
            "failed_at": self.failed_at,
            "event": event_dict,
            "consumer_name": self.consumer_name,
            "error_message": self.error_message,
            "retry_count": self.retry_count,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DeadLetterEnvelope":
        raw_event = data.get("event", {})
        if isinstance(raw_event, dict) and "trace_id" in raw_event and "action_details" in raw_event:
            try:
                event: Union[ActionEventEnvelope, Dict[str, Any]] = ActionEventEnvelope.from_dict(raw_event)
            except Exception:
                event = raw_event
        else:
            event = raw_event

        return cls(
            dlq_id=data.get("dlq_id") or str(uuid.uuid4()),
            failed_at=data.get("failed_at") or generate_utc_iso_timestamp(),
            event=event,
            consumer_name=data.get("consumer_name", ""),
            error_message=data.get("error_message", ""),
            retry_count=int(data.get("retry_count", 0)),
        )

    def to_json(self, indent: Optional[int] = None) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_json(cls, json_str: str) -> "DeadLetterEnvelope":
        return cls.from_dict(json.loads(json_str))
