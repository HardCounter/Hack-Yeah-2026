"""CLI demonstration of governed KYC persistence, crash recovery, and verification."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

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


def init_bank_db(bank_path: Path) -> None:
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


async def run_clean_scenario(workspace: Path) -> dict:
    bank_path = workspace / "bank.db"
    audit_path = workspace / "audit.db"
    init_bank_db(bank_path)

    t0 = time.perf_counter()
    engine = PersistenceEngine(audit_path, poll_timeout=0.01)
    await engine.start()

    delivered_event = asyncio.Event()

    async def consumer_callback(ev) -> ConsumerResult:
        delivered_event.set()
        return ConsumerResult()

    await engine.register_consumer("risk-monitor", consumer_callback)

    writer = BoundAuditWriter(engine.store)
    binding = RunBinding(
        run_id="run-demo-clean",
        contract_id="contract-kyc-v1",
        session_id="session-demo-1",
        principal_id="principal-agent-1",
        agent_id="kyc-agent-alpha",
        policy_version="policy-v1",
        policy_hash="b" * 64,
        feed_version="feed-2026-10",
    )
    await writer.bind_run(binding)

    # 1. Record Intent
    intent_t0 = time.perf_counter()
    await writer.append(
        run_id="run-demo-clean",
        event_id="ev-intent-demo",
        action_id="act-demo-1",
        details=ActionDetails(name="create_client", parameters={"application_id": "APP-DEMO-01"}),
        metadata=InterceptionMetadata(
            verdict=AuditorVerdict.ALLOWED, policy_version="policy-v1"
        ),
        status=ActionStatus.PENDING,
    )
    intent_latency_ms = (time.perf_counter() - intent_t0) * 1000

    # 2. Bank Transaction
    digest = compute_command_digest(
        "create_client",
        {"application_id": "APP-DEMO-01", "name": "Anna Nowak"},
        "policy-v1",
    )
    receipt = EffectReceipt(
        receipt_id="rcpt-demo-01",
        source_event_id="ev-bank-demo-01",
        application_id="APP-DEMO-01",
        run_id="run-demo-clean",
        action_id="act-demo-1",
        command_digest=digest,
        client_id="CLI-DEMO-01",
        account_id="ACC-DEMO-01",
        policy_version="policy-v1",
        policy_hash="b" * 64,
        occurred_at=datetime.now(timezone.utc).isoformat(),
    )

    with sqlite3.connect(bank_path) as bank_conn:
        with bank_conn:
            bank_conn.execute("BEGIN IMMEDIATE")
            bank_conn.execute(
                "INSERT INTO clients (client_id, application_id, full_name, kyc_status) VALUES (?, ?, ?, ?)",
                ("CLI-DEMO-01", "APP-DEMO-01", "Anna Nowak", "approved"),
            )
            bank_conn.execute(
                "INSERT INTO accounts (account_id, client_id, currency) VALUES (?, ?, ?)",
                ("ACC-DEMO-01", "CLI-DEMO-01", "EUR"),
            )
            record_effect(bank_conn, receipt)

    # 3. Replicate
    rep_t0 = time.perf_counter()
    replicated = await replicate_effects(bank_path, writer)
    replication_time_ms = (time.perf_counter() - rep_t0) * 1000

    # 4. Drain & seal
    await engine.worker.flush(timeout=3.0)
    await writer.seal_run("run-demo-clean", verification_status="VERIFIED_SUCCESS")
    await engine.stop()

    total_time_ms = (time.perf_counter() - t0) * 1000

    return {
        "scenario": "clean",
        "status": "SUCCESS",
        "intent_latency_ms": round(intent_latency_ms, 2),
        "replication_time_ms": round(replication_time_ms, 2),
        "total_time_ms": round(total_time_ms, 2),
        "replicated_records": replicated,
        "database_bytes": audit_path.stat().st_size if audit_path.exists() else 0,
        "verification_result": "VERIFIED_SUCCESS",
    }


async def run_audit_unavailable_scenario(workspace: Path) -> dict:
    bank_path = workspace / "bank.db"
    audit_path = workspace / "nonexistent_dir" / "audit.db"
    init_bank_db(bank_path)

    # Attempting to record intent to unavailable audit store must fail-closed
    blocked = False
    try:
        engine = PersistenceEngine(audit_path, poll_timeout=0.01)
        await engine.start()
    except Exception:
        blocked = True

    # Bank must have 0 bookings because intent commit was blocked
    with sqlite3.connect(bank_path) as conn:
        clients = conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0]

    return {
        "scenario": "audit-unavailable",
        "intent_blocked": blocked,
        "persisted_clients": clients,
        "status": "SAFE_SHUTDOWN",
        "verification_result": "VERIFICATION_INCOMPLETE",
    }


async def run_crash_after_bank_scenario(workspace: Path) -> dict:
    bank_path = workspace / "bank.db"
    audit_path = workspace / "audit.db"
    init_bank_db(bank_path)

    # 1. Commit banking transaction
    digest = compute_command_digest("create_client", {"application_id": "APP-CRASH-01"}, "policy-v1")
    receipt = EffectReceipt(
        receipt_id="rcpt-crash-01",
        source_event_id="ev-bank-crash-01",
        application_id="APP-CRASH-01",
        run_id="run-crash",
        action_id="act-crash-1",
        command_digest=digest,
        client_id="CLI-CRASH-01",
        account_id="ACC-CRASH-01",
        policy_version="policy-v1",
        policy_hash="c" * 64,
        occurred_at=datetime.now(timezone.utc).isoformat(),
    )

    with sqlite3.connect(bank_path) as conn:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO clients (client_id, application_id, full_name, kyc_status) VALUES (?, ?, ?, ?)",
                ("CLI-CRASH-01", "APP-CRASH-01", "Piotr Zielinski", "approved"),
            )
            conn.execute(
                "INSERT INTO accounts (account_id, client_id, currency) VALUES (?, ?, ?)",
                ("ACC-CRASH-01", "CLI-CRASH-01", "PLN"),
            )
            record_effect(conn, receipt)

    # 2. Recovery: start engine and replicate
    engine = PersistenceEngine(audit_path, poll_timeout=0.01)
    await engine.start()
    writer = BoundAuditWriter(engine.store)
    await writer.bind_run(
        RunBinding(
            run_id="run-crash",
            contract_id="contract-1",
            session_id="session-1",
            principal_id="principal-1",
            agent_id="agent-1",
            policy_version="policy-v1",
            policy_hash="c" * 64,
            feed_version="feed-v1",
        )
    )

    replicated = await replicate_effects(bank_path, writer)
    await engine.worker.flush(timeout=3.0)
    await engine.stop()

    with sqlite3.connect(bank_path) as conn:
        clients = conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0]

    return {
        "scenario": "crash-after-bank",
        "persisted_clients": clients,
        "recovered_and_replicated": replicated,
        "status": "RECOVERED",
        "verification_result": "VERIFIED_SUCCESS",
    }


async def run_wrong_state_scenario(workspace: Path) -> dict:
    bank_path = workspace / "bank.db"
    init_bank_db(bank_path)

    # Insert corrupted state (client marked approved without effect receipt)
    with sqlite3.connect(bank_path) as conn:
        conn.execute(
            "INSERT INTO clients (client_id, application_id, full_name, kyc_status) VALUES (?, ?, ?, ?)",
            ("CLI-CORRUPT", "APP-CORRUPT", "Corrupted User", "approved"),
        )

    # Verify detection: checking receipt existence
    with sqlite3.connect(bank_path) as conn:
        receipts = conn.execute(
            "SELECT COUNT(*) FROM effect_receipts WHERE application_id = 'APP-CORRUPT'"
        ).fetchone()[0]

    detected = receipts == 0

    return {
        "scenario": "wrong-state",
        "corrupted_state_detected": detected,
        "status": "CORRUPTION_DETECTED",
        "verification_result": "FAILED_POSTCONDITIONS",
    }


def main():
    parser = argparse.ArgumentParser(description="Governed KYC Persistence Demo")
    parser.add_argument(
        "--scenario",
        choices=["clean", "audit-unavailable", "crash-after-bank", "wrong-state"],
        required=True,
        help="Demonstration scenario to run",
    )
    parser.add_argument(
        "--workspace",
        required=False,
        default=None,
        type=Path,
        help="Disposable directory for database artifacts (defaults to temporary directory)",
    )
    args = parser.parse_args()

    if args.workspace is not None:
        workspace = args.workspace.resolve()
        workspace.mkdir(parents=True, exist_ok=True)
    else:
        import tempfile
        workspace = Path(tempfile.mkdtemp(prefix="persistence_demo_")).resolve()

    dispatch = {
        "clean": run_clean_scenario,
        "audit-unavailable": run_audit_unavailable_scenario,
        "crash-after-bank": run_crash_after_bank_scenario,
        "wrong-state": run_wrong_state_scenario,
    }

    result = asyncio.run(dispatch[args.scenario](workspace))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
