from __future__ import annotations

from typing import Protocol, Sequence

from ..model.outputs import Finding


class FindingSink(Protocol):
    async def write(self, findings: Sequence[Finding]) -> None: ...
