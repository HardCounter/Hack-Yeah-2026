from __future__ import annotations

import asyncio
import json
import hashlib
import sqlite3
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest
import hashlib

from contracts import Budget, TaskContract
from consume_plane.adapters.memory import MemoryTrajectoryReader
from consume_plane.plugins.outcome_verifier import OutcomeVerifier
from contracts.wire import decode_event
from persistence import EventStore


class Context:
    def __init__(self, contract, trace_ids=("audit-read",)):
        self._contract = contract
        self.trace_ids = trace_ids
        self.findings = []

    async def contract(self):
        return self._contract

    def emit_finding(self, finding):
        self.findings.append(finding)

    async def trajectory(self):
        names = {"audit-read": "read_application", "audit-create": "create_client"}
        return SimpleNamespace(actions=tuple(SimpleNamespace(kind="tool_use", executed=True, status="completed",
            payload=SimpleNamespace(tool=names.get(action_id, "screen_sanctions" if action_id.startswith("audit-screen") else "unknown")), gateway=None,
            raw={"action_id": action_id}) for action_id in self.trace_ids))


def ended_action(app_id="APP-0001", session_id="sess-verifier"):
    raw = {
        "schema_version": "2.1", "event_id": "evt-verifier-end", "seq": 2,
        "ts": "2026-10-03T12:00:00Z", "run_id": "run-verifier", "session_id": session_id,
        "case_id": app_id, "agent_id": "kyc-agent", "action_type": "session", "status": "completed",
        "action_details": {"phase": "ended", "contract_id": "ctr-verifier", "policy_version": "policy-v1"},
    }
    return decode_event(raw)


def make_contract(app_id="APP-0001", session_id="sess-verifier"):
    return TaskContract(
        contract_id="ctr-verifier", session_id=session_id, run_id="run-verifier", principal_id="principal-test",
        agent_id="kyc-agent", role="reviewer", objective="Review this KYC application",
        target_ids=frozenset({app_id}), case_id=app_id, allowed_tools=frozenset({"read_application"}),
        postconditions=("ONB-P1", "ONB-P2", "ONB-P3", "ONB-P4", "ONB-P5", "ONB-P6"),
        budget=Budget(tokens=1000, tool_calls=10), policy_version="policy-v1",
        policy_hash="a" * 64, feed_version="feed-v1",
    )


