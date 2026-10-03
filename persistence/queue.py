"""In-memory queuing and pub/sub consumer dispatching for Layer 2.

Provides:
1. IngestBuffer (Queue 1): Bounded, non-blocking ingestion buffer.
2. ConsumerDispatcher (Queue 2): Fan-out pub/sub dispatcher for downstream analytics/feeds.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

from persistence.models import ActionEventEnvelope

logger = logging.getLogger(__name__)


class IngestBuffer:
    """Queue 1: High-throughput, bounded async in-memory ingestion buffer."""

    def __init__(self, maxsize: int = 10000) -> None:
        self.maxsize = maxsize
        self._queue: asyncio.Queue[ActionEventEnvelope] = asyncio.Queue(maxsize=maxsize)

    def put_nowait(self, event: ActionEventEnvelope) -> bool:
        """Enqueue an event without blocking the caller.

        Returns:
            True if enqueued successfully, False if buffer is full.
        """
        try:
            self._queue.put_nowait(event)
            return True
        except asyncio.QueueFull:
            logger.warning(
                "IngestBuffer is full (maxsize=%d), dropping event %s",
                self.maxsize,
                event.event_id,
            )
            return False

    async def put(
        self, event: ActionEventEnvelope, timeout: Optional[float] = None
    ) -> bool:
        """Enqueue an event, optionally waiting up to timeout seconds.

        Returns:
            True if enqueued successfully, False if timed out.
        """
        if timeout is None:
            await self._queue.put(event)
            return True
        try:
            await asyncio.wait_for(self._queue.put(event), timeout=timeout)
            return True
        except (asyncio.TimeoutError, TimeoutError):
            logger.warning(
                "Timed out waiting to enqueue event %s into IngestBuffer", event.event_id
            )
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
            self._queue.task_done()
        except (asyncio.TimeoutError, TimeoutError):
            return batch

        while len(batch) < max_items:
            try:
                item = self._queue.get_nowait()
                batch.append(item)
                self._queue.task_done()
            except asyncio.QueueEmpty:
                break

        return batch

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
    """Queue 2: Fan-out pub/sub dispatcher for downstream modules (SSE, graders, webhooks)."""

    def __init__(self) -> None:
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
                if filter_fn is not None and not filter_fn(event):
                    continue
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:
                    logger.warning(
                        "Subscriber %s queue full, dropping event %s",
                        name,
                        event.event_id,
                    )
            except Exception as exc:
                logger.error("Error dispatching event to %s: %s", name, exc)

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
        for attempt in range(max_retries + 1):
            try:
                await consumer_fn(event)
                return True
            except Exception as exc:
                if attempt < max_retries:
                    delay = base_delay * (2**attempt)
                    logger.warning(
                        "Consumer %s attempt %d/%d failed (%s); retrying in %.3fs",
                        consumer_name,
                        attempt + 1,
                        max_retries,
                        exc,
                        delay,
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.error(
                        "Consumer %s failed after %d retries for event %s: %s",
                        consumer_name,
                        max_retries,
                        event.event_id,
                        exc,
                    )
                    return False
        return False
