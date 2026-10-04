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
class DecisionDraft:
    """What a plugin decided at one decision point, recorded with `ctx.record_decision`.

    `decision` is an UPPER_SNAKE code (e.g. NO_CHANGE, LEVEL_RAISED, SKIPPED). `reasoning` is one short
    sentence built from codes, tool names and numbers only; never copy arguments or content into it.
    `factors` are the inputs behind the decision: a flat mapping of numbers, booleans, codes, or
    small maps of code -> number (e.g. signal counts).
    """
    decision: str
    reasoning: str = ""
    factors: Mapping[str, Any] = field(default_factory=dict)


DecisionOutcome = Literal["decided", "failed", "dead_lettered"]


@dataclass(frozen=True, kw_only=True)
class PluginDecision:
    """One traced control-plane action: a plugin decision, or a failed/abandoned plugin run."""
    decision_id: str
    ts: datetime
    plugin: str
    plugin_version: str
    method: Method
    outcome: DecisionOutcome
    decision: str
    reasoning: str
    reason: str | None                  # set only when outcome != "decided": a fixed failure code
    factors: Mapping[str, Any]
    session_id: str
    run_id: str | None
    agent_id: str
    case_id: str | None
    trigger_event_id: str
    trigger_seq: int
    attempt: int
    duration_ms: float
    finding_ids: tuple[str, ...] = ()
    adjustments: tuple[Mapping[str, str], ...] = ()   # {"action": ..., "outcome": accepted | <rejection code>}

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["ts"] = self.ts.isoformat()
        d["finding_ids"] = list(self.finding_ids)
        d["adjustments"] = [dict(a) for a in self.adjustments]
        d["factors"] = dict(self.factors)
        return d


def decision_id(plugin: str, plugin_version: str, event_id: str, outcome: str, index: int, attempt: int) -> str:
    """Deterministic, so a redelivered event records the same decision instead of a new one."""
    digest = hashlib.sha256(f"{plugin}|{plugin_version}|{event_id}|{outcome}|{index}|{attempt}".encode()).hexdigest()
    return f"dec_{digest[:24]}"


@dataclass(frozen=True, kw_only=True)
class AdjustmentProposal:
    action: AdjustmentAction
    tools: tuple[str, ...] = ()
    ttl_s: int = 600
    reason: str = ""
    scope: Literal["session", "agent"] = "session"


from contracts.feedback import PolicyAdjustmentSignal  # canonical public compatibility import
