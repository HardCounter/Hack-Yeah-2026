"""Read-only access to the persistence layer (docs/consumer-plane.md section 5)."""
from __future__ import annotations

from datetime import datetime
from typing import Callable, Iterable, Iterator, Protocol, Sequence, overload

from ..model.actions import ActionKind, AgentAction, ContentRef, Usage
from ..model.contract import TaskContract


class TrajectoryReader(Protocol):
    async def get(self, event_id: str) -> AgentAction | None: ...
    async def session(self, session_id: str, *, up_to_seq: int | None = None,
                      kinds: Iterable[ActionKind] | None = None,
                      limit: int | None = None) -> Sequence[AgentAction]: ...
    async def contract(self, session_id: str) -> TaskContract | None: ...
    async def content(self, ref: ContentRef) -> bytes: ...


class Trajectory(Sequence[AgentAction]):
    """Immutable, seq-ordered view of one session's actions."""

    def __init__(self, session_id: str, actions: Iterable[AgentAction]):
        self.session_id = session_id
        self._actions = tuple(sorted(actions, key=lambda a: a.seq))

    @overload
    def __getitem__(self, i: int) -> AgentAction: ...
    @overload
    def __getitem__(self, i: slice) -> Trajectory: ...

    def __getitem__(self, i):
        if isinstance(i, slice):
            return Trajectory(self.session_id, self._actions[i])
        return self._actions[i]

    def __len__(self) -> int:
        return len(self._actions)

    def __iter__(self) -> Iterator[AgentAction]:
        return iter(self._actions)

    def __repr__(self) -> str:
        return f"Trajectory({self.session_id!r}, {len(self)} actions)"

    def _where(self, pred: Callable[[AgentAction], bool]) -> Trajectory:
        return Trajectory(self.session_id, (a for a in self._actions if pred(a)))

    def of_kind(self, *kinds: str) -> Trajectory:
        return self._where(lambda a: a.kind in kinds)

    def tool_uses(self, tool: str | None = None, *, executed_only: bool = False) -> Trajectory:
        return self._where(lambda a: a.kind == "tool_use"
                           and (tool is None or a.payload.tool == tool)
                           and (not executed_only or a.executed))

    def prompts(self) -> Trajectory:
        return self.of_kind("prompt")

    def since(self, ts: datetime) -> Trajectory:
        return self._where(lambda a: a.ts >= ts)

    def last(self, n: int) -> Trajectory:
        return self[-n:] if n > 0 else Trajectory(self.session_id, ())

    def first(self, pred: Callable[[AgentAction], bool]) -> AgentAction | None:
        return next((a for a in self._actions if pred(a)), None)

    def count(self, pred: Callable[[AgentAction], bool] | None = None) -> int:
        return len(self._actions) if pred is None else sum(1 for a in self._actions if pred(a))

    def usage_total(self) -> Usage:
        total = Usage()
        for a in self._actions:
            if a.usage is not None:
                total = total + a.usage
        return total
