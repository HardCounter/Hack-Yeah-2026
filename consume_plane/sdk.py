"""Everything a plugin author needs. Plugins may import from here; they do not have to subclass anything.

    from consume_plane.sdk import Subscription, FindingDraft, AdjustmentProposal
"""
from .model.actions import (
    ActionKind,
    ActionStatus,
    AgentAction,
    ApprovalPayload,
    ContentRef,
    ControlPayload,
    EgressPayload,
    PromptPayload,
    SessionPayload,
    ToolCallIntent,
    ToolUsePayload,
    UnknownPayload,
    Usage,
)
from .model.contract import Budget, TaskContract
from .model.outputs import AdjustmentProposal, FindingDraft
from .ports.plugin import ConsumerPlugin, PluginContext, SetupContext, Subscription
from .ports.trajectory import Trajectory

__all__ = [
    "ActionKind", "ActionStatus", "AgentAction", "ApprovalPayload", "ContentRef", "ControlPayload",
    "EgressPayload", "PromptPayload", "SessionPayload", "ToolCallIntent", "ToolUsePayload", "UnknownPayload",
    "Usage", "Budget", "TaskContract", "AdjustmentProposal", "FindingDraft", "ConsumerPlugin", "PluginContext",
    "SetupContext", "Subscription", "Trajectory",
]
