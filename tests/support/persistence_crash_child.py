"""Subprocess crash child for deterministic boundary crash injection tests.

Accepts only a disposable workspace directory and an enumerated crash point.
Exits with code 70 at the designated point.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys

# Ensure repository root is on sys.path for direct subprocess invocation
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from persistence.business import (
    EffectReceipt,
    compute_command_digest,
    ensure_business_schema,
    record_effect,
    replicate_effects,
)
from persistence.models import (
    ActionDetails,
    ActionStatus,
    AuditorVerdict,
    ConsumerResult,
    InterceptionMetadata,
    RunBinding,
)
from persistence.store import EventStore
from persistence.worker import PersistenceEngine
from persistence.writer import BoundAuditWriter

ALLOWED_POINTS = {
    "before_bank_commit",
    "after_bank_commit",
    "after_audit_import",
    "after_consumer_result",
    "none",
}


def crash_if(selected: str, reached: str) -> None:
    if selected == reached:
        os._exit(70)


def setup_bank_db(bank_path: Path) -> None:
    with sqlite3.connect(bank_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS clients (
                client_id TEXT PRIMARY KEY,
                application_id TEXT UNIQUE,
                full_name TEXT NOT NULL,
                kyc_status TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                account_id TEXT PRIMARY KEY,
                client_id TEXT NOT NULL REFERENCES clients(client_id),
                currency TEXT NOT NULL
            )
            """
        )
        ensure_business_schema(conn)


async def run_scenario(workspace: Path, crash_point: str) -> None:
    bank_path = workspace / "bank.db"
    audit_path = workspace / "audit.db"

    setup_bank_db(bank_path)

    # 1. Initialize Audit Engine
    engine = PersistenceEngine(audit_path, poll_timeout=0.01)
    await engine.start()

    delivered_event = asyncio.Event()

    async def consumer_callback(ev) -> ConsumerResult:
        delivered_event.set()
        return ConsumerResult()

    await engine.register_consumer("analytics-test", consumer_callback)

    writer = BoundAuditWriter(engine.store)
    binding = RunBinding(
        run_id="run-crash-1",
        contract_id="contract-1",
        session_id="session-1",
        principal_id="principal-1",
        agent_id="agent-1",
        policy_version="policy-v1",
        policy_hash="a" * 64,
        feed_version="feed-v1",
    )
    await writer.bind_run(binding)

    # 2. Record intent in audit store
    await writer.append(
        run_id="run-crash-1",
        event_id="ev-intent-1",
        action_id="act-create-1",
        details=ActionDetails(name="create_client", parameters={"application_id": "APP-0001"}),
        metadata=InterceptionMetadata(
            verdict=AuditorVerdict.ALLOWED, policy_version="policy-v1"
        ),
        status=ActionStatus.PENDING,
    )

    crash_if(crash_point, "before_bank_commit")

    # 3. Perform banking transaction with atomic receipt
    digest = compute_command_digest(
        "create_client",
        {"application_id": "APP-0001", "name": "Jan Kowalski"},
        "policy-v1",
    )
    receipt = EffectReceipt(
        receipt_id="rcpt-0001",
        source_event_id="ev-bank-0001",
        application_id="APP-0001",
        run_id="run-crash-1",
        action_id="act-create-1",
        command_digest=digest,
        client_id="CLI-0001",
        account_id="ACC-0001",
        policy_version="policy-v1",
        policy_hash="a" * 64,
        occurred_at=datetime.now(timezone.utc).isoformat(),
    )

    with sqlite3.connect(bank_path) as bank_conn:
        with bank_conn:
            bank_conn.execute("BEGIN IMMEDIATE")
            bank_conn.execute(
                "INSERT INTO clients (client_id, application_id, full_name, kyc_status) VALUES (?, ?, ?, ?)",
                ("CLI-0001", "APP-0001", "Jan Kowalski", "approved"),
            )
            bank_conn.execute(
                "INSERT INTO accounts (account_id, client_id, currency) VALUES (?, ?, ?)",
                ("ACC-0001", "CLI-0001", "PLN"),
            )
            record_effect(bank_conn, receipt)

    crash_if(crash_point, "after_bank_commit")

    # 4. Replicate effect to audit store
    replicated = await replicate_effects(bank_path, writer)
    assert replicated >= 1

    crash_if(crash_point, "after_audit_import")

    # 5. Await consumer delivery
    await engine.worker.flush(timeout=3.0)

    crash_if(crash_point, "after_consumer_result")

    await engine.stop()


def main():
    if len(sys.argv) < 3:
        sys.stderr.write("Usage: python persistence_crash_child.py <workspace_dir> <crash_point>\n")
        sys.exit(1)

    workspace = Path(sys.argv[1]).resolve()
    crash_point = sys.argv[2].strip()

    if crash_point not in ALLOWED_POINTS:
        sys.stderr.write(f"Invalid crash point: {crash_point}. Allowed: {ALLOWED_POINTS}\n")
        sys.exit(2)

    asyncio.run(run_scenario(workspace, crash_point))
    sys.exit(0)


if __name__ == "__main__":
    main()
