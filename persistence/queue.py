"""In-memory queuing and pub/sub consumer dispatching for Layer 2.

Provides:
1. IngestBuffer (Queue 1): Bounded, non-blocking ingestion buffer.
2. ConsumerDispatcher (Queue 2): Fan-out pub/sub dispatcher for downstream analytics/feeds.
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

from persistence.models import ActionEventEnvelope
from persistence.privacy import sanitize_event, token

logger = logging.getLogger(__name__)


class IngestBuffer:
    """Queue 1: High-throughput, bounded async in-memory ingestion buffer."""

    def __init__(self, maxsize: int = 10000) -> None:
        if isinstance(maxsize, bool) or not isinstance(maxsize, int) or maxsize <= 0:
            raise ValueError("maxsize must be positive")
        self.maxsize = maxsize
        self.rejected_events = 0
        self._queue: asyncio.Queue[ActionEventEnvelope] = asyncio.Queue(maxsize=maxsize)

    def put_nowait(self, event: ActionEventEnvelope) -> bool:
        """Enqueue an event without blocking the caller.

        Returns:
            True if enqueued successfully, False if buffer is full.
        """
        try:
            self._queue.put_nowait(sanitize_event(event))
            return True
        except asyncio.QueueFull:
            self.rejected_events += 1
            logger.warning("IngestBuffer capacity exhausted")
            return False

    async def put(
        self, event: ActionEventEnvelope, timeout: Optional[float] = None
    ) -> bool:
        """Enqueue an event, optionally waiting up to timeout seconds.

        Returns:
            True if enqueued successfully, False if timed out.
        """
        event = sanitize_event(event)
        if timeout is None:
            await self._queue.put(event)
            return True
        try:
            await asyncio.wait_for(self._queue.put(event), timeout=timeout)
            return True
        except (asyncio.TimeoutError, TimeoutError):
            self.rejected_events += 1
            logger.warning("IngestBuffer enqueue timeout")
            return False

    async def get_batch(
        self, max_items: int = 50, timeout: float = 0.05
    ) -> List[ActionEventEnvelope]:
        """Fetch a batch of events from the buffer.

        Waits up to timeout seconds for at least one item, then drains as many
        readily available items as possible up to max_items.
        """
        if max_items <= 0:
            return []

        batch: List[ActionEventEnvelope] = []
        try:
            first = await asyncio.wait_for(self._queue.get(), timeout=timeout)
            batch.append(first)
        except (asyncio.TimeoutError, TimeoutError):
            return batch

        while len(batch) < max_items:
            try:
                item = self._queue.get_nowait()
                batch.append(item)
            except asyncio.QueueEmpty:
                break

        return batch

    def acknowledge(self, batch: List[ActionEventEnvelope]) -> None:
        """Mark dequeued items complete only after their durable commit."""
        for _ in batch:
            self._queue.task_done()

    def qsize(self) -> int:
        """Return the current number of items in the queue."""
        return self._queue.qsize()

    def empty(self) -> bool:
        """Return True if the queue is empty, False otherwise."""
        return self._queue.empty()

    async def join(self) -> None:
        """Wait until all items in the queue have been processed."""
        await self._queue.join()


class ConsumerDispatcher:
    """Best-effort live feed. Durable analytics use the engine's outbox API."""

    def __init__(self) -> None:
        self.dropped_deliveries = 0
        self.filter_errors = 0
        self._subscribers: Dict[
            str,
            Tuple[
                asyncio.Queue[ActionEventEnvelope],
                Optional[Callable[[ActionEventEnvelope], bool]],
            ],
        ] = {}

    @property
    def subscribers(self) -> List[str]:
        """List active subscriber names."""
        return list(self._subscribers.keys())

    def subscribe(
        self,
        name: str,
        filter_fn: Optional[Callable[[ActionEventEnvelope], bool]] = None,
        maxsize: int = 1000,
    ) -> asyncio.Queue[ActionEventEnvelope]:
        """Register a subscriber queue with an optional filter predicate."""
        token(name, required=True)
        if isinstance(maxsize, bool) or not isinstance(maxsize, int) or maxsize <= 0:
            raise ValueError("Subscriber maxsize must be positive")
        if name in self._subscribers:
            raise ValueError("Subscriber is already registered")
        queue: asyncio.Queue[ActionEventEnvelope] = asyncio.Queue(maxsize=maxsize)
        self._subscribers[name] = (queue, filter_fn)
        return queue

    def unsubscribe(self, name: str) -> None:
        """Remove a subscriber and close its registration."""
        self._subscribers.pop(name, None)

    async def dispatch(self, event: ActionEventEnvelope) -> None:
        """Fan out an event to all matching subscribers non-blockingly."""
        for name, (queue, filter_fn) in list(self._subscribers.items()):
            try:
                snapshot = sanitize_event(event)
                if filter_fn is not None and not filter_fn(snapshot):
                    continue
                try:
                    queue.put_nowait(sanitize_event(event))
                except asyncio.QueueFull:
                    self.dropped_deliveries += 1
                    logger.warning("Live feed capacity exhausted; replay from EventStore")
            except Exception:
                self.filter_errors += 1
                logger.error("Live feed filter failed")

    async def dispatch_with_retry(
        self,
        consumer_name: str,
        consumer_fn: Callable[[ActionEventEnvelope], Awaitable[None]],
        event: ActionEventEnvelope,
        max_retries: int = 3,
        base_delay: float = 0.02,
    ) -> bool:
        """Deliver an event to an async consumer function with exponential backoff.

        Returns:
            True if successfully acknowledged, False if all retries exhausted.
        """
        if (isinstance(max_retries, bool) or not isinstance(max_retries, int) or not 0 <= max_retries <= 100
                or isinstance(base_delay, bool) or not isinstance(base_delay, (int, float))
                or not math.isfinite(base_delay) or not 0 <= base_delay <= 3600):
            raise ValueError("Invalid retry configuration")
        for attempt in range(max_retries + 1):
            try:
                await consumer_fn(sanitize_event(event))
                return True
            except Exception:
                if attempt < max_retries:
                    delay = min(2.0, base_delay * (2**attempt))
                    logger.warning("Consumer delivery failed; retrying")
                    await asyncio.sleep(delay)
                else:
                    logger.error("Consumer retry limit exhausted")
                    return False
        return False
