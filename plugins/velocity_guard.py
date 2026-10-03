"""Example drop-in plugin: flags bursts of tool calls within one session."""
from datetime import timedelta

from consume_plane.sdk import AdjustmentProposal, FindingDraft, Subscription


class VelocityGuard:
    name = "velocity-guard"
    version = "1.0.0"
    method = "deterministic"
    subscription = Subscription(kinds=frozenset({"tool_use"}))

    async def setup(self, ctx):
        self.window = timedelta(seconds=ctx.config.get("window_s", 10))
        self.limit = ctx.config.get("max_calls", 8)

    async def handle(self, action, ctx):
        recent = (await ctx.trajectory()).tool_uses().since(action.ts - self.window)
        ctx.emit_metric("tool_calls_in_window", len(recent), agent=action.agent_id)
        if len(recent) > self.limit:
            ctx.emit_finding(FindingDraft(
                rule_id="velocity.tool_calls",
                severity="high",
                summary=f"{len(recent)} tool calls in {int(self.window.total_seconds())}s (limit {self.limit})",
                evidence_event_ids=tuple(a.event_id for a in recent),
            ))
            ctx.propose_adjustment(AdjustmentProposal(
                action="REQUIRE_APPROVAL_FOR", tools=("*",), ttl_s=300,
                reason="tool-call velocity above limit",
            ))


PLUGINS = [VelocityGuard]
