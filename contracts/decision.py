"""Canonical synchronous decision vocabulary."""
from dataclasses import asdict, dataclass
from typing import Any, Mapping

DECISIONS = frozenset({"ALLOW", "BLOCK", "REDACT", "REQUIRE_APPROVAL", "ALERT"})


@dataclass(frozen=True, kw_only=True)
class GatewayDecision:
    action_id: str
    decision: str
    reason_code: str | None
    policy_version: str
    auditor_decisions: tuple[Mapping[str, Any], ...] = ()
    modified_arguments: Mapping[str, Any] | None = None
    interception_overhead_ms: float = 0.0

    def __post_init__(self):
        if self.decision not in DECISIONS:
            raise ValueError("unknown gateway decision")

    def to_dict(self):
        return asdict(self)
