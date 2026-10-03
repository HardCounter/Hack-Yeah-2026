"""Surface deterministic gateway hard denies in the trajectory audit."""
from __future__ import annotations

from consume_plane.sdk import FindingDraft, Subscription


_RULE_FAMILIES = {
    "TASK_SCOPE": "TASK_SCOPE_DENY",
    "SCOPE_DENIED": "TASK_SCOPE_DENY",
    "OUT_OF_SCOPE_TARGET": "TASK_SCOPE_DENY",
    "PREREQUISITE_MISSING": "PREREQUISITE_DENY",
    "SCREENING_REQUIRED": "PREREQUISITE_DENY",
    "SANCTIONS_SCREENING_REQUIRED": "PREREQUISITE_DENY",
    "BUDGET_EXHAUSTED": "BUDGET_DENY",
    "TOOL_BUDGET_EXHAUSTED": "BUDGET_DENY",
    "TOKEN_BUDGET_EXHAUSTED": "BUDGET_DENY",
    "COST_BUDGET_EXHAUSTED": "BUDGET_DENY",
    "UNKNOWN_RUN": "IDENTITY_DENY",
    "IDENTITY_MISMATCH": "IDENTITY_DENY",
    "CONTRACT_IDENTITY_MISMATCH": "IDENTITY_DENY",
    "PRINCIPAL_MISMATCH": "IDENTITY_DENY",
}


class GatewayViolations:
    """Post-event finding for hard gateway denials; it does not enforce policy."""

    name = "gateway-violations"
    version = "1.0.0"
    method = "deterministic"
    subscription = Subscription(
        kinds=frozenset({"tool_use"}),
        statuses=frozenset({"blocked", "pending_approval"}),
        needs_trajectory=False,
    )

    async def handle(self, action, ctx):
        gateway = action.gateway
        if gateway is None:
            family, code = "HARD_DENY", "UNCLASSIFIED_HARD_DENY"
        elif action.status == "pending_approval" or gateway.final == "REQUIRE_APPROVAL":
            family, code = "APPROVAL_REQUIRED", "APPROVAL_REQUIRED"
        else:
            raw_codes = [d.rule_id for d in gateway.decisions if d.rule_id]
            families = []
            for raw in raw_codes:
                normalized = raw.upper().replace(".", "_").replace("-", "_")
                family = _RULE_FAMILIES.get(normalized)
                if family:
                    families.append((family, normalized))
            family, code = families[0] if families else ("HARD_DENY", "UNCLASSIFIED_HARD_DENY")

        ctx.emit_finding(FindingDraft(
            rule_id=f"gateway.{family.lower()}",
            severity="medium" if family == "APPROVAL_REQUIRED" else "high",
            summary=f"Gateway intervention observed: {family}",
            evidence_event_ids=(action.event_id,),
            details={"reason_code": code, "gateway_decision": gateway.final if gateway else "BLOCK"},
        ))
