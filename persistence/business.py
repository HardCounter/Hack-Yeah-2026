"""Atomic banking receipts and source-outbox replication.

Coordinates the banking transaction boundary with durable business receipts
and guarantees idempotent audit replication without replaying side-effects.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Dict

from persistence.models import generate_utc_iso_timestamp
from persistence.store import ConflictingRecordError
from persistence.writer import BoundAuditWriter


@dataclass(frozen=True)
class EffectReceipt:
    receipt_id: str
    source_event_id: str
    application_id: str
    run_id: str
    action_id: str
    command_digest: str
    client_id: str
    account_id: str
    policy_version: str
    policy_hash: str
    occurred_at: str

    def to_dict(self) -> Dict[str, str]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> EffectReceipt:
        return cls(**data)


def compute_command_digest(
    payload_or_tool: Union[Dict[str, Any], str],
    parameters: Optional[Dict[str, Any]] = None,
    policy_version: Optional[str] = None,
) -> str:
    """Compute deterministic SHA-256 digest of canonical command payload."""
    if isinstance(payload_or_tool, dict):
        payload = payload_or_tool
    else:
        payload = {
            "tool": payload_or_tool,
            "parameters": parameters or {},
            "policy_version": policy_version or "",
        }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def ensure_business_schema(con: sqlite3.Connection) -> None:
    """Ensure uniqueness constraint, receipt table, and outbox table exist in bank DB."""
    con.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_created_client_application
        ON clients(application_id) WHERE application_id IS NOT NULL
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS effect_receipts (
            receipt_id TEXT PRIMARY KEY,
            source_event_id TEXT NOT NULL UNIQUE,
            application_id TEXT NOT NULL UNIQUE,
            run_id TEXT NOT NULL,
            action_id TEXT NOT NULL,
            command_digest TEXT NOT NULL,
            client_id TEXT NOT NULL UNIQUE REFERENCES clients(client_id),
            account_id TEXT NOT NULL UNIQUE REFERENCES accounts(account_id),
            policy_version TEXT NOT NULL,
            policy_hash TEXT NOT NULL,
            occurred_at TEXT NOT NULL
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS business_audit_outbox (
            source_event_id TEXT PRIMARY KEY REFERENCES effect_receipts(source_event_id),
            notice_json TEXT NOT NULL,
            acknowledged_at TEXT
        )
        """
    )


def record_effect(con: sqlite3.Connection, receipt: EffectReceipt) -> None:
    """Record an effect receipt and business outbox notice inside the active banking transaction.

    Never starts or commits the transaction; the caller owns the transaction boundary.
    """
    ensure_business_schema(con)

    # Check for existing application receipt
    row = con.execute(
        "SELECT command_digest, receipt_id, client_id, account_id FROM effect_receipts WHERE application_id = ?",
        (receipt.application_id,),
    ).fetchone()
    if row is not None:
        if row[0] != receipt.command_digest:
            raise ConflictingRecordError(
                f"Application '{receipt.application_id}' already booked with different command"
            )
        return

    con.execute(
        """
        INSERT INTO effect_receipts (
            receipt_id, source_event_id, application_id, run_id, action_id,
            command_digest, client_id, account_id, policy_version, policy_hash, occurred_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            receipt.receipt_id,
            receipt.source_event_id,
            receipt.application_id,
            receipt.run_id,
            receipt.action_id,
            receipt.command_digest,
            receipt.client_id,
            receipt.account_id,
            receipt.policy_version,
            receipt.policy_hash,
            receipt.occurred_at,
        ),
    )

    notice_payload = {
        "receipt_id": receipt.receipt_id,
        "source_event_id": receipt.source_event_id,
        "application_id": receipt.application_id,
        "run_id": receipt.run_id,
        "action_id": receipt.action_id,
        "client_id": receipt.client_id,
        "account_id": receipt.account_id,
        "policy_version": receipt.policy_version,
        "occurred_at": receipt.occurred_at,
    }

    con.execute(
        """
        INSERT INTO business_audit_outbox (source_event_id, notice_json, acknowledged_at)
        VALUES (?, ?, NULL)
        """,
        (receipt.source_event_id, json.dumps(notice_payload, sort_keys=True)),
    )


async def replicate_effects(bank_path: Path, writer: BoundAuditWriter, limit: int = 50) -> int:
    """Replicate unacknowledged banking notices into the audit store idempotently."""
    con = sqlite3.connect(bank_path)
    try:
        con.row_factory = sqlite3.Row
        ensure_business_schema(con)
        rows = con.execute(
            """
            SELECT source_event_id, notice_json
            FROM business_audit_outbox
            WHERE acknowledged_at IS NULL
            ORDER BY rowid ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

        if not rows:
            return 0

        replicated = 0
        for row in rows:
            notice = json.loads(row["notice_json"])
            # Append effect to audit store
            await writer.append_effect(
                run_id=notice["run_id"],
                event_id=notice["source_event_id"],
                action_id=notice["action_id"],
                receipt_id=notice["receipt_id"],
                application_id=notice["application_id"],
                client_id=notice["client_id"],
            )
            # Acknowledge in bank DB after audit commit
            now_ts = generate_utc_iso_timestamp()
            with con:
                con.execute(
                    "UPDATE business_audit_outbox SET acknowledged_at = ? WHERE source_event_id = ?",
                    (now_ts, row["source_event_id"]),
                )
            replicated += 1

        return replicated
    finally:
        con.close()
