"""Canonical agent-action model shared by all three planes.

One agent action (a prompt/model request, a tool use, an egress call, or a session/approval/control
lifecycle event) has three stages, and this module defines the vocabulary for all of them:

    ActionProposal  -- Layer 1 input: what the agent asked to do, identity bound by the gateway
    (persisted)     -- Layer 2 stores it; persistence/vocabulary.py maps to its internal storage enums
    AgentAction     -- Layer 3 view: one persisted, sequenced event (decoded from Event Envelope v2.1)

Wire format: docs/consumer-plane-event-envelope.md.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Mapping, Union

from .decision import Decision

ActionKind = Literal["prompt", "tool_use", "egress", "session", "approval", "control"]
ActionStatus = Literal["completed", "blocked", "redacted", "pending_approval", "failed"]
SideEffect = Literal["read", "write", "irreversible"]
Transport = Literal["inproc", "mcp", "http"]


KINDS: frozenset[str] = frozenset({"prompt", "tool_use", "egress", "session", "approval", "control"})
STATUSES: frozenset[str] = frozenset({"completed", "blocked", "redacted", "pending_approval", "failed"})
EXECUTED_STATUSES: frozenset[str] = frozenset({"completed", "redacted"})

# Wire `action_type` (Event Envelope v2.1) -> canonical kind. The only copy of this table.
WIRE_ACTION_TYPES: Mapping[str, str] = {
    "llm_call": "prompt", "tool_call": "tool_use", "mcp_tool": "tool_use", "egress_http": "egress",
    "session": "session", "approval": "approval", "control": "control",
}
# Final gateway decision -> canonical status of the recorded action. The only copy of this table.
STATUS_FOR_DECISION: Mapping[str, str] = {
    "ALLOW": "completed", "ALERT": "completed", "REDACT": "redacted",
    "BLOCK": "blocked", "REQUIRE_APPROVAL": "pending_approval",
}


def kind_for_action_type(action_type: str) -> str:
    """Canonical kind for a wire action_type; unknown types are passed through unchanged."""
    return WIRE_ACTION_TYPES.get(action_type, action_type)


def status_for_decision(decision: str) -> str:
    try:
        return STATUS_FOR_DECISION[decision]
    except KeyError:
        raise ValueError(f"unknown gateway decision {decision!r}") from None


@dataclass(frozen=True, kw_only=True)
class ActionProposal:
    """Normalized Layer 1 proposal; identity and metadata are bound by the trusted gateway."""
    action_id: str
    session_id: str
    agent_id: str
    tool: str
    arguments: Mapping[str, Any]
    kind: str = "tool_use"
    side_effect: str = "read"
    transport: str = "inproc"


@dataclass(frozen=True, slots=True)
class ContentRef:
    """Pointer to a body (prompt, completion, tool result) held in the persistence layer."""
    ref: str
    sha256: str
    size_bytes: int
    redacted: bool
    trust: Literal["trusted", "untrusted"]


@dataclass(frozen=True, slots=True)
class ToolCallIntent:
    """A tool call the model asked for inside an LLM completion."""
    name: str
    args: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class PromptPayload:
    model: str
    provider: str | None
    messages: tuple[ContentRef, ...]
    completion: ContentRef | None
    tool_calls_requested: tuple[ToolCallIntent, ...]
    stop_reason: str | None


@dataclass(frozen=True, slots=True)
class ToolUsePayload:
    tool: str
    side_effect: Literal["read", "write", "irreversible"]
    transport: Literal["inproc", "mcp", "http"]
    args: Mapping[str, Any]
    result: ContentRef | None
    error: str | None


@dataclass(frozen=True, slots=True)
class EgressPayload:
    method: str
    host: str
    path: str
    status_code: int | None
    body: ContentRef | None


@dataclass(frozen=True, slots=True)
class SessionPayload:
    phase: Literal["started", "ended"]
    contract_id: str | None
    policy_version: str | None
    end_reason: str | None


@dataclass(frozen=True, slots=True)
class ApprovalPayload:
    target_event_id: str
    decision: Literal["approved", "rejected", "expired"]
    approver_role: str | None
    delay_ms: float | None


@dataclass(frozen=True, slots=True)
class ControlPayload:
    change: Literal["policy_reloaded", "adjustment_applied", "adjustment_expired"]
    policy_version: str | None
    signal_id: str | None


@dataclass(frozen=True, slots=True)
class UnknownPayload:
    """Payload of an action_type this schema version does not know; delivered to "*" subscribers only."""
    raw: Mapping[str, Any]


ActionPayload = Union[
    PromptPayload, ToolUsePayload, EgressPayload, SessionPayload, ApprovalPayload, ControlPayload, UnknownPayload
]


@dataclass(frozen=True, slots=True)
class AuditorDecision:
    auditor: str
    decision: Decision
    rule_id: str | None = None
    latency_ms: float | None = None


@dataclass(frozen=True, slots=True)
class GatewayVerdict:
    final: Decision
    policy_version: str
    decisions: tuple[AuditorDecision, ...] = ()
    interception_overhead_ms: float | None = None


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    cost_usd: float = 0.0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.latency_ms + other.latency_ms,
            self.cost_usd + other.cost_usd,
        )


@dataclass(frozen=True, slots=True)
class AgentAction:
    schema_version: str
    event_id: str
    seq: int
    ts: datetime

    run_id: str | None
    session_id: str
    case_id: str | None
    agent_id: str
    step_id: int | None
    parent_span_id: str | None

    kind: str  # an ActionKind, or the raw action_type when unknown
    status: ActionStatus
    payload: ActionPayload

    gateway: GatewayVerdict | None
    usage: Usage | None
    fault_injected: bool = False
    action_id: str | None = None  # gateway action ID, shared by intent, result and receipt evidence
    raw: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def known_kind(self) -> bool:
        return self.kind in KINDS

    @property
    def executed(self) -> bool:
        """True if the gateway let the call through and the upstream returned."""
        return self.status in EXECUTED_STATUSES

    @property
    def policy_version(self) -> str:
        if self.gateway is not None:
            return self.gateway.policy_version
        version = getattr(self.payload, "policy_version", None)
        return version or "unknown"
