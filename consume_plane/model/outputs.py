"""What plugins produce: findings, adjustment proposals, and the signal sent to Layer 1."""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Literal, Mapping

Severity = Literal["low", "medium", "high", "critical"]
Method = Literal["deterministic", "semantic"]
AdjustmentAction = Literal["ALERT", "REQUIRE_APPROVAL_FOR", "BLOCK_TOOLS", "STRICT_MODE", "HALT_SESSION"]

SEVERITIES = ("low", "medium", "high", "critical")
# Tighten-only lattice, weakest first (docs/consumer-plane.md section 9.3).
ADJUSTMENT_ORDER: tuple[str, ...] = ("ALERT", "REQUIRE_APPROVAL_FOR", "BLOCK_TOOLS", "STRICT_MODE", "HALT_SESSION")
SEMANTIC_ALLOWED_ADJUSTMENTS = frozenset({"ALERT", "REQUIRE_APPROVAL_FOR"})


@dataclass(frozen=True, kw_only=True)
class FindingDraft:
    rule_id: str
    severity: Severity
    summary: str
    evidence_event_ids: tuple[str, ...] = ()
    confidence: float | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class Finding(FindingDraft):
    finding_id: str
    plugin: str
    plugin_version: str
    method: Method
    run_id: str | None
    session_id: str
    agent_id: str
    case_id: str | None
    trigger_event_id: str
    policy_version: str
    created_at: datetime

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["created_at"] = self.created_at.isoformat()
        d["evidence_event_ids"] = list(self.evidence_event_ids)
        d["details"] = dict(self.details)
        return d


def finding_id(plugin: str, plugin_version: str, event_id: str, rule_id: str, index: int) -> str:
    """Deterministic, so a retried event writes the same finding again instead of a new one."""
    digest = hashlib.sha256(f"{plugin}|{plugin_version}|{event_id}|{rule_id}|{index}".encode()).hexdigest()
    return f"fnd_{digest[:24]}"


@dataclass(frozen=True, kw_only=True)
class AdjustmentProposal:
    action: AdjustmentAction
    tools: tuple[str, ...] = ()
    ttl_s: int = 600
    reason: str = ""
    scope: Literal["session", "agent"] = "session"


@dataclass(frozen=True, kw_only=True)
class PolicyAdjustmentSignal:
    """Format from docs/application-documentation.md section 4.2, plus provenance fields."""
    signal_id: str
    ts: datetime
    target_scope: Mapping[str, str]
    action: AdjustmentAction
    policy_modifications: Mapping[str, Any]
    reason: str
    ttl_seconds: int
    source_plugin: str
    trigger_event_id: str

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["ts"] = self.ts.isoformat()
        return d
