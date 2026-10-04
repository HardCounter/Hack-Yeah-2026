"""Focused security and evidence checks for the governed KYC gateway."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import sqlite3
import sys
import asyncio
from datetime import datetime, timezone

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "simulation"))
import agent  # noqa: E402
import generate  # noqa: E402
from contracts import ActionProposal, PolicyAdjustmentSignal  # noqa: E402


@pytest.fixture
def session(tmp_path):
    dataset = tmp_path / "dataset"
    generate.build(dataset)
    db = tmp_path / "bank.db"
    shutil.copy(dataset / "bank.db", db)
    s = agent.Session("APP-0001", (), db=db, quiet=True)
    yield s
    s.runtime.close()


def proposal(s, action_id, tool, args, side_effect="read"):
    return ActionProposal(action_id=action_id, session_id=s.id, agent_id=s.ctx.agent,
                          tool=tool, arguments=args, side_effect=side_effect)


def test_persists_protected_baseline_and_sanitized_screening_trace(session):
    s = session
    agent.scripted(s)
    result = s.runtime.finish()
    assert result.verification_status == "VERIFIED_SUCCESS"
    with sqlite3.connect(s.ctx.db) as con:
        baseline = con.execute("SELECT run_id,contract_id,case_id,baseline_json FROM governed_baselines WHERE session_id=?",
                               (s.id,)).fetchone()
        assert baseline[:3] == (s.runtime.contract.run_id, s.runtime.contract.contract_id, s.app_id)
        assert json.loads(baseline[3])["application_id"] == s.app_id
        screen = con.execute("SELECT action_id,subject_hash,source_version,hit_count FROM governed_screening_evidence WHERE session_id=?",
                             (s.id,)).fetchone()
        assert screen and screen[0].startswith("act_") and len(screen[1]) == 64 and len(screen[2]) == 64
        assert con.execute("SELECT COUNT(*) FROM effect_receipts WHERE application_id=?", (s.app_id,)).fetchone()[0] == 1
        audit = " ".join(row[0] + " " + row[1] for row in con.execute(
            "SELECT args_json,result_json FROM audit_actions WHERE session_id=?", (s.id,)))
        assert "Katarzyna Nowak" not in audit


def test_pipeline_output_redaction_is_reported_and_raw_result_is_not_persisted(session):
    s = session
    with sqlite3.connect(s.ctx.db) as con:
        name = json.loads(con.execute("SELECT declared FROM onboarding_applications WHERE application_id=?",
                                      (s.app_id,)).fetchone()[0])["name"]
    s.runtime.gateway.pipeline = __import__("intercept.policy.auditors", fromlist=["Pipeline"]).Pipeline([
        {"id": "redact-person", "type": "pattern_scanner",
         "config": {"patterns": [name], "action": "REDACT"}}
    ])
    result = s.execute("read_application", {"app_id": s.app_id})
    assert result["declared"]["name"] == "[REDACTED]"
    decision = s.runtime.decisions[-1]
    assert decision.decision == "REDACT"
    assert any(row.get("phase") == "output" and row["decision"] == "REDACT"
               for row in decision.auditor_decisions)
    serialized = json.dumps(s.runtime.events())
    assert name not in serialized


def test_committed_effect_receipt_cannot_authorize_conflicting_replay(session):
    s = session
    agent.scripted(s)
    with sqlite3.connect(s.ctx.db) as con:
        action_id = con.execute("SELECT action_id FROM audit_actions WHERE session_id=? AND tool='create_client'",
                                (s.id,)).fetchone()[0]
    # Reusing a create action ID with another authenticated agent is rejected
    # before receipt recovery can reveal the original business result.
    wrong_agent = proposal(s, action_id, "create_client",
                           {"app_id": s.app_id, "fields": {"name": "Different Person", "dob": "1970-01-01"}},
                           "irreversible")
    wrong_agent = ActionProposal(action_id=wrong_agent.action_id, session_id=wrong_agent.session_id,
                                 agent_id="admin-agent", tool=wrong_agent.tool,
                                 arguments=wrong_agent.arguments, side_effect=wrong_agent.side_effect)
    decision, result = s.runtime.runner.run(s.runtime.gateway.execute(wrong_agent))
    assert decision.decision == "BLOCK"
    assert decision.reason_code == "IDENTITY_CONFLICT"
    assert result == {"error": "IDENTITY_CONFLICT"}

    wrong_subject = proposal(s, action_id, "create_client",
                             {"app_id": s.app_id, "fields": {"name": "Different Person", "dob": "1970-01-01"}},
                             "irreversible")
    decision, result = s.runtime.runner.run(s.runtime.gateway.execute(wrong_subject))
    assert decision.decision == "BLOCK"
    assert decision.reason_code == "APPROVED_IDENTITY_MISMATCH"
    assert result == {"error": "APPROVED_IDENTITY_MISMATCH"}
    with sqlite3.connect(s.ctx.db) as con:
        assert con.execute("SELECT COUNT(*) FROM clients WHERE application_id=?", (s.app_id,)).fetchone()[0] == 1
    s.runtime.finish()


def test_output_block_after_read_records_blocked_verdict_and_failed_status(session):
    s = session
    base = s.runtime.gateway.pipeline

    class BlockReadOutput:
        async def evaluate(self, action):
            if action["tool"] == "read_application" and "tool_result" in action["arguments"]:
                return action, ({"auditor": "test", "decision": "BLOCK", "code": "OUTPUT_DENY"},), "BLOCK", False
            return await base.evaluate(action)

    s.runtime.gateway.pipeline = BlockReadOutput()
    result = s.execute("read_application", {"app_id": s.app_id})
    assert result == {"error": "OUTPUT_INSPECTION_BLOCK"}
    event = next(e for e in reversed(s.runtime.events()) if e["action_details"]["name"] == "read_application")
    assert event["status"] == "failed"
    assert event["interception_metadata"]["final_decision"] == "BLOCK"
    assert event["interception_metadata"]["auditor_decisions"][-1]["rule_id"] == "OUTPUT_DENY"


def test_hard_budget_deny_wins_over_pipeline_approval(session):
    s = session
    config = json.loads((ROOT / "simulation" / "policy.json").read_text())
    config["budget"]["tool_calls"] = 1
    s.runtime.close()
    s = agent.Session(s.app_id, (), db=s.ctx.db, quiet=True, policy_config=config)
    first = s.runtime.execute("read_application", {"app_id": s.app_id})
    assert "error" not in first

    class RequiresApproval:
        async def evaluate(self, action):
            return action, ({"auditor": "semantic", "decision": "REQUIRE_APPROVAL", "code": "NEEDS_REVIEW"},), "REQUIRE_APPROVAL", False

    s.runtime.gateway.pipeline = RequiresApproval()
    decision, result = s.runtime.runner.run(s.runtime.gateway.execute(
        proposal(s, "budget-overrun", "read_application", {"app_id": s.app_id})))
    assert decision.decision == "BLOCK"
    assert decision.reason_code == "BUDGET_EXHAUSTED"
    assert result == {"error": "BUDGET_EXHAUSTED"}
    s.runtime.close()


def test_feedback_cancel_after_durable_marker_keeps_restriction_active(session, monkeypatch):
    s = session
    s.execute("read_application", {"app_id": s.app_id})
    trigger = s.runtime.events()[-1]["event_id"]
    signal = PolicyAdjustmentSignal(
        signal_id="sig_cancel_marker", ts=datetime.now(timezone.utc),
        target_scope={"session_id": s.id, "agent_id": s.ctx.agent}, action="BLOCK_TOOLS",
        policy_modifications={"blocked_tools": ["read_documents"]}, reason="test restriction",
        ttl_seconds=60, source_plugin="test", trigger_event_id=trigger,
    )
    original = s.runtime.persistence.mark_signal_applied

    async def cancelled_after_commit(signal_id):
        await original(signal_id)
        raise asyncio.CancelledError

    monkeypatch.setattr(s.runtime.persistence, "mark_signal_applied", cancelled_after_commit)
    with pytest.raises(asyncio.CancelledError):
        s.runtime.runner.run(s.runtime.gateway.apply_signal(signal, source=s.runtime._feedback_credential))
    decision, result = s.runtime.runner.run(s.runtime.gateway.execute(
        proposal(s, "blocked-after-cancel", "read_documents", {"app_id": s.app_id})))
    assert decision.decision == "BLOCK"
    assert decision.reason_code == "INTERVENTION_BLOCKED_TOOL"
    assert result == {"error": "INTERVENTION_BLOCKED_TOOL"}


def test_unhashable_and_invalid_tool_arguments_fail_closed_without_crashing(session):
    s = session
    # extract_fields with unhashable list doc_id
    decision, result = s.runtime.runner.run(s.runtime.gateway.execute(
        proposal(s, "unhashable-doc", "extract_fields", {"doc_id": ["DOC-0001"]})))
    assert decision.decision == "BLOCK"
    assert decision.reason_code == "DOCUMENT_OUT_OF_SCOPE"
    assert result == {"error": "DOCUMENT_OUT_OF_SCOPE"}

    # screen_sanctions with unhashable/non-string args
    decision, result = s.runtime.runner.run(s.runtime.gateway.execute(
        proposal(s, "invalid-screen-name", "screen_sanctions", {"name": ["Invalid"], "dob": "1990-01-01"})))
    assert decision.decision == "BLOCK"
    assert decision.reason_code == "SCREEN_SUBJECT_OUT_OF_SCOPE"
    assert result == {"error": "SCREEN_SUBJECT_OUT_OF_SCOPE"}

    decision, result = s.runtime.runner.run(s.runtime.gateway.execute(
        proposal(s, "invalid-screen-dob", "screen_sanctions", {"name": "Valid Name", "dob": {"not": "string"}})))
    assert decision.decision == "BLOCK"
    assert decision.reason_code == "SCREEN_SUBJECT_OUT_OF_SCOPE"
    assert result == {"error": "SCREEN_SUBJECT_OUT_OF_SCOPE"}


def test_approved_identity_changed_detects_all_identity_fields(session):
    gateway = session.runtime.gateway
    original = {"fields": {"name": "Alice", "dob": "1990-01-01", "national_id": "12345"}}
    # Mutating national_id must be detected as identity change
    modified = {"fields": {"name": "Alice", "dob": "1990-01-01", "national_id": "[REDACTED]"}}
    assert gateway._approved_identity_changed(original, modified) is True

