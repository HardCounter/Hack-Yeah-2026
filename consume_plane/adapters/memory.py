"""In-memory adapters for tests and the all-in-one daemon."""
from __future__ import annotations

import asyncio
from collections import deque
from typing import Iterable, Sequence

from contracts.action import ActionKind, AgentAction, ContentRef
from contracts.task_contract import TaskContract
from ..model.outputs import Finding, PolicyAdjustmentSignal
from ..ports.event_source import Delivery


class MemoryEventSource:
    """asyncio-friendly Queue 2 stand-in with ack/nack, delayed redelivery and a dead-letter list."""

    def __init__(self, actions: Iterable[AgentAction] = (), *, max_redeliveries: int = 5):
        self.max_redeliveries = max_redeliveries
        self._ready: deque[Delivery] = deque()
        self._delayed: list[tuple[float, Delivery]] = []
        self._inflight: dict[str, Delivery] = {}
        self._counter = 0
        self.acked: list[str] = []                       # event_ids, in ack order
        self.nacks: list[tuple[str, str]] = []           # (event_id, reason)
        self.dead_letters: list[tuple[Delivery, str]] = []
        for action in actions:
            self.put(action)

    def _next_id(self) -> str:
        self._counter += 1
        return f"dlv_{self._counter}"

    def put(self, action: AgentAction) -> None:
        self._ready.append(Delivery(self._next_id(), action, 1))

    def _promote_due(self, now: float) -> None:
        due = [item for item in self._delayed if item[0] <= now]
        for item in due:
            self._delayed.remove(item)
            self._ready.append(item[1])

    async def receive(self, max_items: int, timeout_s: float) -> Sequence[Delivery]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while True:
            self._promote_due(loop.time())
            if self._ready:
                batch = [self._ready.popleft() for _ in range(min(max_items, len(self._ready)))]
                self._inflight.update((d.delivery_id, d) for d in batch)
                return batch
            remaining = deadline - loop.time()
            if remaining <= 0:
                return []
            await asyncio.sleep(min(0.01, remaining))

    async def ack(self, delivery_id: str) -> None:
        self.acked.append(self._inflight.pop(delivery_id).action.event_id)

    async def nack(self, delivery_id: str, *, reason: str, retry_after_s: float | None = None) -> None:
        d = self._inflight.pop(delivery_id)
        self.nacks.append((d.action.event_id, reason))
        if d.attempt >= self.max_redeliveries:
            self.dead_letters.append((d, reason))
            return
        due = asyncio.get_running_loop().time() + (retry_after_s or 0.0)
        self._delayed.append((due, Delivery(self._next_id(), d.action, d.attempt + 1)))

    def is_idle(self) -> bool:
        return not (self._ready or self._delayed or self._inflight)


class MemoryTrajectoryReader:
    def __init__(self, actions: Iterable[AgentAction] = (), contracts: Iterable[TaskContract] = ()):
        self._by_id: dict[str, AgentAction] = {}
        self._by_session: dict[str, list[AgentAction]] = {}
        self._contracts: dict[str, TaskContract] = {}
        self._content: dict[str, bytes] = {}
        for a in actions:
            self.add(a)
        for c in contracts:
            self.add_contract(c)

    def add(self, action: AgentAction) -> None:
        if action.event_id in self._by_id:
            return
        self._by_id[action.event_id] = action
        self._by_session.setdefault(action.session_id, []).append(action)

    def add_contract(self, contract: TaskContract) -> None:
        self._contracts[contract.session_id] = contract

    def add_content(self, ref: str, body: bytes) -> None:
        self._content[ref] = body

    async def get(self, event_id: str) -> AgentAction | None:
        return self._by_id.get(event_id)

    async def session(self, session_id: str, *, up_to_seq: int | None = None,
                      kinds: Iterable[ActionKind] | None = None,
                      limit: int | None = None) -> Sequence[AgentAction]:
        """Actions in seq order; `limit` keeps the most recent N."""
        wanted = None if kinds is None else frozenset(kinds)
        actions = sorted(
            (a for a in self._by_session.get(session_id, ())
             if (up_to_seq is None or a.seq <= up_to_seq) and (wanted is None or a.kind in wanted)),
            key=lambda a: a.seq,
        )
        return actions[-limit:] if limit else actions

    async def contract(self, session_id: str) -> TaskContract | None:
        return self._contracts.get(session_id)

    async def content(self, ref: ContentRef) -> bytes:
        try:
            return self._content[ref.ref]
        except KeyError:
            raise LookupError(f"content not found: {ref.ref}") from None


class MemorySink:
    def __init__(self):
        self.findings: dict[str, Finding] = {}   # by finding_id, so rewrites dedupe
        self.writes = 0

    async def write(self, findings: Sequence[Finding]) -> None:
        self.writes += 1
        for f in findings:
            self.findings[f.finding_id] = f


class MemoryFeedbackChannel:
    """In-process channel; Layer 1 reads `signals` (or subscribes) in the all-in-one daemon."""

    def __init__(self):
        self.signals: list[PolicyAdjustmentSignal] = []

    async def publish(self, signal: PolicyAdjustmentSignal) -> None:
        self.signals.append(signal)
