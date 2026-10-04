from __future__ import annotations

from typing import Protocol, Sequence

from ..model.outputs import Finding, PluginDecision


class FindingSink(Protocol):
    async def write(self, findings: Sequence[Finding]) -> None: ...


class DecisionSink(Protocol):
    """Optional on a FindingSink: receives the control-plane decision trace."""
    async def write_decisions(self, decisions: Sequence[PluginDecision]) -> None: ...
