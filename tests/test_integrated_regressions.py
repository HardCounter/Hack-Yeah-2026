"""Cross-layer recovery and uniqueness regressions for the integrated KYC runtime."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import shutil
import sqlite3
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "simulation"))
import agent
import generate
from contracts import ActionProposal, PolicyAdjustmentSignal
from contracts.wire import decode_event


@pytest.fixture(scope="module")
def bank_template(tmp_path_factory):
    root = tmp_path_factory.mktemp("integrated-regression-template")
    generate.build(root)
    return root / "bank.db"


@pytest.fixture
def session_factory(tmp_path, bank_template):
    active = []

    def make(*, shared_db=None, label=""):
        db = shared_db or tmp_path / f"bank-{len(active)}.db"
        if shared_db is None:
            shutil.copy(bank_template, db)
        session = agent.Session("APP-0001", db=db, quiet=True, label=label)
        active.append(session)
        return session

    yield make
    for session in active:
        session.runtime.close()


def prime_for_create(session):
    """Run the real read/screen/risk path and capture the final approved proposal."""
    captured = {}
    original_execute = session.execute

    def capture_decision(tool, args):
        if tool == "create_client":
            captured["args"] = args
            return {"deferred_for_test": True}
        return original_execute(tool, args)

    session.execute = capture_decision
    try:
        agent.scripted(session)
    finally:
        session.execute = original_execute
    assert "args" in captured
    return ActionProposal(
        action_id=f"act_regression_{session.id}",
        session_id=session.id,
        agent_id=agent.AGENT,
        tool="create_client",
        arguments=captured["args"],
        side_effect=agent.registry.REGISTRY["create_client"].side_effect,
    )


def execute(session, proposal):
    return session.runtime.runner.run(session.runtime.gateway.execute(proposal))


def db_counts(path, app_id="APP-0001"):
    with sqlite3.connect(path) as con:
        clients = con.execute("SELECT COUNT(*) FROM clients WHERE application_id=?", (app_id,)).fetchone()[0]
        receipts = con.execute("SELECT COUNT(*) FROM effect_receipts WHERE application_id=?", (app_id,)).fetchone()[0]
        pending = con.execute("SELECT COUNT(*) FROM business_audit_outbox WHERE acknowledged_at IS NULL").fetchone()[0]
    return clients, receipts, pending


def test_effect_recovered_after_final_audit_append_fails_without_repeating_client(session_factory, monkeypatch):
    session = session_factory()
    proposal = prime_for_create(session)
    persistence = session.runtime.persistence
    original_append = persistence.append
    failed = False

    async def fail_once_after_business_commit(candidate):
        nonlocal failed
        if candidate.context.action_id == proposal.action_id and candidate.context.effect_receipt_id and not failed:
            # The registry transaction, client and receipt are already committed when
            # the gateway tries to append this correlated result.
            assert db_counts(session.ctx.db)[:2] == (1, 1)
            failed = True
            raise OSError("synthetic final audit append interruption")
        return await original_append(candidate)

    monkeypatch.setattr(persistence, "append", fail_once_after_business_commit)
    decision, result = execute(session, proposal)
    assert failed, (decision.decision, decision.reason_code, result)
    assert decision.action_id == proposal.action_id
    assert "client_id" in result and "account_id" in result
    assert db_counts(session.ctx.db) == (1, 1, 0)

    # Retry with the identical action identity and command: reconcile the committed
    # receipt, append evidence once, and never invoke the business effect again.
    retry_decision, retry_result = execute(session, proposal)
    assert retry_decision.action_id == proposal.action_id
    assert {key: retry_result[key] for key in ("client_id", "account_id")} == {
        key: result[key] for key in ("client_id", "account_id")
    }
    events = session.runtime.events()
    correlated = [e for e in events if e.get("action_id") == proposal.action_id]
    finals = [e for e in correlated if e.get("status") == "completed" and
              e.get("interception_metadata", {}).get("final_decision") == "ALLOW"]
    assert len(finals) == 1, correlated
    internal = session.runtime.runner.run(session.runtime.store.get_event(finals[0]["event_id"]))
    assert internal.context.effect_receipt_id
    assert db_counts(session.ctx.db) == (1, 1, 0)
    recovered = decode_event(finals[0])
    assert recovered.payload.side_effect == "irreversible"
    assert recovered.case_id == session.app_id
    session.runtime.runner.run(session.runtime.settle())
    assert session.runtime.runner.run(session.runtime.store.pending_deliveries()) == 0
    assert session.runtime.finish().verification_status == "VERIFIED_SUCCESS"


def test_same_action_receipt_replay_rejects_substitution_and_exact_retry_recovers(session_factory):
    session = session_factory()
    proposal = prime_for_create(session)
    decision, result = execute(session, proposal)
    assert decision.decision == "ALLOW" and "client_id" in result, (decision.decision, decision.reason_code, result)
    before = db_counts(session.ctx.db)

    substitutions = [
        ActionProposal(**{**proposal.__dict__, "agent_id": "admin-agent"}),
        ActionProposal(**{**proposal.__dict__, "arguments": {"app_id": "APP-0002", "fields": {"name": "Substitute"}}}),
        ActionProposal(**{**proposal.__dict__, "side_effect": "read"}),
        ActionProposal(**{**proposal.__dict__, "tool": "read_application", "arguments": {"app_id": "APP-0001"}}),
    ]
    for changed in substitutions:
        denied, denied_result = execute(session, changed)
        assert denied.decision == "BLOCK"
        assert "error" in denied_result
    assert db_counts(session.ctx.db) == before

    recovered, same_result = execute(session, proposal)
    assert recovered.decision == "ALLOW" and recovered.reason_code == "EFFECT_RECOVERED"
    assert {key: same_result[key] for key in ("client_id", "account_id")} == {
        key: result[key] for key in ("client_id", "account_id")
    }
    assert db_counts(session.ctx.db) == before


def test_control_append_failure_replay_applies_one_restriction_and_control(session_factory, monkeypatch):
    session = session_factory()
    session.execute("read_application", {"app_id": session.app_id})
    trigger = session.runtime.events()[-1]["event_id"]
    signal = PolicyAdjustmentSignal(
        signal_id=f"sig_recovery_{session.id}", ts=datetime.now(timezone.utc),
        target_scope={"session_id": session.id}, action="BLOCK_TOOLS",
        policy_modifications={"tools": ["read_application"]}, reason="synthetic retry regression",
        ttl_seconds=900, source_plugin="trajectory-risk", trigger_event_id=trigger,
    )
    persistence = session.runtime.persistence
    original_append = persistence.append
    failed = False

    async def fail_once_for_control(candidate):
        nonlocal failed
        if candidate.action_type.name == "CONTROL" and candidate.context.intervention_id == signal.signal_id and not failed:
            failed = True
            raise OSError("synthetic control append interruption")
        return await original_append(candidate)

    monkeypatch.setattr(persistence, "append", fail_once_for_control)
    gateway = session.runtime.gateway
    credential = session.runtime._feedback_credential
    with pytest.raises(OSError):
        session.runtime.runner.run(gateway.apply_signal(signal, source=credential))
    assert failed

    # Same signal ID and payload is the only safe retry. Persistence deduplicates its
    # provenance row; the control event then commits before restriction activation.
    session.runtime.runner.run(gateway.apply_signal(signal, source=credential))
    controls = [e for e in session.runtime.events() if e["action_type"] == "control"
                and e["action_details"].get("signal_id") == signal.signal_id]
    assert len(controls) == 1
    with sqlite3.connect(session.runtime.audit_path) as con:
        row = con.execute("SELECT applied FROM policy_signals WHERE signal_id=?", (signal.signal_id,)).fetchone()
    assert row == (1,)
    assert "error" in session.execute("read_application", {"app_id": session.app_id})
    assert session.runtime.decisions[-1].decision == "BLOCK"


def test_parallel_sessions_share_application_and_commit_exactly_one_client(session_factory, tmp_path, bank_template):
    shared_db = tmp_path / "shared-application.db"
    shutil.copy(bank_template, shared_db)
    first = session_factory(shared_db=shared_db, label="parallel-a")
    second = session_factory(shared_db=shared_db, label="parallel-b")
    proposals = [prime_for_create(first), prime_for_create(second)]

    def dispatch(pair):
        session, proposal = pair
        return execute(session, proposal)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(dispatch, [(first, proposals[0]), (second, proposals[1])]))

    assert any("client_id" in result for _, result in results), results
    assert db_counts(shared_db)[:2] == (1, 1)
    # Each session receives a settled decision, and both independent audit stores
    # retain its own action outcome even though only one global client exists.
    assert all(decision.action_id == proposal.action_id for (decision, _), proposal in zip(results, proposals))
    assert all(session.runtime.events() for session in (first, second))
