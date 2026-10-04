"""Durable Layer 2 adapters for the consume plane.

These adapters consume the same governed persistence store used by the runtime;
they do not create volatile callbacks or a second queue.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Iterable, Sequence

from contracts import AgentAction, ContentRef, DecodeError, TaskContract, VerificationResult
from contracts.action import ActionKind
from persistence.adapters import to_agent_action
from persistence.models import ActionEventEnvelope
from persistence.vocabulary import is_intent

from ..model.outputs import Finding, PluginDecision
from ..ports.event_source import Delivery
from ..ports.trajectory import TrajectoryReader


class PersistenceEventSource:
    """EventSource backed by the durable outbox leases in EventStore.

    `governed` is the trusted GovernedPersistence facade. Its store owns consumer
    registration, leases, retry timing, and DLQ transitions.
    """

    def __init__(self, governed: Any, *, consumer_name: str = "consume-plane",
                 replay: bool = False, lease_seconds: float = 30.0,
                 max_retries: int = 3, retry_backoff_s: float = 0.05):
        if not consumer_name or consumer_name == "live-feed":
            raise ValueError("consumer_name must be a non-reserved name")
        self.governed = governed
        self.store = getattr(governed, "store", governed)
        self.consumer_name = consumer_name
        self.replay = replay
        self.lease_seconds = lease_seconds
        self.max_retries = max_retries
        self.retry_backoff_s = retry_backoff_s
        self._started = False
        self._pending = 0
        self._leases: dict[str, tuple[ActionEventEnvelope, str]] = {}
        self.decode_errors: list[str] = []

    async def start(self) -> None:
        if not self._started:
            await self.store.register_consumer(self.consumer_name, replay=self.replay)
            self._started = True
            self._pending = await self.store.pending_deliveries([self.consumer_name])

    async def receive(self, max_items: int, timeout_s: float) -> Sequence[Delivery]:
        if max_items < 1 or timeout_s < 0:
            raise ValueError("max_items must be positive and timeout_s non-negative")
        await self.start()
        deadline = time.monotonic() + timeout_s
        deliveries: list[Delivery] = []
        while len(deliveries) < max_items:
            claim = await self.store.claim_delivery(
                [self.consumer_name], lease_seconds=self.lease_seconds,
                max_retries=self.max_retries,
            )
            if claim is None:
                self._pending = await self.store.pending_deliveries([self.consumer_name])
                if deliveries or time.monotonic() >= deadline:
                    break
                await asyncio.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
                continue
            event, name, attempts, lease_id = claim
            # One conversion for every consumer: storage -> Event Envelope v2.1 -> AgentAction.
            try:
                action = to_agent_action(event)
            except (DecodeError, TypeError, ValueError) as exc:
                self.decode_errors.append(f"{event.event_id}: {type(exc).__name__}")
                await self.store.finish_delivery(
                    event, name, lease_id, failed=True, max_retries=self.max_retries,
                    delay=self.retry_backoff_s * (2 ** max(0, attempts)),
                )
                self._pending = await self.store.pending_deliveries([self.consumer_name])
                continue
            delivery_id = f"{event.event_id}:{lease_id}"
            self._leases[delivery_id] = (event, lease_id)
            deliveries.append(Delivery(delivery_id, action, attempts + 1))
            self._pending = await self.store.pending_deliveries([self.consumer_name])
            if time.monotonic() >= deadline:
                break
        return deliveries

    async def ack(self, delivery_id: str) -> None:
        event, lease_id = self._take_lease(delivery_id)
        await self.store.finish_delivery(event, self.consumer_name, lease_id, failed=False,
                                         max_retries=self.max_retries, delay=self.retry_backoff_s)
        self._pending = await self.store.pending_deliveries([self.consumer_name])

    async def nack(self, delivery_id: str, *, reason: str,
                   retry_after_s: float | None = None) -> None:
        del reason  # persistence uses a fixed, sanitized DLQ reason code
        event, lease_id = self._take_lease(delivery_id)
        delay = self.retry_backoff_s if retry_after_s is None else max(0.0, retry_after_s)
        await self.store.finish_delivery(event, self.consumer_name, lease_id, failed=True,
                                         max_retries=self.max_retries, delay=delay)
        self._pending = await self.store.pending_deliveries([self.consumer_name])

    def _take_lease(self, delivery_id: str) -> tuple[ActionEventEnvelope, str]:
        try:
            return self._leases.pop(delivery_id)
        except KeyError:
            raise ValueError("unknown or already settled persistence delivery") from None

    def is_idle(self) -> bool:
        # Synchronous by design for ConsumerManager's finite-source replay path.
        return self._pending == 0 and not self._leases


class PersistenceTrajectoryReader:
    """Read-only, per-session history and contract adapter over governed storage."""

    def __init__(self, governed: Any):
        self.governed = governed
        self.store = getattr(governed, "store", governed)

    async def get(self, event_id: str) -> AgentAction | None:
        event = await self.store.get_event(event_id)
        if event is None or is_intent(event):
            return None
        return to_agent_action(event)

    async def session(self, session_id: str, *, up_to_seq: int | None = None,
                      kinds: Iterable[ActionKind] | None = None,
                      limit: int | None = None) -> Sequence[AgentAction]:
        events = await self.store.get_events_by_session(session_id)
        actions = [to_agent_action(event) for event in events
                   if not is_intent(event) and event.seq is not None]
        if up_to_seq is not None:
            actions = [a for a in actions if a.seq <= up_to_seq]
        if kinds is not None:
            wanted = frozenset(kinds)
            actions = [a for a in actions if a.kind in wanted]
        actions.sort(key=lambda a: a.seq)
        if limit is not None:
            if limit < 0:
                raise ValueError("limit must be non-negative")
            actions = actions[-limit:] if limit else []
        return actions

    async def contract(self, session_id: str) -> TaskContract | None:
        raw = await self.governed.contract(session_id)
        if raw is None or isinstance(raw, TaskContract):
            return raw
        if hasattr(raw, "to_dict"):
            raw = raw.to_dict()
        return TaskContract.from_dict(raw)

    async def content(self, ref: ContentRef) -> bytes:
        if hasattr(self.governed, "content"):
            return await self.governed.content(ref)
        return await self.store.get_content(ref.ref, sha256=ref.sha256)


class PersistenceFindingSink:
    """Durable idempotent finding and outcome-result sink.

    The store performs the final allowlist/sanitization and enforces uniqueness
    by finding_id. All writes are awaited before the manager marks a plugin done.
    """

    def __init__(self, governed: Any):
        self.governed = governed
        self.store = getattr(governed, "store", governed)

    async def write(self, findings: Sequence[Finding]) -> None:
        for finding in findings:
            payload = finding.to_dict()
            await self.store.write_consumer_finding(finding.finding_id, payload)

    async def write_decisions(self, decisions: Sequence[PluginDecision]) -> None:
        """Control-plane decision trace; the store projects it and ignores replays of the same ID."""
        for decision in decisions:
            await self.store.write_plugin_decision(decision.to_dict())

    async def decisions(self, session_id: str) -> list[dict[str, Any]]:
        return await self.store.list_plugin_decisions(session_id)

    async def write_verification(self, session_id: str, result: VerificationResult) -> None:
        await self.store.write_verification(session_id, result.to_dict())

    async def get_verification(self, session_id: str) -> VerificationResult | None:
        raw = await self.store.get_verification(session_id)
        if raw is None:
            return None
        if isinstance(raw, VerificationResult):
            return raw
        from contracts import VerificationCheck
        checks = tuple(VerificationCheck(
            id=check["id"], status=check["status"], detail=check.get("detail", "STORED_RESULT"))
            for check in raw.get("checks", ()))
        return VerificationResult(raw["verification_status"], checks)

    async def findings(self, session_id: str) -> list[dict[str, Any]]:
        """Return the persistence layer's sanitized finding projection."""
        return await self.store.list_consumer_findings(session_id)
