"""The plugin contract (docs/consumer-plane.md section 4). Plugins satisfy it structurally."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, ClassVar, Iterable, Literal, Mapping, Protocol, TypeVar

from contracts.action import ActionKind, ActionStatus, AgentAction, ContentRef
from contracts.task_contract import TaskContract
from ..model.outputs import AdjustmentProposal, FindingDraft
from .trajectory import Trajectory

T = TypeVar("T")


@dataclass(frozen=True)
class Subscription:
    kinds: frozenset[str]                       # ActionKind values, or "*" for everything incl. unknown kinds
    agents: frozenset[str] | None = None
    tools: frozenset[str] | None = None         # applies to tool_use actions only
    statuses: frozenset[ActionStatus] | None = None
    needs_content: bool = False
    needs_trajectory: bool = True

    def matches(self, action: AgentAction) -> bool:
        if "*" not in self.kinds and (not action.known_kind or action.kind not in self.kinds):
            return False
        if self.agents is not None and action.agent_id not in self.agents:
            return False
        if self.statuses is not None and action.status not in self.statuses:
            return False
        if self.tools is not None and action.kind == "tool_use" and action.payload.tool not in self.tools:
            return False
        return True


@dataclass(frozen=True)
class SetupContext:
    plugin_name: str
    config: Mapping[str, Any]
    log: logging.Logger


class PluginContext(Protocol):
    plugin_name: str
    config: Mapping[str, Any]
    log: logging.Logger

    async def trajectory(self, *, up_to: int | None = None,
                         kinds: Iterable[ActionKind] | None = None) -> Trajectory: ...
    async def contract(self) -> TaskContract | None: ...
    async def content(self, ref: ContentRef) -> bytes: ...

    def emit_finding(self, finding: FindingDraft) -> None: ...
    def emit_metric(self, name: str, value: float, **labels: str) -> None: ...
    def propose_adjustment(self, proposal: AdjustmentProposal) -> None: ...
    def record_decision(self, decision: str, reasoning: str = "", **factors: Any) -> None:
        """Trace a decision point. Persisted with the run's findings and proposals, linked to them."""

    async def run_blocking(self, fn: Callable[..., T], *args: Any) -> T: ...


class ConsumerPlugin(Protocol):
    name: ClassVar[str]
    version: ClassVar[str]
    method: ClassVar[Literal["deterministic", "semantic"]]
    subscription: ClassVar[Subscription]

    async def setup(self, ctx: SetupContext) -> None: ...
    async def handle(self, action: AgentAction, ctx: PluginContext) -> None: ...
    async def teardown(self) -> None: ...
