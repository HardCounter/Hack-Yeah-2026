"""Normalize internal persistence evidence into consumer Event Envelope v2.1.

The persistence schema remains an internal storage format. The translation tables
live in persistence/vocabulary.py (shared with the Layer 1 event builder); this
module shapes the v2.1 dict. Unknown states fail closed.
"""
from __future__ import annotations

from typing import Any

from contracts.action import AgentAction, kind_for_action_type as consumer_kind  # noqa: F401  (compat name)
from contracts.wire import decode_event
from persistence.models import ActionEventEnvelope, ActionType
from persistence.vocabulary import DECISION_FOR_VERDICT, WIRE_ACTION_TYPE, WIRE_STATUS

_ACTION_TYPES = WIRE_ACTION_TYPE
_STATUSES = WIRE_STATUS
_DECISIONS = DECISION_FOR_VERDICT


def to_consumer_v21(event: ActionEventEnvelope) -> dict[str, Any]:
    """Return a plain canonical v2.1 event dict or fail for unsupported states."""
    if event.seq is None:
        raise ValueError("Cannot emit v2.1 event before Layer 2 assigns seq")
    try:
        action_type = _ACTION_TYPES[event.action_type]
    except KeyError:
        raise ValueError(f"Unsupported persistence action type: {event.action_type!r}") from None
    try:
        status = _STATUSES[event.status]
    except KeyError:
        raise ValueError(f"Unsupported persistence status: {event.status!r}") from None
    details = event.action_details
    wire = dict(details.wire_details)
    if event.action_type in (ActionType.TOOL_CALL, ActionType.MCP_TOOL):
        action_details = {
            "name": details.name,
            "side_effect": details.side_effect or "read",
            "transport": details.transport or ("mcp" if event.action_type == ActionType.MCP_TOOL else "inproc"),
            "parameters": dict(details.parameters),
            "result": details.result,
            "error": details.error,
        }
    elif event.action_type == ActionType.LLM_INVOCATION:
        action_details = {
            "model": wire.pop("model", details.name),
            "provider": wire.pop("provider", None),
            "messages": wire.pop("messages", []),
            "completion": wire.pop("completion", None),
            "tool_calls_requested": wire.pop("tool_calls_requested", []),
            "stop_reason": wire.pop("stop_reason", None),
        }
    elif event.action_type == ActionType.EGRESS_HTTP:
        action_details = {k: wire[k] for k in ("method", "host") if k in wire}
        if "method" not in action_details or "host" not in action_details:
            raise ValueError("egress_http requires method and host")
        action_details.update({k: wire[k] for k in ("path", "status_code", "body") if k in wire})
        action_details.setdefault("path", "/")
    elif event.action_type == ActionType.SESSION:
        action_details = {k: wire[k] for k in ("phase", "contract_id", "policy_version", "end_reason") if k in wire}
        if action_details.get("phase") not in ("started", "ended"):
            raise ValueError("session event requires phase started or ended")
    elif event.action_type == ActionType.APPROVAL:
        action_details = {k: wire[k] for k in ("target_event_id", "decision", "approver_role", "delay_ms") if k in wire}
        if "target_event_id" not in action_details or action_details.get("decision") not in ("approved", "rejected", "expired"):
            raise ValueError("approval event requires target_event_id and a supported decision")
    elif event.action_type == ActionType.CONTROL:
        action_details = {k: wire[k] for k in ("change", "policy_version", "signal_id") if k in wire}
        if action_details.get("change") not in ("policy_reloaded", "adjustment_applied", "adjustment_expired"):
            raise ValueError("control event requires a supported change")
    else:  # guarded by _ACTION_TYPES; keeps future enum changes explicit.
        raise ValueError(f"Unsupported persistence action type: {event.action_type!r}")

    meta = event.interception_metadata
    try:
        final_decision = _DECISIONS[meta.verdict]
    except KeyError:
        raise ValueError(f"Unsupported persistence verdict: {meta.verdict!r}") from None
    auditor_decisions = []
    for decision in meta.auditor_decisions:
        try:
            verdict = _DECISIONS[decision.verdict]
        except KeyError:
            raise ValueError(f"Unsupported auditor verdict: {decision.verdict!r}") from None
        auditor_decisions.append({
            "auditor": decision.auditor_name,
            "decision": verdict,
            "rule_id": decision.rule,
            "latency_ms": decision.latency_ms,
        })

    result = {
        "schema_version": "2.1",
        "event_id": event.event_id,
        "action_id": event.context.action_id,
        "seq": event.seq,
        "ts": event.ts,
        "run_id": event.context.run_id,
        "trace_id": event.trace_id,
        "session_id": event.session_id,
        "case_id": event.case_id,
        "agent_id": event.agent_id,
        "step_id": event.context.action_index,
        "parent_span_id": None,
        "action_type": action_type,
        "status": status,
        "action_details": action_details,
        "metrics": {
            "input_tokens": event.context.actual_usage.get("input_tokens", 0),
            "output_tokens": event.context.actual_usage.get("output_tokens", 0),
            "latency_ms": event.context.actual_usage.get("latency_ms", meta.total_latency_ms),
            "cost_usd": event.context.actual_usage.get("actual_cost", 0),
        },
        "fault_injected": meta.fault_injected,
    }
    # Session/approval records created outside the gateway have no decision.
    if event.action_type not in (ActionType.SESSION, ActionType.APPROVAL):
        result["interception_metadata"] = {
            "final_decision": final_decision,
            "policy_version": meta.policy_version,
            "auditor_decisions": auditor_decisions,
            "interception_overhead_ms": meta.total_latency_ms,
        }
    return result


def to_agent_action(event: ActionEventEnvelope) -> AgentAction:
    """Layer 2 -> Layer 3 in one step: storage envelope -> Event Envelope v2.1 -> canonical AgentAction.

    Going through the wire dict keeps one contract: what in-process consumers see is exactly what an
    out-of-process consumer would decode.
    """
    return decode_event(to_consumer_v21(event))
