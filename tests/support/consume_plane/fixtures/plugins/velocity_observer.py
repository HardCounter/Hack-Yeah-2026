"""Consumer-SDK replay fixture only; production velocity enforcement lives in Layer 1."""
from datetime import timedelta

from consume_plane.sdk import AdjustmentProposal, FindingDraft, Subscription


class VelocityObserver:
    name = "velocity-observer"
    version = "1.0.0"
    method = "deterministic"
    subscription = Subscription(kinds=frozenset({"tool_use"}))

    async def setup(self, ctx):
        self.window = timedelta(seconds=ctx.config.get("window_s", 10))
        self.limit = ctx.config.get("max_calls", 8)

    async def handle(self, action, ctx):
        recent = (await ctx.trajectory()).tool_uses().since(action.ts - self.window)
        if len(recent) > self.limit:
            ctx.emit_finding(FindingDraft(
                rule_id="velocity.tool_calls", severity="high", summary="Fixture call burst",
                evidence_event_ids=tuple(a.event_id for a in recent),
            ))
            ctx.propose_adjustment(AdjustmentProposal(
                action="REQUIRE_APPROVAL_FOR", tools=("*",), ttl_s=300,
                reason="fixture call burst",
            ))


PLUGINS = [VelocityObserver]
