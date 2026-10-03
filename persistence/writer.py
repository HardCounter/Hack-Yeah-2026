"""Trusted run writer and durable dispatch evidence.

Enforces server-side authenticated run bindings, assigns contiguous atomic
action indices, and binds immutable context to event envelopes.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, Optional

from persistence.models import (
    ActionDetails,
    ActionEventEnvelope,
    ActionStatus,
    ActionType,
    AuditContext,
    AuditorVerdict,
    InterceptionMetadata,
    RunBinding,
)
from persistence.store import ConflictingRecordError, EventStore


class BoundAuditWriter:
    """Enforces authenticated run binding and contiguous event indexing for governed workflows."""

    def __init__(self, store: EventStore) -> None:
        self.store = store

    def _sync_bind_run(self, binding: RunBinding) -> None:
        conn = self.store._get_connection()
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT binding_json, lifecycle FROM audit_runs WHERE run_id = ?",
                (binding.run_id,),
            ).fetchone()
            if row is not None:
                stored = json.loads(row[0])
                if stored != binding.to_dict():
                    raise ConflictingRecordError(
                        f"Run '{binding.run_id}' already bound with conflicting binding"
                    )
                return
            conn.execute(
                "INSERT INTO audit_runs (run_id, binding_json, next_index, lifecycle) VALUES (?, ?, 0, 'ACTIVE')",
                (binding.run_id, json.dumps(binding.to_dict(), sort_keys=True)),
            )

    async def bind_run(self, binding: RunBinding) -> None:
        """Bind an active workflow run to an immutable server-authenticated contract."""
        async with self.store._lock:
            await self.store._offload(self._sync_bind_run, binding)

    def _sync_append(
        self,
        run_id: str,
        event_id: str,
        action_id: str,
        details: ActionDetails,
        metadata: InterceptionMetadata,
        status: ActionStatus,
        reason_code: Optional[str] = None,
        effect_receipt_id: Optional[str] = None,
    ) -> ActionEventEnvelope:
        conn = self.store._get_connection()
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            # 1. Verify active run binding
            run_row = conn.execute(
                "SELECT binding_json, next_index, lifecycle FROM audit_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if run_row is None:
                raise ValueError(f"Unknown or unbound run_id: '{run_id}'")

            lifecycle = run_row[2]
            if lifecycle != "ACTIVE":
                raise ValueError(f"Run '{run_id}' is not active (lifecycle={lifecycle})")

            binding_data = json.loads(run_row[0])
            binding = RunBinding.from_dict(binding_data)

            # Reject forged policy_version
            if metadata.policy_version and metadata.policy_version != binding.policy_version:
                raise ValueError(
                    f"Supplied metadata policy_version '{metadata.policy_version}' conflicts with run binding '{binding.policy_version}'"
                )

            # Ensure metadata has run's policy_version
            if not metadata.policy_version:
                metadata.policy_version = binding.policy_version

            # 2. Check if event_id already exists (idempotent retry or conflict check)
            existing_row = conn.execute(
                "SELECT payload_json FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
            if existing_row is not None:
                existing_envelope = ActionEventEnvelope.from_json(existing_row[0])
                if (
                    existing_envelope.context.action_id != action_id
                    or existing_envelope.action_details.name != details.name
                ):
                    raise ConflictingRecordError(
                        f"Event '{event_id}' already recorded with conflicting action details"
                    )
                return existing_envelope

            # 3. Atomically allocate contiguous action_index
            action_index = run_row[1]
            conn.execute(
                "UPDATE audit_runs SET next_index = next_index + 1 WHERE run_id = ? AND lifecycle = 'ACTIVE'",
                (run_id,),
            )

            # 4. Construct AuditContext and ActionEventEnvelope
            context = AuditContext(
                contract_id=binding.contract_id,
                run_id=run_id,
                action_id=action_id,
                principal_id=binding.principal_id,
                action_index=action_index,
                policy_hash=binding.policy_hash,
                feed_version=binding.feed_version,
                reason_code=reason_code if reason_code else None,
                effect_receipt_id=effect_receipt_id,
            )

            envelope = ActionEventEnvelope(
                event_id=event_id,
                trace_id=run_id,
                session_id=binding.session_id,
                agent_id=binding.agent_id,
                action_type=ActionType.TOOL_CALL,
                status=status,
                action_details=details,
                interception_metadata=metadata,
                context=context,
            )

            # 5. Persist event, outbox deliveries, and index mappings
            # Use internal event row insertion
            row = self.store._event_row(envelope)
            self.store._insert_event_row(row)

            # Register outbox jobs for all consumers
            consumers = [r[0] for r in conn.execute("SELECT name FROM consumers")]
            pending = conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
            if pending + len(consumers) > self.store.outbox_maxsize:
                from persistence.store import AuditBackpressureError
                raise AuditBackpressureError("Durable audit outbox capacity exhausted")

            conn.executemany(
                "INSERT INTO outbox(event_id, consumer_name) VALUES (?, ?)",
                [(event_id, name) for name in consumers],
            )

            return envelope

    async def append(
        self,
        run_id: str,
        event_id: str,
        action_id: str,
        details: ActionDetails,
        metadata: InterceptionMetadata,
        status: ActionStatus,
        reason_code: Optional[str] = None,
    ) -> ActionEventEnvelope:
        """Atomically allocate an action index, bind run context, and commit evidence."""
        async with self.store._lock:
            return await self.store._offload(
                self._sync_append,
                run_id,
                event_id,
                action_id,
                details,
                metadata,
                status,
                reason_code,
            )

    async def append_effect(
        self,
        run_id: str,
        event_id: str,
        action_id: str,
        receipt_id: str,
        application_id: str,
        client_id: str,
    ) -> ActionEventEnvelope:
        """Import a committed banking notice recording an executed side effect."""
        details = ActionDetails(
            name="create_client",
            parameters={"application_id": application_id},
            result={"client_id": client_id, "receipt_id": receipt_id},
        )
        metadata = InterceptionMetadata(verdict=AuditorVerdict.ALLOWED)
        async with self.store._lock:
            return await self.store._offload(
                self._sync_append,
                run_id,
                event_id,
                action_id,
                details,
                metadata,
                ActionStatus.EXECUTED,
                "EFFECT_COMMITTED",
                receipt_id,
            )
