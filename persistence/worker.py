"""Background persistence worker and high-level PersistenceEngine facade.

Coordinates asynchronous ingestion from IngestBuffer, batch writes to EventStore,
and outbox-backed delivery to registered analytics consumers.
"""

from __future__ import annotations

import asyncio
from enum import Enum
import logging
import math
from pathlib import Path
import random
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union

from persistence.models import (
    ActionDetails,
    AuditContext,
    AuditorVerdict,
    ActionEventEnvelope,
    ActionStatus,
    ActionType,
    AlertEvent,
    AuditActionRecord,
    ConsumerResult,
    InterceptionMetadata,
    Severity,
)
from persistence.queue import ConsumerDispatcher, IngestBuffer
from persistence.store import EventStore
from persistence.privacy import sanitize_event, sanitize_alert, sanitize_audit

logger = logging.getLogger(__name__)


class EngineState(str, Enum):
    """Explicit lifecycle states for PersistenceEngine."""

    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    QUIESCING = "QUIESCING"
    FAILED = "FAILED"


class PersistenceWorker:
    """Commit volatile telemetry batches and deliver durable analytics jobs.

    Volatile ingestion is retained across transient write failures but cannot
    survive process death. Critical evidence must use engine.emit_action().
    """

    def __init__(
        self,
        ingest_buffer: IngestBuffer,
        store: EventStore,
        consumer_dispatcher: Optional[ConsumerDispatcher] = None,
        batch_size: int = 50,
        poll_timeout: float = 0.05,
        concurrency: int = 2,
    ):
        if (
            isinstance(batch_size, bool)
            or not isinstance(batch_size, int)
            or batch_size <= 0
            or isinstance(poll_timeout, bool)
            or not isinstance(poll_timeout, (int, float))
            or not math.isfinite(poll_timeout)
            or poll_timeout <= 0
        ):
            raise ValueError("batch_size and poll_timeout must be positive")
        self.ingest_buffer = ingest_buffer
        self.store = store
        self.consumer_dispatcher = consumer_dispatcher
        self.batch_size = batch_size
        self.poll_timeout = poll_timeout
        self.concurrency = concurrency
        self._running = False
        self._tasks: list[asyncio.Task] = []
        self._batch: list[ActionEventEnvelope] = []
        self._active_consumers: set[str] = set()
        self._claim_lock = asyncio.Lock()
        self.last_error = None
        self.consumers: dict[str, Callable[[ActionEventEnvelope], Awaitable[Any]]] = {}
        self.max_retries = 3
        self.consumer_timeout = 5.0

    @property
    def delivery_names(self) -> list[str]:
        return ["live-feed", *self.consumers]

    @property
    def _delivering(self) -> bool:
        return bool(self._active_consumers)

    @_delivering.setter
    def _delivering(self, value: bool) -> None:
        pass

    @property
    def _task(self) -> Optional[asyncio.Task]:
        return self._tasks[0] if self._tasks else None

    async def start(self) -> None:
        if not self._running:
            await self.store.register_consumer("live-feed")
            self._running = True
            self._tasks.append(asyncio.create_task(self._ingest_loop()))
            for _ in range(self.concurrency):
                self._tasks.append(asyncio.create_task(self._delivery_worker()))

    async def _process_batch(self, batch: list[ActionEventEnvelope]) -> None:
        await self.store.append_with_outbox(batch)
        self.ingest_buffer.acknowledge(batch)

    async def _deliver_one(self) -> bool:
        async with self._claim_lock:
            available_names = [n for n in self.delivery_names if n not in self._active_consumers]
            if not available_names:
                return False
            delivery = await self.store.claim_delivery(
                available_names,
                lease_seconds=self.consumer_timeout + 30,
                max_retries=self.max_retries,
            )
            if delivery is None:
                return False
            event, name, attempts, lease_id = delivery
            self._active_consumers.add(name)

        try:
            failed = False
            consumer_result: Optional[ConsumerResult] = None
            try:
                if name == "live-feed":
                    if self.consumer_dispatcher is not None:
                        await self.consumer_dispatcher.dispatch(event)
                else:
                    callback = self.consumers.get(name)
                    if callback is not None:
                        res = await asyncio.wait_for(
                            callback(sanitize_event(event)),
                            timeout=self.consumer_timeout,
                        )
                        if isinstance(res, ConsumerResult):
                            consumer_result = res
            except Exception:
                failed = True
                logger.warning("Analytics consumer delivery failed")

            delay = min(2.0, 0.02 * 2 ** min(attempts, 8)) * random.uniform(0.8, 1.2)
            if failed:
                await self.store.finish_delivery(
                    event, name, lease_id, failed=True,
                    max_retries=self.max_retries, delay=delay,
                )
            elif consumer_result is not None:
                await self.store.commit_consumer_result(
                    event.event_id, name, lease_id, consumer_result
                )
            else:
                await self.store.finish_delivery(
                    event, name, lease_id, failed=False,
                    max_retries=self.max_retries, delay=delay,
                )
        finally:
            self._active_consumers.discard(name)
        return True

    async def _ingest_loop(self) -> None:
        while self._running:
            try:
                if not self._batch:
                    self._batch = await self.ingest_buffer.get_batch(
                        max_items=self.batch_size, timeout=self.poll_timeout
                    )
                if self._batch:
                    await self._process_batch(self._batch)
                    self._batch = []
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = type(exc).__name__
                logger.error("Persistence worker ingestion failed")
                await asyncio.sleep(self.poll_timeout)

    async def _delivery_worker(self) -> None:
        while self._running:
            try:
                did_work = await self._deliver_one()
                if not did_work:
                    await asyncio.sleep(self.poll_timeout)
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = type(exc).__name__
                logger.error("Persistence worker delivery failed")
                await asyncio.sleep(self.poll_timeout)

    async def flush(self, timeout: Optional[float] = 5.0) -> None:
        """Wait for committed ingestion and delivery; raise on timeout/failure."""
        async def drain() -> None:
            await self.ingest_buffer.join()
            while (
                bool(self._active_consumers)
                or bool(self._batch)
                or (await self.store.pending_deliveries(self.delivery_names, active_only=True) > 0)
            ):
                if not self._running:
                    raise RuntimeError("Persistence worker is not running")
                for task in self._tasks:
                    if task.done() and task.exception():
                        raise RuntimeError(f"Persistence worker task failed: {task.exception()}")
                await asyncio.sleep(self.poll_timeout)

        if not self._running:
            raise RuntimeError("Persistence worker is not running")
        for task in self._tasks:
            if task.done() and task.exception():
                raise RuntimeError(f"Persistence worker task failed: {task.exception()}")

        if timeout is None:
            await drain()
        else:
            await asyncio.wait_for(drain(), timeout=timeout)

    async def stop(self, timeout: Optional[float] = 5.0) -> None:
        """Drain before stopping. On timeout the worker remains alive for recovery."""
        if not self._running:
            return
        await self.flush(timeout)
        self._running = False
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()


