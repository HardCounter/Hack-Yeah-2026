"""Tests for atomic banking receipts and source-outbox replication."""

import asyncio
from functools import wraps
from pathlib import Path
import sqlite3
import pytest

from persistence import (
    ConflictingRecordError,
    EventStore,
    RunBinding,
)
from persistence.business import (
    EffectReceipt,
    compute_command_digest,
    ensure_business_schema,
    record_effect,
    replicate_effects,
)
from persistence.writer import BoundAuditWriter


def async_test(fn):
    @wraps(fn)
    def run(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))
    return run


def _setup_bank_db(path: Path) -> None:
    with sqlite3.connect(path) as con:
        con.execute("PRAGMA foreign_keys=ON")
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS clients (
                client_id TEXT PRIMARY KEY,
                full_name TEXT NOT NULL,
                application_id TEXT
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                account_id TEXT PRIMARY KEY,
                client_id TEXT REFERENCES clients(client_id)
            )
            """
        )
        ensure_business_schema(con)
        con.commit()


def _sample_receipt(app_id="APP-0001", client_id="CLI-0001", account_id="ACC-0001", **kwargs):
    cmd_payload = {"name": "Alice Smith", "application_id": app_id}
    defaults = {
        "receipt_id": f"rcpt-{app_id}",
        "source_event_id": f"evt-{app_id}",
        "application_id": app_id,
        "run_id": "run-1",
        "action_id": "act-1",
        "command_digest": compute_command_digest(cmd_payload),
        "client_id": client_id,
        "account_id": account_id,
        "policy_version": "policy-v2",
        "policy_hash": "a" * 64,
        "occurred_at": "2026-10-03T12:00:00Z",
    }
    defaults.update(kwargs)
    return EffectReceipt(**defaults)


def test_exactly_one_booking_and_idempotent_retry(tmp_path):
    bank_path = tmp_path / "bank.db"
    _setup_bank_db(bank_path)

    receipt = _sample_receipt("APP-0001")

    with sqlite3.connect(bank_path) as con:
        with con:
            con.execute("INSERT INTO clients VALUES ('CLI-0001', 'Alice Smith', 'APP-0001')")
            con.execute("INSERT INTO accounts VALUES ('ACC-0001', 'CLI-0001')")
            record_effect(con, receipt)

    # Verify rows created
    with sqlite3.connect(bank_path) as con:
        assert con.execute("SELECT COUNT(*) FROM clients WHERE application_id='APP-0001'").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM effect_receipts WHERE application_id='APP-0001'").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM accounts WHERE client_id='CLI-0001'").fetchone()[0] == 1

    # Exact retry is idempotent
    with sqlite3.connect(bank_path) as con:
        with con:
            record_effect(con, receipt)

    # Conflicting command digest raises ConflictingRecordError
    conflicting_receipt = _sample_receipt("APP-0001", command_digest="different_digest_hash")
    with pytest.raises(ConflictingRecordError, match="already booked with different command"):
        with sqlite3.connect(bank_path) as con:
            with con:
                record_effect(con, conflicting_receipt)


def test_rollback_on_receipt_insert_failure(tmp_path):
    bank_path = tmp_path / "bank_err.db"
    _setup_bank_db(bank_path)

    receipt = _sample_receipt("APP-0001")
    # Pre-insert receipt with same receipt_id to force primary key collision
    with sqlite3.connect(bank_path) as con:
        con.execute(
            "INSERT INTO clients VALUES ('CLI-0000', 'Dummy', 'APP-0000')"
        )
        con.execute("INSERT INTO accounts VALUES ('ACC-0000', 'CLI-0000')")
        record_effect(con, _sample_receipt("APP-0000", receipt_id="rcpt-COLLIDE", client_id="CLI-0000", account_id="ACC-0000"))

    colliding_receipt = _sample_receipt("APP-0001", receipt_id="rcpt-COLLIDE")

    with pytest.raises(sqlite3.IntegrityError):
        with sqlite3.connect(bank_path) as con:
            with con:
                con.execute("INSERT INTO clients VALUES ('CLI-0001', 'Alice Smith', 'APP-0001')")
                con.execute("INSERT INTO accounts VALUES ('ACC-0001', 'CLI-0001')")
                record_effect(con, colliding_receipt)

    # Client was rolled back!
    with sqlite3.connect(bank_path) as con:
        assert con.execute("SELECT COUNT(*) FROM clients WHERE application_id='APP-0001'").fetchone()[0] == 0


@async_test
async def test_replicate_effects_to_audit_store(tmp_path):
    bank_path = tmp_path / "bank_repl.db"
    _setup_bank_db(bank_path)

    audit_path = tmp_path / "audit.db"
    store = EventStore(audit_path)
    await store.initialize()
    writer = BoundAuditWriter(store)

    run_binding = RunBinding(
        run_id="run-1",
        contract_id="contract-1",
        session_id="session-1",
        principal_id="alice",
        agent_id="onboarding-agent",
        policy_version="policy-v2",
        policy_hash="a" * 64,
        feed_version="feed-v1",
    )
    await writer.bind_run(run_binding)

    receipt = _sample_receipt("APP-0001")
    with sqlite3.connect(bank_path) as con:
        with con:
            con.execute("INSERT INTO clients VALUES ('CLI-0001', 'Alice Smith', 'APP-0001')")
            con.execute("INSERT INTO accounts VALUES ('ACC-0001', 'CLI-0001')")
            record_effect(con, receipt)

    # Replicate to audit store
    replicated = await replicate_effects(bank_path, writer)
    assert replicated == 1

    # Audit store has the event
    event = await store.get_event("evt-APP-0001")
    assert event is not None
    assert event.action_details.name == "create_client"
    assert event.context.effect_receipt_id == "rcpt-APP-0001"

    # Second replication run is a no-op because it was acknowledged
    replicated_again = await replicate_effects(bank_path, writer)
    assert replicated_again == 0

    await store.close()
