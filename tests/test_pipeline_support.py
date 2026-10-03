from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import pytest

from simulation.pipeline_support import discover_bound_session, export_run, prepare_project, request
from persistence.models import ActionDetails, ActionEventEnvelope, ActionStatus, ActionType, AuditContext, AuditorDecision, AuditorVerdict, InterceptionMetadata
from persistence.store import EventStore


def test_prepare_isolated_gateway_only_agent_and_safe_provider_config(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    project = tmp_path / "project"
    provider = tmp_path / "providers.json"
    provider.write_text(json.dumps({"providers": {"mock": {"npm": "@ai-sdk/openai-compatible",
                                                               "options": {"baseURL": "http://127.0.0.1:9000/v1",
                                                                           "apiKey": "{env:MOCK_API_KEY}"}}}}))
    config_path = prepare_project(project, repo, "APP-0001", "contract_demo_v1", "mock/test", "http://127.0.0.1:8080", provider)
    config = json.loads(config_path.read_text())
    assert config["default_agent"] == "onboarding-agent"
    assert list(config["agents"]) == ["onboarding-agent"]
    assert config["agents"]["onboarding-agent"]["tools"] == {}
    assert config["plugins"][0]["options"]["registerTools"] is True
    assert config["providers"] == json.loads(provider.read_text())["providers"]
    assert os.stat(config_path).st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="isolated"):
        prepare_project(repo / "nested", repo, "APP-0001", "contract_demo_v1", "mock/test", "http://127.0.0.1:8080")
    provider.write_text('{"providers":{"mock":{"options":{"apiKey":"sk-test-12345678901234567890"}}}}')
    with pytest.raises(ValueError, match="credentials"):
        prepare_project(project, repo, "APP-0001", "contract_demo_v1", "mock/test", "http://127.0.0.1:8080", provider)


def test_discover_bound_session_requires_exact_contract_and_application(tmp_path):
    def make(session, contract, app):
        path = tmp_path / f"{session}.bank.db"
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE governed_baselines(session_id TEXT,contract_id TEXT,baseline_json TEXT)")
            db.execute("INSERT INTO governed_baselines VALUES(?,?,?)", (session, contract, json.dumps({"application_id": app})))
    make("sess_one", "contract_demo_v1", "APP-0001")
    make("sess_two", "contract_demo_v1", "APP-0002")
    assert discover_bound_session(tmp_path, "contract_demo_v1", "APP-0001") == "sess_one"
    assert discover_bound_session(tmp_path, "contract_missing", "APP-0001") is None


def test_request_restricts_loopback_route_and_does_not_expose_error_body():
    seen = {}
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen["path"] = self.path
            seen["auth"] = self.headers.get("Authorization")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "11")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
        def log_message(self, *_):
            pass
    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = request(f"http://127.0.0.1:{server.server_port}", "/v1/session/finish", {"session_id": "sess_test"}, "x" * 40)
        assert result == {"ok": True}
        assert seen == {"path": "/v1/session/finish", "auth": "Bearer " + "x" * 40}
        with pytest.raises(ValueError, match="loopback"):
            request("http://example.com:80", "/v1/session/finish", {}, "x" * 40)
        with pytest.raises(ValueError, match="unsupported"):
            request(f"http://127.0.0.1:{server.server_port}", "/other", {}, "x" * 40)
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_export_run_writes_canonical_and_privacy_limited_artifacts(tmp_path):
    async def setup():
        store = EventStore(tmp_path / "sess_export.evidence.db")
        await store.initialize()
        event = ActionEventEnvelope(
            trace_id="trace_x", session_id="sess_export", agent_id="agent_x",
            action_type=ActionType.TOOL_CALL, status=ActionStatus.EXECUTED,
            action_details=ActionDetails(name="read_application", parameters={"app_id": "APP-0001"}),
            interception_metadata=InterceptionMetadata(verdict=AuditorVerdict.ALLOWED,
                auditor_decisions=[AuditorDecision("rules", AuditorVerdict.ALLOWED, rule="allowed", latency_ms=1)],
                policy_version="policy_v1"), event_id="event_x", context=AuditContext(action_id="act_x"))
        await store.insert_event(event)
        con = store._get_connection()
        con.execute("INSERT INTO task_contracts VALUES(?,?,?,?)", ("sess_export", "run_x", "contract_x", '{"contract_id":"contract_x"}'))
        con.commit()
        await store.write_consumer_finding("finding_x", {"session_id": "sess_export", "severity": "high", "evidence_event_ids": ["event_x"], "rule_id": "risk_rule", "summary": "PRIVATE PERSON NAME"})
        await store.write_verification("sess_export", {"verification_status": "VERIFICATION_INCOMPLETE", "checks": [{"id": "create", "status": "INCOMPLETE", "detail": "MISSING_RECEIPT"}]})
        await store.close()
    asyncio.run(setup())
    runs = tmp_path / "runs"
    runs.mkdir()
    os.replace(tmp_path / "sess_export.evidence.db", runs / "sess_export.evidence.db")
    output = export_run(runs, "sess_export", tmp_path / "export")
    event = json.loads((output / "events.jsonl").read_text().splitlines()[0])
    assert event["schema_version"] == "2.1" and event["action_id"] == "act_x"
    assert json.loads((output / "contract.json").read_text()) == {"contract_id": "contract_x"}
    assert "PRIVATE PERSON NAME" not in (output / "findings.jsonl").read_text()
    assert json.loads((output / "verification.json").read_text())["checks"][0]["detail"] == "MISSING_RECEIPT"
    assert os.stat(output).st_mode & 0o777 == 0o700
    assert all(os.stat(output / name).st_mode & 0o777 == 0o600 for name in ("events.jsonl", "findings.jsonl", "contract.json", "verification.json"))
