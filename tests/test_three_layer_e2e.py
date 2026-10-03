"""Executable local acceptance tests over actual SQLite delivery and business state."""
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import subprocess

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "simulation"))
import agent
import generate
from contracts import ActionProposal, PolicyAdjustmentSignal
from contracts.wire import decode_event
from simulation.governed import POLICY_PATH
from persistence import ActionDetails, ActionEventEnvelope, ActionStatus, ActionType, AuditContext, AuditorVerdict, InterceptionMetadata


@pytest.fixture(scope="module")
def dataset(tmp_path_factory):
    root = tmp_path_factory.mktemp("integrated-bank")
    generate.build(root)
    return root / "bank.db"


@pytest.fixture
def sessions(tmp_path, dataset):
    active = []

    def make(app_id="APP-0001", faults=(), config=None):
        db = tmp_path / f"bank-{len(active)}.db"
        shutil.copy(dataset, db)
        s = agent.Session(app_id, faults, db=db, quiet=True, policy_config=config)
        active.append(s)
        return s

    yield make
    for s in active:
        s.runtime.close()


def config():
    return json.loads(POLICY_PATH.read_text())


def test_happy_path_persisted_ordered_verified_once(sessions):
    s = sessions()
    agent.scripted(s)
    result = s.runtime.finish()
    assert result.verification_status == "VERIFIED_SUCCESS"
    with sqlite3.connect(s.ctx.db) as con:
        assert con.execute("SELECT COUNT(*) FROM clients WHERE application_id=?", (s.app_id,)).fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM effect_receipts WHERE application_id=?", (s.app_id,)).fetchone()[0] == 1
    events = s.runtime.events()
    assert [e["seq"] for e in events] == sorted(set(e["seq"] for e in events))
    decoded = [decode_event(e) for e in events]
    assert decoded[0].payload.phase == "started"
    assert decoded[-1].payload.phase == "ended"
    assert len([e for e in decoded if e.kind == "tool_use"]) == s.n
    assert all(d.decision in {"ALLOW", "REDACT"} for d in s.runtime.decisions)
    assert any(d.decision == "ALLOW" for d in s.runtime.decisions)
    assert not s.runtime.manager.feedback.log
    assert s.runtime.runner.run(s.runtime.store.pending_deliveries()) == 0
    persisted = s.runtime.runner.run(s.runtime.reader.session(s.id))
    assert [a.event_id for a in persisted] == [a.event_id for a in decoded]
    assert s.runtime.runner.run(s.runtime.reader.contract(s.id)) == s.runtime.contract
    # Identity bodies and inline result bodies never become generic evidence.
    raw = json.dumps(events)
    assert '"fields"' not in raw and '"dob"' not in raw and '"ocr_text"' not in raw


def test_missing_sanctions_write_prevented_and_lifecycle_verifies(sessions):
    s = sessions("APP-0003", ["skip_step:screen_sanctions"])
    agent.scripted(s)
    create = s.runtime.decisions[-1]
    assert create.reason_code == "SCREENING_NOT_COMPLETE"
    assert create.decision in {"BLOCK", "REQUIRE_APPROVAL"}
    with sqlite3.connect(s.ctx.db) as con:
        assert con.execute("SELECT COUNT(*) FROM clients WHERE application_id=?", (s.app_id,)).fetchone()[0] == 0
    result = s.runtime.finish()
    assert result.verification_status in {"FAILED_POSTCONDITIONS", "VERIFICATION_INCOMPLETE"}
    create_event = next(e for e in s.runtime.events() if e["action_details"].get("name") == "create_client")
    assert create_event["status"] in {"blocked", "pending_approval"}
    # Persisted blocked trajectory is handled by the real consumer ledger.
    assert s.runtime.manager.ledger.is_settled(create_event["event_id"], "trajectory-risk", "1.0.0")
    findings = s.runtime.runner.run(s.runtime.sink.findings(s.id))
    assert any(create_event["event_id"] in f["evidence_event_ids"] for f in findings)


def test_scope_drift_drives_real_risk_feedback_then_restricts(sessions):
    cfg = config()
    # Test-configured risk thresholds produce a repeatable high transition.
    cfg["consumer"]["trajectory_risk"] = {"levels": {"medium": 0.2, "high": 0.4, "critical": 50},
                                             "approval_tools": ["create_client"]}
    s = sessions(config=cfg)
    assert "error" not in s.execute("read_application", {"app_id": s.app_id})
    assert "error" in s.execute("read_application", {"app_id": "APP-0002"})
    # More permitted evidence lets the cumulative risk cross its high threshold.
    s.execute("read_documents", {"app_id": s.app_id})
    s.execute("read_application", {"app_id": s.app_id})
    accepted = [d for d in s.runtime.manager.feedback.log if d.accepted]
    assert any(d.signal.action == "REQUIRE_APPROVAL_FOR" for d in accepted)
    signal = next(d.signal for d in accepted if d.signal.action == "REQUIRE_APPROVAL_FOR")
    assert any(e["action_type"] == "control" and e["action_details"]["signal_id"] == signal.signal_id for e in s.runtime.events())
    agent.scripted(s)
    assert s.runtime.decisions[-1].decision in {"BLOCK", "REQUIRE_APPROVAL"}
    with sqlite3.connect(s.ctx.db) as con:
        assert con.execute("SELECT COUNT(*) FROM clients WHERE application_id=?", (s.app_id,)).fetchone()[0] == 0
    with sqlite3.connect(s.runtime.audit_path) as con:
        assert con.execute("SELECT COUNT(*) FROM consumer_findings").fetchone()[0] > 0


