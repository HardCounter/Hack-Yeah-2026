"""What plugins produce: findings, adjustment proposals, and the signal sent to Layer 1."""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Literal, Mapping

Severity = Literal["low", "medium", "high", "critical"]
Method = Literal["deterministic", "semantic"]
from contracts.feedback import ADJUSTMENT_ORDER, SEMANTIC_ALLOWED_ADJUSTMENTS, AdjustmentAction  # noqa: F401

SEVERITIES = ("low", "medium", "high", "critical")


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


from contracts.feedback import PolicyAdjustmentSignal  # canonical public compatibility import
