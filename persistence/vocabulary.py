"""The only translation between the canonical action vocabulary (contracts.action / contracts.decision)
and Layer 2's internal storage enums (persistence.models).

Storage keeps its historical uppercase values (schema "2.0" rows, SQLite columns and indexes); nothing
outside persistence/ should need them. Layer 1 writes through `persistence.events.build_action_event`,
and Layer 3 reads Event Envelope v2.1 produced by `persistence.adapters.consumer_v21`, both of which use
these tables. Unknown values fail closed.
"""
from __future__ import annotations

from typing import Mapping, TypeVar

from contracts.action import status_for_decision
from contracts.decision import DECISIONS
from persistence.models import ActionStatus, ActionType, AuditorVerdict

K = TypeVar("K")
V = TypeVar("V")


def _invert(table: Mapping[K, V]) -> dict[V, K]:
    inverse = {v: k for k, v in table.items()}
    assert len(inverse) == len(table), "vocabulary table must be one-to-one"
    return inverse


# Wire action_type (Event Envelope v2.1) <-> storage ActionType.
STORAGE_ACTION_TYPE: Mapping[str, ActionType] = {
    "tool_call": ActionType.TOOL_CALL,
    "llm_call": ActionType.LLM_INVOCATION,
    "mcp_tool": ActionType.MCP_TOOL,
    "egress_http": ActionType.EGRESS_HTTP,
    "session": ActionType.SESSION,
    "approval": ActionType.APPROVAL,
    "control": ActionType.CONTROL,
}
WIRE_ACTION_TYPE: Mapping[ActionType, str] = _invert(STORAGE_ACTION_TYPE)

# Canonical action status <-> storage ActionStatus. PENDING (a durable dispatch intent) has no
# canonical status: intents are never delivered to consumers.
STORAGE_STATUS: Mapping[str, ActionStatus] = {
    "completed": ActionStatus.EXECUTED,
    "blocked": ActionStatus.BLOCKED,
    "redacted": ActionStatus.REDACTED,
    "pending_approval": ActionStatus.ESCALATED,
    "failed": ActionStatus.FAILED,
}
WIRE_STATUS: Mapping[ActionStatus, str] = _invert(STORAGE_STATUS)

# Gateway decision <-> storage AuditorVerdict.
VERDICT_FOR_DECISION: Mapping[str, AuditorVerdict] = {
    "ALLOW": AuditorVerdict.ALLOWED,
    "ALERT": AuditorVerdict.WARNED,
    "REDACT": AuditorVerdict.REDACTED,
    "BLOCK": AuditorVerdict.BLOCKED,
    "REQUIRE_APPROVAL": AuditorVerdict.ESCALATED,
}
DECISION_FOR_VERDICT: Mapping[AuditorVerdict, str] = _invert(VERDICT_FOR_DECISION)

# Execution states that are not an authorization outcome: the action was allowed, then it was
# queued (PENDING), ran (EXECUTED) or failed (FAILED).
_ALLOWED_EXECUTION_STATES = frozenset({"PENDING", "EXECUTED", "FAILED"})


def storage_status(value: str | ActionStatus) -> ActionStatus:
    """Storage status for a gateway decision ("ALLOW"...), a canonical status ("completed"...),
    or a storage status name ("PENDING", "FAILED"...)."""
    if isinstance(value, ActionStatus):
        return value
    if value in DECISIONS:
        return STORAGE_STATUS[status_for_decision(value)]
    if value in STORAGE_STATUS:
        return STORAGE_STATUS[value]
    return ActionStatus(value)  # raises ValueError for unknown values


def storage_verdict(value: str) -> AuditorVerdict:
    """Storage verdict for a gateway decision, or ALLOWED for an execution state of an allowed action."""
    if value in VERDICT_FOR_DECISION:
        return VERDICT_FOR_DECISION[value]
    if value in _ALLOWED_EXECUTION_STATES:
        return AuditorVerdict.ALLOWED
    raise ValueError(f"no storage verdict for {value!r}")


def is_intent(event) -> bool:
    """True for a durable dispatch intent: stored before a high-impact call, never delivered to consumers."""
    return event.status == ActionStatus.PENDING
