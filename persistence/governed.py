"""Trusted orchestration facade for immutable contracts and governed evidence."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
import shutil
from typing import Any, Iterable, Mapping

from contracts import PolicyAdjustmentSignal, TaskContract

from persistence.adapters.consumer_v21 import consumer_kind, to_consumer_v21
from persistence.models import (
    ActionEventEnvelope, AuditContext, RunBinding,
)
from persistence.privacy import sanitize_event, token
from persistence.store import AuditBackpressureError, ConflictingRecordError, EventStore


def _contract_dict(contract: TaskContract | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(contract, Mapping):
        return TaskContract.from_dict(contract).to_dict()
    if not isinstance(contract, TaskContract):
        raise TypeError("contract must be the canonical contracts.TaskContract")
    return contract.to_dict()


class GovernedPersistence:
    """Persistence boundary that binds Layer 1 events to trusted contract state.

    It does not authenticate its caller. The gateway/orchestrator must keep this
    object private and pass only its already-authenticated contract and action.
    """

    def __init__(self, store: EventStore):
        self.store = store

    async def persist_contract(self, contract: TaskContract | Mapping[str, Any]) -> None:
        data = _contract_dict(contract)
        required = ("contract_id", "run_id", "session_id", "principal_id", "agent_id",
                    "policy_version", "policy_hash", "feed_version")
        if any(not isinstance(data.get(k), str) or not data[k].strip() for k in required):
            raise ValueError("TaskContract must include all trusted identity and policy bindings")
        session_id, run_id = data["session_id"], data["run_id"]
        raw = json.dumps(data, sort_keys=True, separators=(",", ":"))

        async with self.store._lock:
            def atomic_insert_and_bind() -> None:
                conn = self.store._get_connection()
                with conn:
                    conn.execute("BEGIN IMMEDIATE")
                    row = conn.execute(
                        "SELECT contract_id,run_id,contract_json FROM task_contracts WHERE session_id=?",
                        (session_id,),
                    ).fetchone()
                    if row and tuple(row) != (data["contract_id"], run_id, raw):
                        raise ConflictingRecordError("Session already has a different immutable TaskContract")
                    binding = RunBinding(
                        run_id=run_id, contract_id=data["contract_id"], session_id=session_id,
                        principal_id=data["principal_id"], agent_id=data["agent_id"],
                        policy_version=data["policy_version"], policy_hash=data["policy_hash"],
                        feed_version=data["feed_version"],
                    )
                    stored_run = conn.execute(
                        "SELECT binding_json FROM audit_runs WHERE run_id=?", (run_id,)
                    ).fetchone()
                    binding_json = json.dumps(binding.to_dict(), sort_keys=True)
                    if stored_run and stored_run[0] != binding_json:
                        raise ConflictingRecordError("Run already has a conflicting trusted binding")
                    if not row:
                        conn.execute(
                            "INSERT INTO task_contracts(session_id,run_id,contract_id,contract_json) VALUES(?,?,?,?)",
                            (session_id, run_id, data["contract_id"], raw),
                        )
                    if not stored_run:
                        conn.execute(
                            "INSERT INTO audit_runs(run_id,binding_json,next_index,lifecycle) VALUES(?,?,0,'ACTIVE')",
                            (run_id, binding_json),
                        )
            await self.store._offload(atomic_insert_and_bind)

    async def bind_contract(self, contract: TaskContract | Mapping[str, Any]) -> None:
        """Compatibility spelling used by the trusted runtime at session start."""
        await self.persist_contract(contract)

    async def contract(self, session_id: str) -> TaskContract | None:
        token(session_id, required=True)
        async with self.store._lock:
            def read():
                row = self.store._get_connection().execute(
                    "SELECT contract_json FROM task_contracts WHERE session_id=?", (session_id,)
                ).fetchone()
                return json.loads(row[0]) if row else None
            data = await self.store._offload(read)
        return TaskContract.from_dict(data) if data else None

    async def seal_run(self, run_id: str, verification_status: str | None = None) -> None:
        await self.store.seal_run(run_id, verification_status)

    async def _append_bound(self, candidate: ActionEventEnvelope, deliver: bool) -> ActionEventEnvelope:
        candidate = sanitize_event(candidate, allow_unindexed_run=True)
        if not candidate.context.run_id or not candidate.context.action_id:
            raise ValueError("Governed evidence requires trusted run_id and action_id")
        if candidate.seq is not None or candidate.context.action_index is not None:
            raise ValueError("Layer 2 assigns seq and run action_index")

        def commit() -> None:
            conn = self.store._get_connection()
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                run = conn.execute(
                    "SELECT binding_json,next_index,lifecycle FROM audit_runs WHERE run_id=?",
                    (candidate.context.run_id,),
                ).fetchone()
                if run is None or run[2] != "ACTIVE":
                    raise ValueError("Unknown or inactive governed run")
                binding = json.loads(run[0])
                contract_row = conn.execute(
                    "SELECT contract_json FROM task_contracts WHERE session_id=? AND run_id=?",
                    (binding["session_id"], candidate.context.run_id),
                ).fetchone()
                if contract_row is None:
                    raise ValueError("No persisted TaskContract is bound to this run")
                contract = json.loads(contract_row[0])
                if candidate.session_id != binding["session_id"] or candidate.agent_id != binding["agent_id"]:
                    raise ValueError("Candidate identity conflicts with trusted run binding")
                if candidate.case_id not in (None, contract.get("case_id")):
                    raise ValueError("Candidate case_id conflicts with trusted TaskContract")
                for key, expected in (("contract_id", binding["contract_id"]),
                                      ("principal_id", binding["principal_id"]),
                                      ("policy_hash", binding["policy_hash"]),
                                      ("feed_version", binding["feed_version"])):
                    supplied = getattr(candidate.context, key)
                    if supplied not in (None, expected):
                        raise ValueError(f"Candidate {key} conflicts with trusted run binding")
                policy_version = candidate.interception_metadata.policy_version
                if policy_version not in (None, binding["policy_version"]):
                    raise ValueError("Candidate policy_version conflicts with pinned run policy")
                metadata = replace(candidate.interception_metadata, policy_version=binding["policy_version"])
                existing = conn.execute(
                    "SELECT payload_json FROM events WHERE event_id=?", (candidate.event_id,)
                ).fetchone()
                action_index = run[1]
                seq_for_retry = None
                if existing:
                    old = ActionEventEnvelope.from_json(existing[0])
                    if old.context.action_id != candidate.context.action_id:
                        raise ConflictingRecordError("Event ID was reused for another action")
                    action_index = old.context.action_index
                    seq_for_retry = old.seq
                context = replace(
                    candidate.context,
                    contract_id=binding["contract_id"],
                    principal_id=binding["principal_id"],
                    policy_hash=binding["policy_hash"],
                    feed_version=binding["feed_version"],
                    action_index=action_index,
                )
                trusted = replace(
                    candidate,
                    case_id=contract.get("case_id"),
                    context=context,
                    interception_metadata=metadata,
                    seq=seq_for_retry,
                )
                # Validate the complete consumer boundary before allocating any
                # persistent order or exposing the event to a consumer.
                if deliver:
                    to_consumer_v21(replace(trusted, seq=0))
                elif trusted.seq is not None:
                    trusted = replace(trusted, seq=None)
                row = self.store._event_row(trusted)
                if existing:
                    old = ActionEventEnvelope.from_json(existing[0])
                    comparable = sanitize_event(replace(trusted, seq=old.seq))
                    if comparable.to_dict() != old.to_dict():
                        raise ConflictingRecordError("Conflicting immutable event retry")
                    return
                conn.execute("UPDATE audit_runs SET next_index=next_index+1 WHERE run_id=?", (candidate.context.run_id,))
                encoded_size = len(row[-1].encode("utf-8"))
                used_size = conn.execute("SELECT COALESCE(SUM(LENGTH(payload_json)),0) FROM events").fetchone()[0]
                if used_size + encoded_size > self.store.settings.max_retained_logical_bytes:
                    raise AuditBackpressureError("Logical payload quota exhausted")
                if self.store.db_path != ":memory:":
                    db_dir = os.path.dirname(os.path.abspath(self.store.db_path))
                    try:
                        if shutil.disk_usage(db_dir).free < self.store.settings.min_free_bytes:
                            raise AuditBackpressureError("Insufficient disk free space")
                    except OSError:
                        pass
                inserted = self.store._insert_event_row(row, assign_seq=deliver)
                if not inserted:
                    return
                if deliver:
                    consumers = [r[0] for r in conn.execute("SELECT name FROM consumers")]
                    pending = conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
                    if pending + len(consumers) > self.store.outbox_maxsize:
                        raise AuditBackpressureError("Durable audit outbox capacity exhausted")
                    conn.executemany(
                        "INSERT INTO outbox(event_id,consumer_name) VALUES(?,?)",
                        [(trusted.event_id, name) for name in consumers],
                    )

        async with self.store._lock:
            await self.store._offload(commit)
        stored = await self.store.get_event(candidate.event_id)
        assert stored is not None
        return stored

    async def append(self, candidate: ActionEventEnvelope) -> ActionEventEnvelope:
        """Commit final evidence and consumer jobs atomically."""
        if candidate.status.value == "PENDING":
            raise ValueError("Intent-only PENDING evidence must use intent()")
        return await self._append_bound(candidate, deliver=True)

    async def intent(self, candidate: ActionEventEnvelope) -> ActionEventEnvelope:
        """Durably commit a not-yet-executed intent without consumer delivery."""
        if candidate.status.value != "PENDING":
            raise ValueError("Intent evidence must have PENDING status")
        return await self._append_bound(candidate, deliver=False)

    async def wire_event(self, event_id: str) -> dict[str, Any]:
        event = await self.store.get_event(event_id)
        if event is None:
            raise KeyError(event_id)
        return to_consumer_v21(event)

    async def wire_session(self, session_id: str) -> list[dict[str, Any]]:
        events = await self.store.get_events_by_session_seq(session_id)
        return [to_consumer_v21(event) for event in events if event.status.value != "PENDING"]

    async def events(self, session_id: str, up_to_seq: int | None = None,
                     kinds: Iterable[str] | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        rows = await self.store.get_events_by_session_seq(session_id)
        wire = [to_consumer_v21(row) for row in rows if row.seq is not None
                and (up_to_seq is None or row.seq <= up_to_seq)
                and row.status.value != "PENDING"]
        if kinds is not None:
            allowed = set(kinds)
            wire = [row for row in wire if consumer_kind(row["action_type"]) in allowed]
        if limit is not None:
            if limit < 0:
                raise ValueError("limit must be non-negative")
            wire = wire[:limit] if limit else []
        return wire

    async def put_content(self, body: bytes, *, redacted: bool, trust: str = "untrusted") -> dict[str, Any]:
        """Store pre-redacted bytes and return metadata only; raw bodies never enter events."""
        if not isinstance(body, bytes) or not redacted or trust not in ("trusted", "untrusted"):
            raise ValueError("Content must be bytes redacted by Layer 1 before persistence")
        digest = hashlib.sha256(body).hexdigest()
        content_id = digest
        ref = f"store://agent_content/{content_id}"
        def write():
            conn = self.store._get_connection()
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                old = conn.execute("SELECT sha256,size_bytes,body FROM agent_content WHERE content_id=?", (content_id,)).fetchone()
                if old and (old[0] != digest or old[1] != len(body) or old[2] != body):
                    raise ConflictingRecordError("Content reference collision")
                conn.execute("INSERT OR IGNORE INTO agent_content VALUES(?,?,?,?,?,?)",
                             (content_id,digest,len(body),1,trust,body))
        async with self.store._lock:
            await self.store._offload(write)
        return {"ref": ref, "sha256": digest, "size_bytes": len(body), "redacted": True, "trust": trust}

    async def content(self, ref: Any) -> bytes:
        ref_value = ref.get("ref") if isinstance(ref, Mapping) else getattr(ref, "ref", None)
        expected_sha = ref.get("sha256") if isinstance(ref, Mapping) else getattr(ref, "sha256", None)
        expected_size = ref.get("size_bytes") if isinstance(ref, Mapping) else getattr(ref, "size_bytes", None)
        prefix = "store://agent_content/"
        if not isinstance(ref_value, str) or not ref_value.startswith(prefix):
            raise ValueError("Unsupported content reference")
        content_id = token(ref_value[len(prefix):], required=True)
        async with self.store._lock:
            def read():
                row = self.store._get_connection().execute(
                    "SELECT sha256,size_bytes,body FROM agent_content WHERE content_id=?", (content_id,)
                ).fetchone()
                return row if row else None
            row = await self.store._offload(read)
        if row is None:
            raise KeyError(ref_value)
        body = bytes(row[2])
        if (hashlib.sha256(body).hexdigest() != row[0] or len(body) != row[1]
                or (expected_sha is not None and expected_sha != row[0])
                or (expected_size is not None and expected_size != row[1])):
            raise ValueError("Stored content integrity check failed")
        return body

    async def persist_signal(self, signal: PolicyAdjustmentSignal) -> bool:
        """Persist deduplicated, privacy-filtered feedback provenance."""
        data = signal.to_dict()
        if len(data.get("target_scope", {})) != 1 or "session_id" not in data["target_scope"]:
            raise ValueError("Feedback must target exactly one session")
        sid = token(data["target_scope"]["session_id"], required=True)
        signal_id = token(data["signal_id"], required=True)
        action = data["action"]
        if action not in {"ALERT", "REQUIRE_APPROVAL_FOR", "BLOCK_TOOLS", "STRICT_MODE", "HALT_SESSION"}:
            raise ValueError("Unsupported feedback action")
        ttl = data["ttl_seconds"]
        if type(ttl) is not int or ttl <= 0:
            raise ValueError("Feedback ttl_seconds must be positive")
        mods = data.get("policy_modifications") or {}
        if set(mods) - {"tools", "enabled"} or ("tools" in mods and (not isinstance(mods["tools"], (list, tuple)) or any(token(x, required=True) is None for x in mods["tools"]))) or ("enabled" in mods and mods["enabled"] is not True):
            raise ValueError("Unsupported feedback modification")
        # Free-text reason is deliberately not persisted.
        safe = {"signal_id": signal_id, "ts": data["ts"], "session_id": sid,
                "action": action, "policy_modifications": {"tools": list(mods.get("tools", ())),
                    **({"enabled": True} if mods.get("enabled") is True else {})},
                "ttl_seconds": ttl, "source_plugin": token(data["source_plugin"], required=True),
                "trigger_event_id": token(data["trigger_event_id"], required=True)}
        raw = json.dumps(safe, sort_keys=True, separators=(",", ":"))
        def write():
            conn = self.store._get_connection()
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                if not conn.execute("SELECT 1 FROM task_contracts WHERE session_id=?", (sid,)).fetchone():
                    raise ValueError("Feedback targets unknown session")
                old = conn.execute("SELECT session_id,signal_json,applied FROM policy_signals WHERE signal_id=?", (signal_id,)).fetchone()
                if old:
                    if (old[0], old[1]) != (sid, raw):
                        raise ConflictingRecordError("Conflicting feedback signal ID")
                    return not bool(old[2])
                conn.execute("INSERT INTO policy_signals(signal_id,session_id,signal_json,applied) VALUES(?,?,?,0)", (signal_id,sid,raw))
                return True
        async with self.store._lock:
            return await self.store._offload(write)

    async def mark_signal_applied(self, signal_id: str) -> None:
        """Mark feedback fully applied only after its control event is durable."""
        signal_id = token(signal_id, required=True)
        async with self.store._lock:
            def update():
                conn = self.store._get_connection()
                with conn:
                    changed = conn.execute(
                        "UPDATE policy_signals SET applied=1 WHERE signal_id=?", (signal_id,)
                    ).rowcount
                    if not changed:
                        raise KeyError(signal_id)
            await self.store._offload(update)
