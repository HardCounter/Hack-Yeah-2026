from __future__ import annotations

from typing import Protocol

from ..model.outputs import PolicyAdjustmentSignal


class FeedbackChannel(Protocol):
    """Transport of accepted signals to Layer 1's dynamic policy cache."""

    async def publish(self, signal: PolicyAdjustmentSignal) -> None: ...