def seed_bank(path, *, app_id="APP-0001", status="rejected", created_name=None, with_trace=True):
    data_dir = str(Path(__file__).resolve().parents[2] / "data")
    if data_dir not in sys.path:
        sys.path.insert(0, data_dir)
    from generate import build

    build(path.parent / "generated")
    generated = path.parent / "generated" / "bank.db"
    path.write_bytes(generated.read_bytes())
    with sqlite3.connect(path) as con:
        cols = {row[1] for row in con.execute("PRAGMA table_info(audit_actions)")}
        if "action_id" not in cols:
            con.execute("ALTER TABLE audit_actions ADD COLUMN action_id TEXT")
        con.execute("UPDATE onboarding_applications SET status=? WHERE application_id=?", (status, app_id))
        declared, applicant_type = con.execute(
            "SELECT declared, applicant_type FROM onboarding_applications WHERE application_id=?", (app_id,)
        ).fetchone()
        declared_obj = json.loads(declared)
        docs = [{"doc_id": r[0], "doc_type": r[1], "expiry_date": r[2]} for r in con.execute(
            "SELECT doc_id,doc_type,expiry_date FROM documents WHERE application_id=?", (app_id,)
        ).fetchall()]
        registry = None
        if applicant_type == "company" and declared_obj.get("reg_number"):
            row = con.execute("SELECT reg_number,legal_name,status,ubos FROM company_registry WHERE reg_number=?",
                              (declared_obj["reg_number"],)).fetchone()
            if row:
                registry = {"legal_name": row[1], "status": row[2], "ubos": json.loads(row[3] or "[]")}
        source_rows = []
        for table in ("sanctions_list", "pep_list"):
            source_rows.append((table, [tuple(r) for r in con.execute(f"SELECT * FROM {table} ORDER BY 1")]))
        source_version = hashlib.sha256(json.dumps(source_rows, sort_keys=True, default=str).encode()).hexdigest()
        con.execute("CREATE TABLE governed_baselines(session_id TEXT PRIMARY KEY, run_id TEXT, contract_id TEXT, case_id TEXT, baseline_json TEXT, captured_at TEXT)")
        con.execute("INSERT INTO governed_baselines VALUES(?,?,?,?,?,?)", (
            "sess-verifier", "run-verifier", "ctr-verifier", app_id,
            json.dumps({"application_id":app_id, "applicant_type":applicant_type,
                        "status":"pending", "declared":declared_obj, "documents":docs, "registry":registry,
                        "screening_source_version":source_version}),
            "2026-10-03T12:00:00Z"))
        con.execute("CREATE TABLE governed_screening_evidence(session_id TEXT,run_id TEXT,action_id TEXT,subject_hash TEXT,source_version TEXT,occurred_at TEXT,hit_count INTEGER,max_score REAL,decision TEXT)")
        d = declared_obj
        sys.path.insert(0, data_dir) if data_dir not in sys.path else None
        from rules import MATCH_THRESHOLD, screen
        def entries(table):
            return [{"entry_id": e, "primary_name": n, "aliases": json.loads(a or "[]"), "date_of_birth": dob}
                    for e, n, a, dob in con.execute(f"SELECT entry_id,primary_name,aliases,date_of_birth FROM {table}")]
        applicant_name = (registry or {}).get("legal_name") if applicant_type == "company" else d["name"]
        applicant_dob = None if applicant_type == "company" else d.get("date_of_birth")
        screen_subjects = [(applicant_name, applicant_dob, "audit-screen")]
        if applicant_type == "company":
            screen_subjects.extend((u["name"], u.get("dob"), f"audit-screen-ubo-{i}")
                                   for i, u in enumerate((registry or {}).get("ubos", []), 1)
                                   if u.get("ownership_pct", 0) >= 25)
        screen_results = {}
        for subject_name, subject_dob, action_id in screen_subjects:
            hits = []
            for tag, table in (("sanctions", "sanctions_list"), ("pep", "pep_list")):
                hits.extend({"list": tag, **hit} for hit in screen(subject_name, subject_dob, entries(table)))
            max_score = max((h["score"] for h in hits), default=0)
            decision = "HIT" if max_score >= MATCH_THRESHOLD else ("POSSIBLE" if hits else "CLEAR")
            subject_hash = hashlib.sha256(f"{subject_name.strip().casefold()}|{subject_dob}".encode()).hexdigest()
            con.execute("INSERT INTO governed_screening_evidence VALUES(?,?,?,?,?,?,?,?,?)", (
                "sess-verifier", "run-verifier", action_id, subject_hash,
                source_version, "2026-10-03T12:00:01Z", len(hits), max_score, decision))
            screen_results[action_id] = (subject_hash, len(hits), max_score, sorted({h["list"] for h in hits}))
        if created_name is not None:
            declared = json.loads(con.execute(
            "SELECT declared FROM onboarding_applications WHERE application_id=?", (app_id,)
            ).fetchone()[0])
            name = ((registry or {}).get("legal_name") if applicant_type == "company" else declared["name"]) if created_name == "__declared__" else created_name
            dob = None if applicant_type == "company" else declared.get("date_of_birth")
            con.execute("INSERT INTO clients(client_id,client_type,full_name,date_of_birth,application_id) VALUES(?,?,?,?,?)",
                        ("CLI-TEST-001", applicant_type, name, dob, app_id))
            con.execute("INSERT INTO accounts(account_id,client_id,currency,account_type,status,opened_at,opening_balance,balance) VALUES(?,?,'PLN','current','active','2026-10-03',0,0)",
                        ("ACC-TEST-001", "CLI-TEST-001"))
            con.execute("CREATE TABLE effect_receipts(receipt_id TEXT PRIMARY KEY, source_event_id TEXT UNIQUE, application_id TEXT UNIQUE, run_id TEXT, action_id TEXT, command_digest TEXT, client_id TEXT UNIQUE, account_id TEXT UNIQUE, policy_version TEXT, policy_hash TEXT, occurred_at TEXT)")
            con.execute("INSERT INTO effect_receipts VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
                "rcpt-1", "evt-create", app_id, "run-verifier", "audit-create", "d"*64,
                "CLI-TEST-001", "ACC-TEST-001", "policy-v1", "a"*64, "2026-10-03T12:00:02Z"))
        if with_trace:
            con.execute("INSERT INTO audit_actions(ts,session_id,agent,tool,args_json,result_json,action_id) VALUES(?,?,?,?,?,?,?)",
                        ("2026-10-03T12:00:00Z", "sess-verifier", "kyc-agent", "read_application",
                         json.dumps({"app_id": "APP-0001"}), "{}", "audit-read"))
            if created_name is not None:
                declared = json.loads(con.execute(
                    "SELECT declared FROM onboarding_applications WHERE application_id=?", (app_id,)
                ).fetchone()[0])
                name = ((registry or {}).get("legal_name") if applicant_type == "company" else declared["name"]) if created_name == "__declared__" else created_name
                dob = None if applicant_type == "company" else declared.get("date_of_birth")
                if created_name == "__declared__":
                    for _, _, action_id in screen_subjects:
                        subject_hash, hit_count, max_score, lists = screen_results[action_id]
                        con.execute("INSERT INTO audit_actions(ts,session_id,agent,tool,args_json,result_json,action_id) VALUES(?,?,?,?,?,?,?)",
                                    ("2026-10-03T12:00:01Z", "sess-verifier", "kyc-agent", "screen_sanctions",
                                     json.dumps({"subject_hash": subject_hash}),
                                     json.dumps({"hit_count":hit_count,"max_score":max_score,"lists":lists}), action_id))
                con.execute("INSERT INTO audit_actions(ts,session_id,agent,tool,args_json,result_json,action_id) VALUES(?,?,?,?,?,?,?)",
                            ("2026-10-03T12:00:02Z", "sess-verifier", "kyc-agent", "create_client",
                             json.dumps({"app_id": app_id}), "{}", "audit-create"))


