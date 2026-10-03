"""Background persistence worker and high-level PersistenceEngine facade.

Coordinates asynchronous ingestion from IngestBuffer, batch writes to EventStore,
and reliable dispatching to downstream consumers.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

from persistence.models import (
    ActionDetails,
    ActionEventEnvelope,
    ActionStatus,
    ActionType,
    AlertEvent,
    AuditActionRecord,
    InterceptionMetadata,
    Severity,
)
from persistence.queue import ConsumerDispatcher, IngestBuffer
from persistence.store import EventStore

logger = logging.getLogger(__name__)


class PersistenceWorker:
    """Background asynchronous worker consuming from IngestBuffer and writing to EventStore."""

    def __init__(
        self,
        ingest_buffer: IngestBuffer,
        store: EventStore,
        consumer_dispatcher: Optional[ConsumerDispatcher] = None,
        batch_size: int = 50,
        poll_timeout: float = 0.05,
    ) -> None:
        self.ingest_buffer = ingest_buffer
        self.store = store
        self.consumer_dispatcher = consumer_dispatcher
        self.batch_size = batch_size
        self.poll_timeout = poll_timeout
        self._running = False
        self._task: Optional[asyncio.Task[None]] = None

    async def start(self) -> None:
        """Start the background persistence loop."""
        if not self._running:
            self._running = True
            self._task = asyncio.create_task(self._run_loop())

    async def _process_batch(self, batch: List[ActionEventEnvelope]) -> None:
        if not batch:
            return
        await self.store.insert_events_batch(batch)
        if self.consumer_dispatcher is not None:
            for event in batch:
                await self.consumer_dispatcher.dispatch(event)

    async def _run_loop(self) -> None:
        while self._running:
            try:
                batch = await self.ingest_buffer.get_batch(
                    max_items=self.batch_size,
                    timeout=self.poll_timeout,
                )
                if batch:
                    await self._process_batch(batch)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("Error in PersistenceWorker processing loop: %s", exc)
                await asyncio.sleep(self.poll_timeout)

    async def flush(self, timeout: Optional[float] = 5.0) -> None:
        """Wait until all currently queued items in IngestBuffer are processed."""
        start_time = asyncio.get_event_loop().time()
        while not self.ingest_buffer.empty():
            if timeout is not None:
                elapsed = asyncio.get_event_loop().time() - start_time
                if elapsed >= timeout:
                    logger.warning("Flush timed out after %.2f seconds", timeout)
                    break
            # Give worker opportunity to drain
            await asyncio.sleep(0.01)

    async def stop(self, timeout: Optional[float] = 5.0) -> None:
        """Signal worker to stop, gracefully drain remaining items, and terminate."""
        self._running = False

        # Drain any items remaining in buffer
        while not self.ingest_buffer.empty():
            batch = await self.ingest_buffer.get_batch(
                max_items=self.batch_size, timeout=0.01
            )
            if batch:
                await self._process_batch(batch)

        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass


class PersistenceEngine:
    """High-level facade coordinating store, ingest buffer, worker, and consumer dispatcher.

    Supports usage as an async context manager:
        async with PersistenceEngine(db_path) as engine:
            await engine.emit_action(...)
    """

    def __init__(
        self,
        db_path: Union[str, Path] = ":memory:",
        buffer_maxsize: int = 10000,
        batch_size: int = 50,
        poll_timeout: float = 0.05,
    ) -> None:
        self.db_path = str(db_path)
        self.store = EventStore(self.db_path)
        self.ingest_buffer = IngestBuffer(maxsize=buffer_maxsize)
        self.dispatcher = ConsumerDispatcher()
        self.worker = PersistenceWorker(
            ingest_buffer=self.ingest_buffer,
            store=self.store,
            consumer_dispatcher=self.dispatcher,
            batch_size=batch_size,
            poll_timeout=poll_timeout,
        )
        self._started = False

    async def start(self) -> None:
        """Initialize store and start worker."""
        if not self._started:
            await self.store.initialize()
            await self.worker.start()
            self._started = True

    async def stop(self) -> None:
        """Stop worker, flush buffers, and close store connection."""
        if self._started:
            await self.worker.stop()
            await self.store.close()
            self._started = False

    async def __aenter__(self) -> "PersistenceEngine":
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
        timeout: Optional[float] = None,
    ) -> bool:
        """Emit an action event into the ingest buffer asynchronously."""
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
                from persistence.models import AuditorVerdict
                int_meta = InterceptionMetadata(verdict=AuditorVerdict.ALLOWED)

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
            )

        return await self.ingest_buffer.put(event, timeout=timeout)

    def emit_action_nowait(self, event: ActionEventEnvelope) -> bool:
        """Non-blocking emission of an action event."""
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
        return stats
