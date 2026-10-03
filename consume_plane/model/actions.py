"""AgentAction: the consume plane's typed view of one persisted agent event.

See docs/consumer-plane.md section 3 and docs/consumer-plane-event-envelope.md for the wire format.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Mapping, Union

ActionKind = Literal["prompt", "tool_use", "egress", "session", "approval", "control"]
ActionStatus = Literal["completed", "blocked", "redacted", "pending_approval", "failed"]
Decision = Literal["ALLOW", "BLOCK", "REDACT", "REQUIRE_APPROVAL", "ALERT"]

KINDS: frozenset[str] = frozenset({"prompt", "tool_use", "egress", "session", "approval", "control"})
STATUSES: frozenset[str] = frozenset({"completed", "blocked", "redacted", "pending_approval", "failed"})
EXECUTED_STATUSES: frozenset[str] = frozenset({"completed", "redacted"})


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
