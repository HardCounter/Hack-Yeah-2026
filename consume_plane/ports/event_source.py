"""Queue 2 as seen by the consume plane (docs/consumer-plane.md section 6)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

from ..model.actions import AgentAction


@dataclass(frozen=True)
class Delivery:
    delivery_id: str
    action: AgentAction
    attempt: int = 1


class EventSource(Protocol):
    async def receive(self, max_items: int, timeout_s: float) -> Sequence[Delivery]: ...
    async def ack(self, delivery_id: str) -> None: ...
    async def nack(self, delivery_id: str, *, reason: str, retry_after_s: float | None = None) -> None: ...


def source_is_idle(source: object) -> bool:
    """Finite sources (replay, tests) expose is_idle(); live queues never report idle."""
    is_idle = getattr(source, "is_idle", None)
    return bool(is_idle()) if callable(is_idle) else False
