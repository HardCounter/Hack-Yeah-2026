"""Feedback Controller: decides which plugin proposals become signals to Layer 1 (section 9.3).

Proposals can only add restrictions; there is no proposal that removes one, so relaxing happens
only through TTL expiry, a policy-file change, or an operator.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta

from contracts.action import AgentAction
from ..model.outputs import (
    ADJUSTMENT_ORDER,
    SEMANTIC_ALLOWED_ADJUSTMENTS,
    AdjustmentProposal,
    PolicyAdjustmentSignal,
)
from ..ports.feedback import FeedbackChannel
from .config import FeedbackConfig

TOOL_ACTIONS = frozenset({"REQUIRE_APPROVAL_FOR", "BLOCK_TOOLS"})
RATE_WINDOW = timedelta(minutes=1)


@dataclass(frozen=True)
class FeedbackDecision:
    accepted: bool
    reason: str
    plugin: str
    trigger_event_id: str
    proposal: AdjustmentProposal
    signal: PolicyAdjustmentSignal | None = None


@dataclass
class _Active:
    action: str
    tools: frozenset[str]
    expires_at: datetime


def _modifications(p: AdjustmentProposal) -> dict:
    return {
        "ALERT": {},
        "REQUIRE_APPROVAL_FOR": {"require_approval_for": list(p.tools)},
        "BLOCK_TOOLS": {"blocked_tools": list(p.tools)},
        "STRICT_MODE": {"strict_mode": True},
        "HALT_SESSION": {"halt": True},
    }[p.action]


class FeedbackController:
    def __init__(self, cfg: FeedbackConfig, channel: FeedbackChannel):
        self.cfg = cfg
        self.channel = channel
        self.log: list[FeedbackDecision] = []
        self._active: dict[tuple[str, str], list[_Active]] = {}
        self._recent: dict[str, list[datetime]] = {}

    @staticmethod
    def _scope_key(p: AdjustmentProposal, a: AgentAction) -> tuple[str, str]:
        return ("agent", a.agent_id) if p.scope == "agent" else ("session", a.session_id)

    def _reject_reason(self, p: AdjustmentProposal, plugin: str, method: str, a: AgentAction) -> str | None:
        if not self.cfg.enabled:
            return "feedback_disabled"
        if p.action not in ADJUSTMENT_ORDER:
            return "unknown_action"
        if method == "semantic" and p.action not in SEMANTIC_ALLOWED_ADJUSTMENTS:
            return "semantic_not_allowed"
        if p.action not in self.cfg.allowed_actions.get(plugin, ()):
            return "not_allowed_for_plugin"
        if p.scope not in ("session", "agent"):
            return "unknown_scope"
        if p.scope == "agent" and not self.cfg.allow_agent_scope:
            return "agent_scope_disabled"
        if p.action in TOOL_ACTIONS and not p.tools:
            return "missing_tools"
        if p.ttl_s <= 0:
            return "bad_ttl"
        now = a.ts
        active = [x for x in self._active.get(self._scope_key(p, a), ()) if x.expires_at > now]
        if any(x.action == "HALT_SESSION" or (x.action == p.action and set(p.tools) <= x.tools) for x in active):
            return "already_active"
        recent = [t for t in self._recent.get(a.session_id, ()) if t > now - RATE_WINDOW]
        if len(recent) >= self.cfg.max_signals_per_session_per_minute:
            return "rate_limited"
        return None

    async def submit(self, p: AdjustmentProposal, *, plugin: str, method: str, action: AgentAction) -> FeedbackDecision:
        reason = self._reject_reason(p, plugin, method, action)
        if reason is not None:
            decision = FeedbackDecision(False, reason, plugin, action.event_id, p)
            self.log.append(decision)
            return decision

        ttl = min(p.ttl_s, self.cfg.max_ttl_s)
        digest = hashlib.sha256(f"{plugin}|{action.event_id}|{p.action}|{sorted(p.tools)}".encode()).hexdigest()
        scope = ({"agent_id": action.agent_id} if p.scope == "agent"
                 else {"session_id": action.session_id, "agent_id": action.agent_id})
        signal = PolicyAdjustmentSignal(
            signal_id=f"sig_{digest[:20]}",
            ts=action.ts,
            target_scope=scope,
            action=p.action,
            policy_modifications=_modifications(p),
            reason=p.reason,
            ttl_seconds=ttl,
            source_plugin=plugin,
            trigger_event_id=action.event_id,
        )
        await self.channel.publish(signal)
        self._active.setdefault(self._scope_key(p, action), []).append(
            _Active(p.action, frozenset(p.tools), action.ts + timedelta(seconds=ttl)))
        self._recent.setdefault(action.session_id, []).append(action.ts)
        decision = FeedbackDecision(True, "accepted", plugin, action.event_id, p, signal)
        self.log.append(decision)
        return decision
