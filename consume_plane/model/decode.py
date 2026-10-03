"""Decode wire envelope v2.1 (docs/consumer-plane-event-envelope.md) into AgentAction."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from .actions import (
    STATUSES,
    AgentAction,
    ApprovalPayload,
    AuditorDecision,
    ContentRef,
    ControlPayload,
    EgressPayload,
    GatewayVerdict,
    PromptPayload,
    SessionPayload,
    ToolCallIntent,
    ToolUsePayload,
    UnknownPayload,
    Usage,
)

SUPPORTED_SCHEMA_VERSIONS = frozenset({"2.1"})

ACTION_TYPE_TO_KIND = {
    "llm_call": "prompt",
    "tool_call": "tool_use",
    "mcp_tool": "tool_use",
    "egress_http": "egress",
    "session": "session",
    "approval": "approval",
    "control": "control",
}


class DecodeError(ValueError):
    pass


def _req(d: Mapping[str, Any], key: str, where: str = "event") -> Any:
    if key not in d or d[key] is None:
        raise DecodeError(f"{where}: missing required field '{key}'")
    return d[key]


def _ts(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as e:
        raise DecodeError(f"event: bad timestamp {value!r}") from e


def _content_ref(value: Any, where: str) -> ContentRef | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or "ref" not in value:
        raise DecodeError(f"{where}: bodies must be content references, not inline values")
    trust = value.get("trust", "untrusted")
    if trust not in ("trusted", "untrusted"):
        raise DecodeError(f"{where}: bad trust label {trust!r}")
    return ContentRef(
        ref=value["ref"],
        sha256=_req(value, "sha256", where),
        size_bytes=int(value.get("size_bytes", 0)),
        redacted=bool(value.get("redacted", False)),
        trust=trust,
    )


def _payload(kind: str, d: Mapping[str, Any]):
    if kind == "prompt":
        return PromptPayload(
            model=_req(d, "model", "llm_call"),
            provider=d.get("provider"),
            messages=tuple(_content_ref(m, "llm_call.messages") for m in d.get("messages", ())),
            completion=_content_ref(d.get("completion"), "llm_call.completion"),
            tool_calls_requested=tuple(
                ToolCallIntent(name=_req(t, "name", "tool_calls_requested"), args=dict(t.get("parameters", {})))
                for t in d.get("tool_calls_requested", ())
            ),
            stop_reason=d.get("stop_reason"),
        )
    if kind == "tool_use":
        return ToolUsePayload(
            tool=_req(d, "name", "tool_call"),
            side_effect=d.get("side_effect", "read"),
            transport=d.get("transport", "inproc"),
            args=dict(d.get("parameters", {})),
            result=_content_ref(d.get("result"), "tool_call.result"),
            error=d.get("error"),
        )
    if kind == "egress":
        return EgressPayload(
            method=_req(d, "method", "egress_http"),
            host=_req(d, "host", "egress_http"),
            path=d.get("path", "/"),
            status_code=d.get("status_code"),
            body=_content_ref(d.get("body"), "egress_http.body"),
        )
    if kind == "session":
        phase = _req(d, "phase", "session")
        if phase not in ("started", "ended"):
            raise DecodeError(f"session: bad phase {phase!r}")
        return SessionPayload(phase, d.get("contract_id"), d.get("policy_version"), d.get("end_reason"))
    if kind == "approval":
        return ApprovalPayload(
            target_event_id=_req(d, "target_event_id", "approval"),
            decision=_req(d, "decision", "approval"),
            approver_role=d.get("approver_role"),
            delay_ms=d.get("delay_ms"),
        )
    if kind == "control":
        return ControlPayload(_req(d, "change", "control"), d.get("policy_version"), d.get("signal_id"))
    return UnknownPayload(raw=dict(d))


def _gateway(d: Mapping[str, Any] | None) -> GatewayVerdict | None:
    if not d:
        return None
    return GatewayVerdict(
        final=_req(d, "final_decision", "interception_metadata"),
        policy_version=_req(d, "policy_version", "interception_metadata"),
        decisions=tuple(
            AuditorDecision(
                auditor=_req(a, "auditor", "auditor_decisions"),
                decision=_req(a, "decision", "auditor_decisions"),
                rule_id=a.get("rule_id"),
                latency_ms=a.get("latency_ms"),
            )
            for a in d.get("auditor_decisions", ())
        ),
        interception_overhead_ms=d.get("interception_overhead_ms"),
    )


def _usage(d: Mapping[str, Any] | None) -> Usage | None:
    if not d:
        return None
    return Usage(
        input_tokens=int(d.get("input_tokens", 0)),
        output_tokens=int(d.get("output_tokens", 0)),
        latency_ms=float(d.get("latency_ms", 0.0)),
        cost_usd=float(d.get("cost_usd", 0.0)),
    )


def decode_event(raw: Mapping[str, Any]) -> AgentAction:
    if not isinstance(raw, Mapping):
        raise DecodeError("event: not a JSON object")
    version = _req(raw, "schema_version")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise DecodeError(f"event: unsupported schema_version {version!r}")
    action_type = _req(raw, "action_type")
    kind = ACTION_TYPE_TO_KIND.get(action_type, action_type)
    status = _req(raw, "status")
    if status not in STATUSES:
        raise DecodeError(f"event: bad status {status!r}")
    seq = _req(raw, "seq")
    if not isinstance(seq, int) or seq < 0:
        raise DecodeError(f"event: seq must be a non-negative integer, got {seq!r}")
    try:
        payload = _payload(kind, raw.get("action_details") or {})
    except (TypeError, AttributeError) as e:
        raise DecodeError(f"event: malformed action_details ({e})") from e
    return AgentAction(
        schema_version=version,
        event_id=_req(raw, "event_id"),
        seq=seq,
        ts=_ts(_req(raw, "ts")),
        run_id=raw.get("run_id"),
        session_id=_req(raw, "session_id"),
        case_id=raw.get("case_id"),
        agent_id=_req(raw, "agent_id"),
        step_id=raw.get("step_id"),
        parent_span_id=raw.get("parent_span_id"),
        kind=kind,
        status=status,
        payload=payload,
        gateway=_gateway(raw.get("interception_metadata")),
        usage=_usage(raw.get("metrics")),
        fault_injected=bool(raw.get("fault_injected", False)),
        raw=raw,
    )
