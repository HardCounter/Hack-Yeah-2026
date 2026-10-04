"""PluginContext implementation: read access, permission checks, buffered outputs."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, TypeVar

from contracts.action import ActionKind, AgentAction, ContentRef
from contracts.task_contract import TaskContract
from ..model.outputs import SEVERITIES, AdjustmentProposal, DecisionDraft, FindingDraft
from ..ports.trajectory import Trajectory, TrajectoryReader
from .registry import LoadedPlugin

T = TypeVar("T")
_UNSET = object()


@dataclass
class OutputBuffer:
    """Committed only if handle() returns normally, so a retry does not duplicate outputs."""
    findings: list[FindingDraft] = field(default_factory=list)
    metrics: list[tuple[str, float, dict[str, str]]] = field(default_factory=list)
    proposals: list[AdjustmentProposal] = field(default_factory=list)
    decisions: list[DecisionDraft] = field(default_factory=list)


class PluginContextImpl:
    def __init__(self, plugin: LoadedPlugin, action: AgentAction, reader: TrajectoryReader):
        self._plugin = plugin
        self._action = action
        self._reader = reader
        self._contract: Any = _UNSET
        self.plugin_name = plugin.name
        self.config = plugin.config
        self.log = plugin.log
        self.buffer = OutputBuffer()

    async def trajectory(self, *, up_to: int | None = None,
                         kinds: Iterable[ActionKind] | None = None) -> Trajectory:
        """Session snapshot as of the handled event; `up_to` can narrow it but never look ahead."""
        if not self._plugin.subscription.needs_trajectory:
            raise PermissionError(f"{self.plugin_name}: subscription has needs_trajectory=False")
        limit = self._action.seq if up_to is None else min(up_to, self._action.seq)
        actions = await self._reader.session(self._action.session_id, up_to_seq=limit, kinds=kinds)
        return Trajectory(self._action.session_id, actions)

    async def contract(self) -> TaskContract | None:
        if self._contract is _UNSET:
            self._contract = await self._reader.contract(self._action.session_id)
        return self._contract

    async def content(self, ref: ContentRef) -> bytes:
        if not self._plugin.subscription.needs_content:
            raise PermissionError(f"{self.plugin_name}: subscription has needs_content=False")
        return await self._reader.content(ref)

    def emit_finding(self, finding: FindingDraft) -> None:
        if finding.severity not in SEVERITIES:
            raise ValueError(f"unknown severity {finding.severity!r}")
        if self._plugin.method == "semantic":
            if finding.confidence is None or not 0.0 <= finding.confidence <= 1.0:
                raise ValueError("semantic findings need a confidence in [0, 1]")
            if finding.severity == "critical":
                finding = replace(finding, severity="high",
                                  details={**finding.details, "severity_capped_from": "critical"})
        self.buffer.findings.append(finding)

    def emit_metric(self, name: str, value: float, **labels: str) -> None:
        self.buffer.metrics.append((name, float(value), labels))

    def propose_adjustment(self, proposal: AdjustmentProposal) -> None:
        self.buffer.proposals.append(proposal)

    def record_decision(self, decision: str, reasoning: str = "", **factors: Any) -> None:
        self.buffer.decisions.append(DecisionDraft(decision=decision, reasoning=reasoning, factors=factors))

    async def run_blocking(self, fn: Callable[..., T], *args: Any) -> T:
        return await asyncio.to_thread(fn, *args)