@pytest.fixture
def verifier_store():
    store = EventStore(":memory:")
    asyncio.run(store.initialize())
    yield store
    asyncio.run(store.close())


def test_terminal_no_create_verifies_and_is_durably_readable(tmp_path, verifier_store):
    bank_path = tmp_path / "bank.db"
    seed_bank(bank_path, status="rejected")
    plugin = OutcomeVerifier(bank_paths={"sess-verifier": bank_path}, result_store=verifier_store)
    asyncio.run(plugin.setup(SimpleNamespace(config={})))
    ctx = Context(make_contract())
    asyncio.run(plugin.handle(ended_action(), ctx))

    result = asyncio.run(plugin.result("sess-verifier"))
    assert result.verification_status == "VERIFIED_SUCCESS"
    assert next(c for c in result.checks if c.id == "KYC-CLIENT-COUNT").status == "PASS"
    assert ctx.findings[0].details["verification_status"] == "VERIFIED_SUCCESS"


def test_approved_application_requires_exactly_one_client_and_create_trace(tmp_path, verifier_store):
    bank_path = tmp_path / "bank.db"
    seed_bank(bank_path, status="approved", created_name="__declared__")
    plugin = OutcomeVerifier(bank_paths={"sess-verifier": bank_path}, result_store=verifier_store)
    asyncio.run(plugin.setup(SimpleNamespace(config={})))
    ctx = Context(make_contract(), ("audit-read", "audit-screen", "audit-create"))
    asyncio.run(plugin.handle(ended_action(), ctx))

    result = asyncio.run(plugin.result("sess-verifier"))
    assert result.verification_status == "VERIFIED_SUCCESS"
    assert next(c for c in ctx.findings[0].details["checks"] if c["id"] == "KYC-CLIENT-COUNT")["status"] == "PASS"
    assert next(c for c in ctx.findings[0].details["checks"] if c["id"] == "KYC-CREATE-TRACE")["status"] == "PASS"


def test_wrong_persisted_state_fails_without_copying_pii_to_result(tmp_path, verifier_store):
    bank_path = tmp_path / "bank.db"
    seed_bank(bank_path, status="approved", created_name="A Person With Sensitive Data")
    plugin = OutcomeVerifier(bank_paths={"sess-verifier": bank_path}, result_store=verifier_store)
    asyncio.run(plugin.setup(SimpleNamespace(config={})))
    ctx = Context(make_contract(), ("audit-read", "audit-screen", "audit-create"))
    asyncio.run(plugin.handle(ended_action(), ctx))

    result = asyncio.run(plugin.result("sess-verifier"))
    assert result.verification_status == "FAILED_POSTCONDITIONS"
    assert any(c["id"] == "ONB-P2" and c["status"] == "FAIL" and c["detail"] == "POSTCONDITION_FAILED"
               for c in ctx.findings[0].details["checks"])
    serialized = json.dumps(result.to_dict())
    assert "A Person With Sensitive Data" not in serialized
    assert "1990-01-01" not in serialized


def test_approved_client_without_account_fails(tmp_path):
    bank_path = tmp_path / "bank.db"
    seed_bank(bank_path, status="approved", created_name="__declared__")
    with sqlite3.connect(bank_path) as con:
        con.execute("DELETE FROM accounts")
    plugin = OutcomeVerifier(bank_paths={"sess-verifier": bank_path})
    asyncio.run(plugin.setup(SimpleNamespace(config={})))
    ctx = Context(make_contract(), ("audit-read", "audit-screen", "audit-create"))
    asyncio.run(plugin.handle(ended_action(), ctx))
    assert ctx.findings[0].details["verification_status"] == "FAILED_POSTCONDITIONS"
    assert next(c for c in ctx.findings[0].details["checks"] if c["id"] == "KYC-ACCOUNT-LINK")["status"] == "FAIL"


