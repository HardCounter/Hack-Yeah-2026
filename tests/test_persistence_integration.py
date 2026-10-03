"""Crash matrix and integrated persistence demonstration tests.

Uses isolated child Python subprocesses and read-only database connections to
verify recovery across explicit transaction boundaries.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from functools import wraps
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import pytest

from persistence.business import replicate_effects
from persistence.models import (
    ActionDetails,
    ActionStatus,
    AuditorVerdict,
    InterceptionMetadata,
    RunBinding,
)
from persistence.store import EventStore
from persistence.writer import BoundAuditWriter


def async_test(fn):
    @wraps(fn)
    def run(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))
    return run


def launch_crash(directory: Path, point: str) -> subprocess.CompletedProcess:
    root = str(Path(__file__).resolve().parent.parent)
    env = os.environ.copy()
    env["PYTHONPATH"] = root
    return subprocess.run(
        [sys.executable, "tests/persistence_crash_child.py", str(directory), point],
        capture_output=True,
        timeout=15,
        text=True,
        env=env,
        cwd=root,
    )


def read_client_count(bank_path: Path, app_id: str = "APP-0001") -> int:
    uri = f"{bank_path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM clients WHERE application_id = ?", (app_id,)
        ).fetchone()[0]


def read_receipt_count(bank_path: Path, app_id: str = "APP-0001") -> int:
    uri = f"{bank_path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM effect_receipts WHERE application_id = ?", (app_id,)
        ).fetchone()[0]


@async_test
async def test_crash_before_bank_commit(tmp_path: Path):
    workspace = tmp_path / "crash_before_commit"
    workspace.mkdir()

    # Launch child that crashes before banking commit
    res = launch_crash(workspace, "before_bank_commit")
    assert res.returncode == 70, res.stderr

    bank_path = workspace / "bank.db"
    assert read_client_count(bank_path) == 0
    assert read_receipt_count(bank_path) == 0

    # Clean run resumes and completes
    res_clean = launch_crash(workspace, "none")
    assert res_clean.returncode == 0, res_clean.stderr
    assert read_client_count(bank_path) == 1
    assert read_receipt_count(bank_path) == 1


@async_test
async def test_crash_after_bank_commit_before_audit_import(tmp_path: Path):
    workspace = tmp_path / "crash_after_bank"
    workspace.mkdir()

    # Launch child that crashes after bank commit before replication
    res = launch_crash(workspace, "after_bank_commit")
    assert res.returncode == 70, res.stderr

    bank_path = workspace / "bank.db"
    audit_path = workspace / "audit.db"

    # Banking effect was committed atomically
    assert read_client_count(bank_path) == 1
    assert read_receipt_count(bank_path) == 1

    # Restart replicator directly in orchestrator
    store = EventStore(audit_path)
    await store.initialize()
    writer = BoundAuditWriter(store)

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

    replicated = await replicate_effects(bank_path, writer)
    assert replicated == 1

    # Verify no second client was created and effect exists in audit store
    assert read_client_count(bank_path) == 1
    imported_event = await store.get_event("ev-bank-0001")
    assert imported_event is not None
    assert imported_event.context.effect_receipt_id == "rcpt-0001"

    # Re-running replication is idempotent and imports 0 additional events
    replicated_again = await replicate_effects(bank_path, writer)
    assert replicated_again == 0

    await store.close()


@async_test
async def test_crash_after_audit_import_before_consumer_result(tmp_path: Path):
    workspace = tmp_path / "crash_after_import"
    workspace.mkdir()

    res = launch_crash(workspace, "after_audit_import")
    assert res.returncode == 70, res.stderr

    bank_path = workspace / "bank.db"
    audit_path = workspace / "audit.db"

    assert read_client_count(bank_path) == 1
    assert read_receipt_count(bank_path) == 1

    # Audit store has imported effect
    store = EventStore(audit_path)
    await store.initialize()
    ev = await store.get_event("ev-bank-0001")
    assert ev is not None

    await store.close()