def signal_for(s, action, modifications):
    return PolicyAdjustmentSignal(signal_id=f"sig_test_{action}", ts=datetime.now(timezone.utc),
        target_scope={"session_id": s.id}, action=action, policy_modifications=modifications,
        reason="integration test", ttl_seconds=900, source_plugin="trajectory-risk",
        trigger_event_id=s.runtime.events()[-1]["event_id"])


@pytest.mark.parametrize("action,modifications", [("BLOCK_TOOLS", {"tools": ["read_application"]}), ("HALT_SESSION", {})])
def test_feedback_durable_authenticated_idempotent_restricts(sessions, action, modifications):
    s = sessions()
    s.execute("read_application", {"app_id": s.app_id})
    signal = signal_for(s, action, modifications)
    with pytest.raises((PermissionError, ValueError)):
        s.runtime.runner.run(s.runtime.gateway.apply_signal(signal, source=object()))
    for _ in range(2):
        s.runtime.runner.run(s.runtime.gateway.apply_signal(signal, source=s.runtime._feedback_credential))
    s.runtime.runner.run(s.runtime.settle())
    assert "error" in s.execute("read_application", {"app_id": s.app_id})
    assert s.runtime.decisions[-1].decision == "BLOCK"
    controls = [e for e in s.runtime.events() if e["action_type"] == "control"]
    assert len(controls) == 1


def test_budget_exhaustion_is_persisted_and_observed(sessions):
    cfg = config()
    cfg["budget"]["tool_calls"] = 1
    s = sessions(config=cfg)
    s.execute("read_application", {"app_id": s.app_id})
    assert "error" in s.execute("read_documents", {"app_id": s.app_id})
    assert s.runtime.decisions[-1].reason_code == "BUDGET_EXHAUSTED"
    blocked = s.runtime.events()[-1]
    assert blocked["status"] == "blocked"
    assert s.runtime.manager.ledger.is_settled(blocked["event_id"], "trajectory-risk", "1.0.0")


def test_identity_hint_and_action_replay_cannot_change_authority(sessions):
    s = sessions()
    proposal = ActionProposal(action_id="act_identity", session_id=s.id, agent_id="admin-agent",
                             tool="read_application", arguments={"app_id": s.app_id})
    decision, result = s.runtime.runner.run(s.runtime.gateway.execute(proposal))
    assert decision.decision == "BLOCK" and "error" in result
    proposal = ActionProposal(action_id="act_replay", session_id=s.id, agent_id=agent.AGENT,
                             tool="read_application", arguments={"app_id": s.app_id})
    first, _ = s.runtime.runner.run(s.runtime.gateway.execute(proposal))
    second, _ = s.runtime.runner.run(s.runtime.gateway.execute(proposal))
    assert first.decision == "ALLOW" and second.decision == "BLOCK"


def test_real_consumer_process_restart_delivers_committed_outbox(sessions):
    s = sessions()
    runtime = s.runtime
    candidate = ActionEventEnvelope(
        event_id="evt_restart_delivery", trace_id=runtime.contract.run_id, session_id=s.id,
        agent_id=agent.AGENT, case_id=s.app_id, action_type=ActionType.TOOL_CALL,
        status=ActionStatus.BLOCKED, action_details=ActionDetails(name="create_client"),
        interception_metadata=InterceptionMetadata(verdict=AuditorVerdict.BLOCKED,
                                                  policy_version=runtime.contract.policy_version),
        context=AuditContext(run_id=runtime.contract.run_id, action_id="act_restart_delivery"),
    )
    runtime.runner.run(runtime.persistence.append(candidate))
    audit_path = str(runtime.audit_path)
    runtime.close()
    code = """
import asyncio, sys
from persistence import EventStore
from persistence.governed import GovernedPersistence
from consume_plane.adapters.persistence import PersistenceEventSource
async def main():
    store = EventStore(sys.argv[1])
    await store.initialize()
    source = PersistenceEventSource(GovernedPersistence(store))
    await source.start()
    deliveries = await source.receive(1, 0.1)
    assert len(deliveries) == 1
    assert deliveries[0].action.event_id == 'evt_restart_delivery'
    await source.ack(deliveries[0].delivery_id)
    assert await store.pending_deliveries() == 0
    await store.close()
asyncio.run(main())
"""
    completed = subprocess.run([sys.executable, "-c", code, audit_path], cwd=Path(__file__).resolve().parents[1],
                               capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0, completed.stderr
    with sqlite3.connect(audit_path) as con:
        assert con.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0


def test_redelivery_dedupes_findings_feedback_and_business_effect(sessions):
    s = sessions()
    agent.scripted(s)
    s.runtime.finish()
    runtime = s.runtime
    before = runtime.runner.run(runtime.sink.findings(s.id))
    feedback_count = len(runtime.manager.feedback.log)
    events = runtime.events()
    def redeliver():
        con = runtime.store._get_connection()
        with con:
            con.executemany("INSERT INTO outbox(event_id,consumer_name) VALUES(?,?)",
                            [(e["event_id"], runtime.source.consumer_name) for e in events])
    async def replay():
        async with runtime.store._lock:
            await runtime.store._offload(redeliver)
        await runtime.settle()
    runtime.runner.run(replay())
    assert runtime.runner.run(runtime.sink.findings(s.id)) == before
    assert len(runtime.manager.feedback.log) == feedback_count
    with sqlite3.connect(s.ctx.db) as con:
        assert con.execute("SELECT COUNT(*) FROM clients WHERE application_id=?", (s.app_id,)).fetchone()[0] == 1