def test_missing_effect_receipt_is_incomplete(tmp_path):
    bank_path = tmp_path / "bank.db"
    seed_bank(bank_path, status="approved", created_name="__declared__")
    with sqlite3.connect(bank_path) as con:
        con.execute("DELETE FROM effect_receipts")
    plugin = OutcomeVerifier(bank_paths={"sess-verifier": bank_path})
    asyncio.run(plugin.setup(SimpleNamespace(config={})))
    ctx = Context(make_contract(), ("audit-read", "audit-screen", "audit-create"))
    asyncio.run(plugin.handle(ended_action(), ctx))
    assert ctx.findings[0].details["verification_status"] == "VERIFICATION_INCOMPLETE"
    assert next(c for c in ctx.findings[0].details["checks"] if c["id"] == "KYC-EFFECT-RECEIPT")["detail"] == "EFFECT_RECEIPT_MISSING"


@pytest.mark.parametrize(("column", "value", "code"), [
    ("source_version", "tampered", "SCREENING_SOURCE_VERSION_MISMATCH"),
    ("action_id", "act_unrelated", "SCREENING_ACTION_BINDING_MISMATCH"),
    ("decision", "CLEAR", "SCREENING_RESULT_MISMATCH"),
    ("hit_count", "hit_count - 1", "SCREENING_RESULT_MISMATCH"),
    ("max_score", "0.0", "SCREENING_RESULT_MISMATCH"),
    ("occurred_at", "1900-01-01T00:00:00Z", "SCREENING_CHRONOLOGY_INVALID"),
])
def test_tampered_screening_evidence_never_verifies(tmp_path, column, value, code):
    bank_path = tmp_path / "bank.db"
    seed_bank(bank_path, status="approved", created_name="__declared__")
    with sqlite3.connect(bank_path) as con:
        if value == "hit_count - 1":
            con.execute(f"UPDATE governed_screening_evidence SET {column}={column}-1")
        else:
            con.execute(f"UPDATE governed_screening_evidence SET {column}=?", (value,))
    plugin = OutcomeVerifier(bank_paths={"sess-verifier": bank_path})
    asyncio.run(plugin.setup(SimpleNamespace(config={})))
    ctx = Context(make_contract(), ("audit-read", "audit-screen", "audit-create"))
    asyncio.run(plugin.handle(ended_action(), ctx))
    assert ctx.findings[0].details["verification_status"] == "FAILED_POSTCONDITIONS"
    assert next(c for c in ctx.findings[0].details["checks"] if c["id"] == "KYC-SCREENING-EVIDENCE")["detail"] == code


def test_company_verifier_uses_registry_legal_name_and_checks_ubo_screenings(tmp_path):
    bank_path = tmp_path / "company.db"
    seed_bank(bank_path, app_id="APP-0002", status="approved", created_name="__declared__")
    plugin = OutcomeVerifier(bank_paths={"sess-verifier": bank_path})
    asyncio.run(plugin.setup(SimpleNamespace(config={})))
    ctx = Context(make_contract("APP-0002"),
                  ("audit-read", "audit-screen", "audit-screen-ubo-1", "audit-screen-ubo-2", "audit-create"))
    asyncio.run(plugin.handle(ended_action("APP-0002"), ctx))
    checks = ctx.findings[0].details["checks"]
    assert next(c for c in checks if c["id"] == "KYC-CLIENT-IDENTITY")["status"] == "PASS"
    assert next(c for c in checks if c["id"] == "KYC-SCREENING-EVIDENCE")["status"] == "PASS"


def test_missing_bank_or_process_trace_is_incomplete(tmp_path, verifier_store):
    plugin = OutcomeVerifier(bank_paths={"sess-verifier": tmp_path / "missing.db"}, result_store=verifier_store)
    asyncio.run(plugin.setup(SimpleNamespace(config={})))
    ctx = Context(make_contract())
    asyncio.run(plugin.handle(ended_action(), ctx))
    result = asyncio.run(plugin.result("sess-verifier"))
    assert result.verification_status == "VERIFICATION_INCOMPLETE"
    assert result.checks[0].detail == "BANK_STATE_UNAVAILABLE"

    bank_path = tmp_path / "no-trace.db"
    seed_bank(bank_path, with_trace=False)
    plugin = OutcomeVerifier(bank_paths={"sess-verifier": bank_path})
    asyncio.run(plugin.setup(SimpleNamespace(config={})))
    ctx = Context(make_contract())
    asyncio.run(plugin.handle(ended_action(), ctx))
    assert ctx.findings[0].details["verification_status"] == "VERIFICATION_INCOMPLETE"
    assert ctx.findings[0].details["checks"][0]["detail"] == "PROCESS_TRACE_MISSING"
