"""Telemetry boundary: retain structured evidence, never raw interaction content.

This is deliberately an allowlist, not a promise to recognize every secret with
regexes. Identity tokens must be opaque, gateway-issued IDs; this module is not
an authentication mechanism. Protected KYC baselines belong in a separate store.
"""

from __future__ import annotations

import math
import re
from typing import Any

from persistence.models import (
    ActionEventEnvelope, AlertEvent, AuditActionRecord, DeadLetterEnvelope,
    parse_utc_iso_timestamp,
)

OMITTED = "[OMITTED]"
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SECRET = re.compile(r"(?i)(?:\bsk-|ghp_|github_pat_|AKIA|bearer|password|secret|token=)")
_NUMBERS = {
    "attempt_count", "rows_written", "bytes_returned", "client_count",
    "input_tokens", "output_tokens", "reserved_tokens", "actual_tokens",
    "reserved_cost", "actual_cost", "latency_ms", "duration_ms",
}
_IDS = {
    "application_id", "client_id", "receipt_id", "action_id", "contract_id",
    "run_id", "approval_id", "intervention_id", "policy_version", "feed_version",
    "rule", "reason_code", "model", "model_version", "forbidden_tool",
}
_STATES = {"status", "verification_status", "side_effect_class", "decision"}


def token(value: Any, *, required: bool = False) -> str | None:
    if value is None and not required:
        return None
    if (not isinstance(value, str) or not _TOKEN.fullmatch(value)
            or _SECRET.search(value) or re.fullmatch(r"\d{9,}", value)
            or re.search(r"\d{3}-\d{2}-\d{4}", value)):
        raise ValueError("Invalid telemetry identifier; use an opaque trusted ID")
    return value


def number(value: Any) -> float | int:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < 0 or value > 2**63 - 1):
        raise ValueError("Telemetry measurements must be finite and non-negative")
    return value


def timestamp(value: str) -> str:
    return parse_utc_iso_timestamp(value).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def evidence(value: Any) -> dict[str, Any]:
    """Discard unknown keys and all free text, including nested payloads."""
    if not isinstance(value, dict):
        return {}
    result = {}
    for key, item in value.items():
        if key in _NUMBERS:
            result[key] = number(item)
        elif key in _IDS:
            result[key] = token(item)
        elif key in _STATES:
            states = {
                "CREATED", "APPROVED", "REJECTED", "ESCALATED", "PENDING", "EXECUTED",
                "BLOCKED", "REDACTED", "FAILED", "SKIPPED", "ALLOWED", "ALLOW", "BLOCK",
                "VERIFIED_SUCCESS", "FAILED_POSTCONDITIONS", "VERIFICATION_INCOMPLETE",
                "READ", "WRITE", "NONE",
            }
            if not isinstance(item, str) or item.upper() not in states:
                raise ValueError("Invalid structured evidence state")
            result[key] = item.upper()
    return result


def sanitize_event(event: ActionEventEnvelope) -> ActionEventEnvelope:
    # Roundtrip creates an owned snapshot and validates enums even for dataclass
    # callers. Raw content is omitted before serializing to any durable medium.
    if len(event.interception_metadata.auditor_decisions) > 128:
        raise ValueError("Too many auditor decisions")
    data = event.to_dict()
    if data["schema_version"] != "2.0":
        raise ValueError("Unsupported audit envelope schema")
    for key in ("event_id", "trace_id", "session_id", "agent_id", "source", "schema_version"):
        data[key] = token(data[key], required=True)
    data["case_id"] = token(data["case_id"])
    data["ts"] = timestamp(data["ts"])
    details = data["action_details"]
    details["name"] = token(details["name"], required=True)
    details["parameters"] = evidence(details["parameters"])
    details["result"] = evidence(details["result"]) if details["result"] is not None else None
    details["error"] = OMITTED if details["error"] is not None else None
    if details["bytes_returned"] is not None:
        number(details["bytes_returned"])
    meta = data["interception_metadata"]
    if not isinstance(meta["fault_injected"], bool):
        raise ValueError("fault_injected must be a boolean")
    meta["policy_version"] = token(meta["policy_version"])
    number(meta["total_latency_ms"])
    for decision in meta["auditor_decisions"]:
        decision["auditor_name"] = token(decision["auditor_name"], required=True)
        decision["rule"] = token(decision["rule"])
        decision["reason"] = OMITTED if decision["reason"] else ""
        decision["modifications"] = None
        number(decision["latency_ms"])
    meta["sanitized_fields"] = ["parameters", "result", "error", "reason", "modifications"]
    context = data["context"]
    if (context["run_id"] is None) != (context["action_index"] is None):
        raise ValueError("Run identity and gateway event index must be supplied together")
    for key, value in context.items():
        if key == "action_index":
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
                raise ValueError("Invalid action index")
        elif key in ("reserved_usage", "actual_usage"):
            context[key] = evidence(value)
        elif key == "policy_hash":
            if value is not None and not re.fullmatch(r"[a-f0-9]{64}", value):
                raise ValueError("Policy hash must be a SHA-256 hex digest")
        elif key == "verification_status":
            if value not in (None, "VERIFIED_SUCCESS", "FAILED_POSTCONDITIONS", "VERIFICATION_INCOMPLETE"):
                raise ValueError("Invalid verification status")
        else:
            context[key] = token(value)
    return ActionEventEnvelope.from_dict(data)


def sanitize_alert(alert: AlertEvent) -> AlertEvent:
    data = alert.to_dict()
    for key in ("alert_id", "rule", "agent_id", "session_id", "action_taken"):
        data[key] = token(data[key], required=True)
    data["case_id"] = token(data["case_id"])
    data["ts"] = timestamp(data["ts"])
    data["evidence"] = evidence(data["evidence"])
    return AlertEvent.from_dict(data)


def sanitize_audit(action: AuditActionRecord) -> AuditActionRecord:
    data = action.to_dict()
    for key in ("action_id", "run_id", "session_id", "agent_id", "tool_name", "side_effect_class", "status"):
        data[key] = token(data[key], required=True)
    data["target_id"] = token(data["target_id"])
    data["ts"] = timestamp(data["ts"])
    data["details"] = evidence(data["details"])
    return AuditActionRecord.from_dict(data)


def sanitize_dead_letter(dlq: DeadLetterEnvelope) -> DeadLetterEnvelope:
    data = dlq.to_dict()
    data["dlq_id"] = token(data["dlq_id"], required=True)
    data["consumer_name"] = token(data["consumer_name"], required=True)
    data["failed_at"] = timestamp(data["failed_at"])
    number(data["retry_count"])
    raw_err = str(data.get("error_message") or "")
    if raw_err.startswith("CONSUMER_RETIRED:"):
        parts = raw_err.split(":", 1)
        intervention_id = token(parts[1].strip(), required=True)
        data["error_message"] = f"CONSUMER_RETIRED: {intervention_id}"
    else:
        data["error_message"] = "CONSUMER_DELIVERY_FAILED"
    data["event"] = sanitize_event(dlq.event).to_dict() if isinstance(dlq.event, ActionEventEnvelope) else evidence(dlq.event)
    return DeadLetterEnvelope.from_dict(data)
