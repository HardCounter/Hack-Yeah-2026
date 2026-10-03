"""Tighten-only signal crossing the consumer/interception boundary."""
from __future__ import annotations
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Any, Literal, Mapping

AdjustmentAction = Literal["ALERT", "REQUIRE_APPROVAL_FOR", "BLOCK_TOOLS", "STRICT_MODE", "HALT_SESSION"]

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