class PersistenceEngine:
    """Gateway-private storage facade; it has no model-facing or public endpoint.

    Only trusted orchestration code may supply context/IDs and register callbacks.
    Storage syntax checks do not authenticate callers or bind policy authority.

    Supports usage as an async context manager:
        async with PersistenceEngine(db_path) as engine:
            await engine.emit_action(...)
    """

    def __init__(
        self,
        db_path: Union[str, Path],
        buffer_maxsize: int = 10000,
        batch_size: int = 50,
        poll_timeout: float = 0.05,
        outbox_maxsize: int = 10000,
    ) -> None:
        self.db_path = str(db_path)
        if self.db_path in (":memory:", ""):
            raise ValueError("PersistenceEngine requires a file-backed database")
        self.store = EventStore(self.db_path, outbox_maxsize=outbox_maxsize)
        self.ingest_buffer = IngestBuffer(maxsize=buffer_maxsize)
        self.dispatcher = ConsumerDispatcher()
        self.worker = PersistenceWorker(
            ingest_buffer=self.ingest_buffer,
            store=self.store,
            consumer_dispatcher=self.dispatcher,
            batch_size=batch_size,
            poll_timeout=poll_timeout,
        )
        self.state = EngineState.STOPPED
        self._started = False
        self._accepting = False
        self._lifecycle_lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self.state == EngineState.RUNNING:
                return
            if self.state in (EngineState.STARTING, EngineState.QUIESCING):
                raise RuntimeError(f"PersistenceEngine is {self.state.value}, not STOPPED")
            self.state = EngineState.STARTING
            try:
                await self.store.initialize()
                await self.worker.start()
                self._started = True
                self._accepting = True
                self.state = EngineState.RUNNING
            except Exception:
                self.state = EngineState.FAILED
                raise

    async def stop(self, timeout: Optional[float] = 5.0) -> None:
        async with self._lifecycle_lock:
            if self.state == EngineState.STOPPED:
                return
            if self.state == EngineState.QUIESCING:
                pass
            else:
                self.state = EngineState.QUIESCING
                self._accepting = False

        # Lifecycle lock is released during worker drain so callbacks can run without deadlock
        if self._started:
            await self.worker.stop(timeout)

        async with self._lifecycle_lock:
            try:
                await self.store.close()
            finally:
                self._started = False
                self.state = EngineState.STOPPED

    def _require_started(self) -> None:
        if self.state != EngineState.RUNNING:
            raise RuntimeError(f"PersistenceEngine is {self.state.value}, not RUNNING")

    async def register_consumer(
        self,
        name: str,
        consumer: Callable[[ActionEventEnvelope], Awaitable[Union[None, ConsumerResult]]],
        *,
        replay: bool = False,
    ) -> None:
        """Attach an idempotent analytics consumer; never a business-effect tool."""
        if name == "live-feed":
            raise ValueError("Reserved consumer name")
        if not callable(consumer):
            raise ValueError("Consumer must be an async callable")
        self._require_started()
        async with self._lifecycle_lock:
            self._require_started()
            if name in self.worker.consumers:
                raise ValueError("Consumer is already attached")
            await self.store.register_consumer(name, replay=replay)
            self.worker.consumers[name] = consumer

    async def pause_consumer(self, name: str) -> None:
        """Pause scheduling deliveries for a registered consumer."""
        self._require_started()
        await self.store.pause_consumer(name)

    async def resume_consumer(self, name: str) -> None:
        """Resume scheduling deliveries for a paused consumer."""
        self._require_started()
        await self.store.resume_consumer(name)

    async def retire_consumer(self, name: str, intervention_id: str) -> None:
        """Retire a consumer, moving pending outbox jobs to DLQ."""
        if not intervention_id:
            raise ValueError("intervention_id is required")
        self._require_started()
        await self.store.retire_consumer(name, intervention_id)
        self.worker.consumers.pop(name, None)

    async def __aenter__(self) -> PersistenceEngine:
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.stop()

    async def emit_action(
        self,
        event: Optional[ActionEventEnvelope] = None,
        *,
        trace_id: Optional[str] = None,
        session_id: Optional[str] = None,
        case_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        action_type: Union[ActionType, str] = ActionType.TOOL_CALL,
        status: Union[ActionStatus, str] = ActionStatus.PENDING,
        action_details: Optional[Union[ActionDetails, Dict[str, Any]]] = None,
        interception_metadata: Optional[
            Union[InterceptionMetadata, Dict[str, Any]]
        ] = None,
        source: str = "gateway",
        context: Optional[AuditContext] = None,
        timeout: Optional[float] = None,
    ) -> bool:
        """Await an atomic evidence/outbox commit before returning True."""
        self._require_started()
        if event is None:
            if trace_id is None or session_id is None or agent_id is None:
                raise ValueError(
                    "trace_id, session_id, and agent_id are required when event is not provided"
                )

            if isinstance(action_details, dict):
                act_details = ActionDetails.from_dict(action_details)
            elif isinstance(action_details, ActionDetails):
                act_details = action_details
            else:
                act_details = ActionDetails(name="unknown")

            if isinstance(interception_metadata, dict):
                int_meta = InterceptionMetadata.from_dict(interception_metadata)
            elif isinstance(interception_metadata, InterceptionMetadata):
                int_meta = interception_metadata
            else:
                raise ValueError("Explicit interception_metadata is required")

            event = ActionEventEnvelope(
                trace_id=trace_id,
                session_id=session_id,
                case_id=case_id,
                agent_id=agent_id,
                action_type=action_type
                if isinstance(action_type, ActionType)
                else ActionType(str(action_type)),
                status=status
                if isinstance(status, ActionStatus)
                else ActionStatus(str(status)),
                action_details=act_details,
                interception_metadata=int_meta,
                source=source,
                context=context or AuditContext(),
            )

        snapshot = sanitize_event(event)
        if timeout is None:
            await self._lifecycle_lock.acquire()
        else:
            await asyncio.wait_for(self._lifecycle_lock.acquire(), timeout=timeout)
        try:
            self._require_started()
            await self.store.append_with_outbox([snapshot])
            return True
        finally:
            self._lifecycle_lock.release()

    def emit_action_nowait(self, event: ActionEventEnvelope) -> bool:
        """Best-effort volatile telemetry only; never use for critical intents."""
        self._require_started()
        return self.ingest_buffer.put_nowait(event)

    async def emit_alert(
        self,
        alert: Optional[AlertEvent] = None,
        *,
        severity: Union[Severity, str] = Severity.INFO,
        rule: str = "",
        agent_id: str = "",
        session_id: str = "",
        action_taken: str = "",
        case_id: Optional[str] = None,
        evidence: Optional[Dict[str, Any]] = None,
    ) -> AlertEvent:
        """Directly insert and persist a security alert."""
        self._require_started()
        if alert is None:
            alert = AlertEvent(
                severity=severity
                if isinstance(severity, Severity)
                else Severity(str(severity).upper()),
                rule=rule,
                agent_id=agent_id,
                session_id=session_id,
                action_taken=action_taken,
                case_id=case_id,
                evidence=evidence or {},
            )
        async with self._lifecycle_lock:
            self._require_started()
            alert = sanitize_alert(alert)
            await self.store.insert_alert(alert)
            return alert

    async def emit_audit_action(
        self,
        action: Optional[AuditActionRecord] = None,
        *,
        run_id: str = "",
        session_id: str = "",
        agent_id: str = "",
        tool_name: str = "",
        target_id: Optional[str] = None,
        side_effect_class: str = "read",
        status: str = "EXECUTED",
        details: Optional[Dict[str, Any]] = None,
    ) -> AuditActionRecord:
        """Directly persist an executed tool audit record."""
        self._require_started()
        if action is None:
            action = AuditActionRecord(
                run_id=run_id,
                session_id=session_id,
                agent_id=agent_id,
                tool_name=tool_name,
                target_id=target_id,
                side_effect_class=side_effect_class,
                status=status,
                details=details or {},
            )
        async with self._lifecycle_lock:
            self._require_started()
            action = sanitize_audit(action)
            await self.store.insert_audit_action(action)
            return action

    def subscribe_live_feed(
        self,
        name: str = "live_feed",
        filter_fn: Optional[Callable[[ActionEventEnvelope], bool]] = None,
        maxsize: int = 1000,
    ) -> asyncio.Queue[ActionEventEnvelope]:
        """Subscribe to real-time action events emitted through the dispatcher."""
        return self.dispatcher.subscribe(
            name=name, filter_fn=filter_fn, maxsize=maxsize
        )

    async def get_case_timeline(self, case_id: str) -> List[ActionEventEnvelope]:
        """Retrieve full chronological action timeline for a KYC case."""
        return await self.store.get_events_by_case(case_id)

    async def get_stats(self) -> Dict[str, Any]:
        """Return store statistics augmented with buffer and subscriber telemetry."""
        stats = await self.store.get_stats()
        stats["ingest_buffer_qsize"] = self.ingest_buffer.qsize()
        stats["active_subscribers"] = len(self.dispatcher.subscribers)
        stats["pending_deliveries"] = await self.store.pending_deliveries()
        stats["rejected_volatile_events"] = self.ingest_buffer.rejected_events
        stats["dropped_live_deliveries"] = self.dispatcher.dropped_deliveries
        stats["live_filter_errors"] = self.dispatcher.filter_errors
        stats["worker_error"] = self.worker.last_error
        stats["engine_state"] = self.state.value
        return stats
