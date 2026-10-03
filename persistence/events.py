"""Layer 1 -> Layer 2: the one way to turn a gateway action into a persistence envelope.

Interception code (tool calls, model requests, session lifecycle) describes the action in canonical
terms -- wire action type, gateway decision or execution state, sanitized parameters -- and this
builder fills the internal storage envelope. Translation lives in persistence.vocabulary only.
"""
from __future__ import annotations

import uuid
from typing import Any, Iterable, Mapping

from contracts import TaskContract
from persistence.models import (
    ActionDetails,
    ActionEventEnvelope,
    ActionStatus,
    AuditContext,
    AuditorDecision,
    InterceptionMetadata,
)
from persistence.vocabulary import STORAGE_ACTION_TYPE, VERDICT_FOR_DECISION, storage_status, storage_verdict

REASON_CODE_MAX = 64


def auditor_decisions(rows: Iterable[Mapping[str, Any]]) -> list[AuditorDecision]:
    """Storage auditor decisions from pipeline rows; rows with no recognized decision are dropped."""
    out = []
    for row in rows:
        verdict = VERDICT_FOR_DECISION.get(row.get("decision"))
        if verdict is None:
            continue
        rule = row.get("code") or row.get("rule_id")
        out.append(AuditorDecision(
            auditor_name=str(row.get("auditor", "unknown")), verdict=verdict,
            latency_ms=float(row.get("latency_ms", 0.0)), rule=str(rule) if rule else None,
        ))
    return out


def build_action_event(
    *,
    contract: TaskContract,
    action_type: str,
    action_id: str,
    name: str,
    status: str | ActionStatus,
    decision: str | None = None,
    intent: bool = False,
    parameters: Mapping[str, Any] | None = None,
    side_effect: str = "read",
    transport: str = "inproc",
    wire_details: Mapping[str, Any] | None = None,
    auditor_rows: Iterable[Mapping[str, Any]] = (),
    latency_ms: float = 0.0,
    fault_injected: bool = False,
    reason_code: str | None = None,
    reserved_usage: Mapping[str, Any] | None = None,
    actual_usage: Mapping[str, Any] | None = None,
    effect_receipt_id: str | None = None,
    intervention_id: str | None = None,
    event_id: str | None = None,
) -> ActionEventEnvelope:
    """Build the storage envelope for one action of the contract's run.

    `action_type` is the wire type ("tool_call", "llm_call", "session", ...). `status` is a gateway
    decision ("ALLOW"...), a canonical status ("completed"...) or a storage execution state
    ("FAILED"...). `decision` is the authorization outcome when it differs from `status` (e.g. an
    allowed call that failed). `intent=True` records a durable dispatch intent (storage PENDING).
    Parameters must already be sanitized by the caller; persistence projects them again on write.
    """
    try:
        stored_type = STORAGE_ACTION_TYPE[action_type]
    except KeyError:
        raise ValueError(f"unsupported action type {action_type!r}") from None
    metadata = InterceptionMetadata(
        verdict=storage_verdict(decision or (status.value if isinstance(status, ActionStatus) else status)),
        auditor_decisions=auditor_decisions(auditor_rows),
        policy_version=contract.policy_version,
        total_latency_ms=max(0.0, latency_ms),
        fault_injected=fault_injected,
    )
    details = ActionDetails(
        name=name,
        parameters=dict(parameters or {}),
        result=None,
        side_effect=side_effect,
        transport=transport if transport in ("inproc", "mcp", "http") else "inproc",
        wire_details=dict(wire_details or {}),
    )
    context = AuditContext(
        contract_id=contract.contract_id,
        run_id=contract.run_id,
        action_id=action_id,
        principal_id=contract.principal_id,
        policy_hash=contract.policy_hash,
        feed_version=contract.feed_version,
        effect_receipt_id=effect_receipt_id,
        intervention_id=intervention_id,
        reason_code=reason_code[:REASON_CODE_MAX] if reason_code else None,
        reserved_usage=dict(reserved_usage or {}),
        actual_usage=dict(actual_usage or {}),
    )
    return ActionEventEnvelope(
        event_id=event_id or str(uuid.uuid4()),
        trace_id=contract.run_id or contract.session_id,
        session_id=contract.session_id,
        case_id=contract.case_id,
        agent_id=contract.agent_id,
        action_type=stored_type,
        status=ActionStatus.PENDING if intent else storage_status(status),
        action_details=details,
        interception_metadata=metadata,
        context=context,
    )
