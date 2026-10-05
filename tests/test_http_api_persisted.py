"""Real SQLite reads, privacy, error handling, keyset pagination and export integrity."""
import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from contracts import Budget, TaskContract
from persistence.events import build_action_event
from persistence.governed import GovernedPersistence
from persistence.http_api import create_app
from persistence.models import ActionStatus
from persistence.settings import PersistenceSettings
from persistence.store import EventStore


def seed(directory, session="ses_demo", *, settings=None, verification=True):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session}.evidence.db"
    async def write():
        store = EventStore(path, settings=settings)
        await store.initialize()
        boundary = GovernedPersistence(store)
        contract = TaskContract(contract_id=f"contract_{session}", run_id=f"run_{session}", session_id=session,
                                principal_id="principal_demo", agent_id="onboarding-agent", case_id="APP-0001",
                                role="KYC analyst", objective="private objective must not be returned",
                                target_ids=frozenset({"APP-0001"}), allowed_tools=frozenset({"read_application"}),
                                postconditions=("ONB-P1",), budget=Budget(tokens=1000, tool_calls=3),
                                policy_version="policy_demo", policy_hash="a" * 64, feed_version="feed_demo")
        await boundary.persist_contract(contract)
        started = build_action_event(contract=contract, action_type="session", action_id="start",
                                     name="session_started", status="EXECUTED", wire_details={"phase": "started"})
        started.ts = "2026-10-04T10:00:00Z"
        await boundary.append(started)
        intent = build_action_event(contract=contract, action_type="tool_call", action_id="act_allowed",
                                    name="read_application", status="PENDING", intent=True)
        await boundary.intent(intent)
        allowed = build_action_event(contract=contract, action_type="tool_call", action_id="act_allowed",
                                     name="read_application", status="ALLOW", parameters={"app_id": "APP-0001", "secret": "private marker"})
        allowed.event_id = f"evt_{session}_allowed"
        allowed.ts = "2026-10-04T10:00:01Z"
        await boundary.append(allowed)
        denied = build_action_event(contract=contract, action_type="tool_call", action_id="act_denied",
                                    name="read_application", status="BLOCK", reason_code="REGEX_PATTERN_MATCH",
                                    auditor_rows=[{"auditor": "pattern-match", "decision": "BLOCK", "rule_id": "regex.pattern.0"}])
        denied.event_id = f"evt_{session}_denied"
        denied.ts = "2026-10-04T10:00:02Z"
        await boundary.append(denied)
        if verification:
            await store.write_verification(session, {"verification_status": "VERIFIED_SUCCESS", "checks": [
                {"id": "ONB-P1", "status": "PASS", "detail": "BANK_STATE_MATCH"}]})
        await store.close()
    asyncio.run(write())
    return path


@pytest.fixture
def live(tmp_path):
    path = seed(tmp_path / "evidence")
    return TestClient(create_app(evidence_dir=path.parent)), path


def test_health_stats_session_and_verification_read_real_rows(live):
    client, path = live
    assert client.get("/api/v1/health").json()["status"] == "ok"
    stats = client.get("/api/v1/system/stats").json()
    assert stats["total_events"] == 4 and stats["evidence_stores"] == 1
    assert stats["db_size_bytes"] == path.stat().st_size
    sessions = client.get("/api/v1/sessions").json()["items"]
    assert [item["session_id"] for item in sessions] == ["ses_demo"]
    assert sessions[0]["action_count"] == 3 and sessions[0]["blocked_count"] == 1
    assert sessions[0]["verification_status"] == "VERIFIED_SUCCESS"
    detail = client.get("/api/v1/sessions/ses_demo").json()
    assert detail["contract"]["budget"]["tool_calls"] == 3
    assert "objective" not in detail["contract"]
    verification = client.get("/api/v1/sessions/ses_demo/verification").json()
    assert verification["verification_status"] == "VERIFIED_SUCCESS"
    assert verification["verified_at"] is None
    assert verification["checks"][0]["status"] == "PASS"


def test_action_and_timeline_have_no_private_bodies_and_exclude_intents(live):
    client, _ = live
    response = client.get("/api/v1/actions/evt_ses_demo_denied")
    assert response.status_code == 200
    assert response.json()["summary"]["reason_code"] == "REGEX_PATTERN_MATCH"
    assert response.json()["previous_event_id"] == "evt_ses_demo_allowed"
    assert response.json()["event"]["schema_version"] == "2.1"
    assert response.json()["detections"][0]["source"] == "gateway"
    timeline = client.get("/api/v1/trajectories/session/ses_demo?view=full").json()
    assert len(timeline["segments"][0]["steps"]) == 3
    assert [step["summary"]["seq"] for step in timeline["segments"][0]["steps"]] == [0, 1, 2]
    raw = json.dumps(timeline)
    assert "private marker" not in raw and "private objective" not in raw
    filtered = client.get("/api/v1/trajectories/session/ses_demo?statuses=blocked&include_detections=false").json()
    assert len(filtered["segments"][0]["steps"]) == 1
    assert filtered["segments"][0]["steps"][0]["detections"] == []


def test_keyset_pagination_and_filter_binding(live):
    client, _ = live
    first = client.get("/api/v1/trajectories/session/ses_demo?limit=1").json()
    cursor = first["next_cursor"]
    second = client.get("/api/v1/trajectories/session/ses_demo", params={"limit": 1, "cursor": cursor}).json()
    assert first["segments"][0]["steps"][0]["seq"] == 0
    assert second["segments"][0]["steps"][0]["seq"] == 1
    changed = client.get("/api/v1/trajectories/session/ses_demo", params={"limit": 1, "cursor": cursor, "statuses": "blocked"})
    assert changed.status_code == 400 and changed.json()["error"]["code"] == "invalid_cursor"


def test_session_filters_and_pagination_across_files(tmp_path):
    seed(tmp_path, "ses_a")
    seed(tmp_path, "ses_b")
    client = TestClient(create_app(evidence_dir=tmp_path))
    first = client.get("/api/v1/sessions?limit=1").json()
    second = client.get("/api/v1/sessions", params={"limit": 1, "cursor": first["next_cursor"]}).json()
    assert first["items"][0]["session_id"] != second["items"][0]["session_id"]
    assert client.get("/api/v1/sessions?case_id=APP-0002").json()["items"] == []
    assert client.get("/api/v1/sessions?verification_status=none").json()["items"] == []


def test_time_bounds_are_inclusive_exclusive_at_microsecond_precision(live):
    client, _ = live
    url = "/api/v1/trajectories/session/ses_demo"
    result = client.get(url, params={"since": "2026-10-04T10:00:01.000000Z", "until": "2026-10-04T10:00:02.000000Z"}).json()
    assert [step["event_id"] for step in result["segments"][0]["steps"]] == ["evt_ses_demo_allowed"]
    result = client.get(url, params={"since": "2026-10-04T10:00:01.000001Z"}).json()
    assert [step["event_id"] for step in result["segments"][0]["steps"]] == ["evt_ses_demo_denied"]


def test_no_result_is_different_from_unknown_session(tmp_path):
    seed(tmp_path, verification=False)
    client = TestClient(create_app(evidence_dir=tmp_path))
    response = client.get("/api/v1/sessions/ses_demo/verification")
    assert response.status_code == 200 and response.json()["verification_status"] is None
    missing = client.get("/api/v1/sessions/missing/verification")
    assert missing.status_code == 404
    assert client.get("/api/v1/actions/unknown").status_code == 404


def test_export_integrity_related_intents_and_limits(live, tmp_path):
    client, _ = live
    response = client.get("/api/v1/export/sessions/ses_demo")
    assert response.status_code == 200
    lines = response.content.splitlines(keepends=True)
    records = [json.loads(line) for line in lines]
    assert records[0]["record_type"] == "session"
    assert records[-1]["sha256"] == hashlib.sha256(b"".join(lines[:-1])).hexdigest()
    assert records[-1]["rows"] == len(records) - 1
    assert any(record["record_type"] == "intent" for record in records)
    assert "private objective" not in response.text and "private marker" not in response.text
    path = seed(tmp_path / "limited", settings=PersistenceSettings(max_export_rows=2))
    limited = TestClient(create_app(evidence_dir=path.parent)).get("/api/v1/export/sessions/ses_demo")
    assert limited.status_code == 413 and limited.json()["error"]["code"] == "export_quota_exceeded"


def test_expired_run_corrupt_store_and_read_only_connection(live, tmp_path):
    client, path = live
    before = path.read_bytes()
    assert client.get("/api/v1/actions/evt_ses_demo_allowed").status_code == 200
    assert path.read_bytes() == before
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE audit_runs SET lifecycle='EXPIRED'")
    assert client.get("/api/v1/trajectories/session/ses_demo").status_code == 410
    assert client.get("/api/v1/sessions").status_code == 200
    bad_dir = tmp_path / "bad"
    bad_dir.mkdir()
    (bad_dir / "ses_broken.evidence.db").write_text("not sqlite")
    broken = TestClient(create_app(evidence_dir=bad_dir))
    assert broken.get("/api/v1/health").status_code == 503
    assert broken.get("/api/v1/system/stats").status_code == 503
    assert "not sqlite" not in broken.get("/api/v1/system/stats").text


def test_read_reapplies_privacy_even_if_database_contains_raw_content(live):
    client, path = live
    marker = "PRIVATE_BODY_MUST_NOT_LEAVE_STORE"
    with sqlite3.connect(path) as conn:
        row = conn.execute("SELECT payload_json FROM events WHERE event_id=?", ("evt_ses_demo_denied",)).fetchone()
        event = json.loads(row[0])
        event["action_details"]["result"] = {"raw": marker}
        event["interception_metadata"]["auditor_decisions"][0]["reason"] = marker
        conn.execute("UPDATE events SET payload_json=? WHERE event_id=?", (json.dumps(event), "evt_ses_demo_denied"))
        finding = {"session_id": "ses_demo", "rule_id": "rule_demo", "plugin": "trajectory-risk",
                   "severity": "high", "summary": marker, "trigger_event_id": "evt_ses_demo_denied",
                   "evidence_event_ids": ["evt_ses_demo_denied"]}
        conn.execute("INSERT INTO consumer_findings VALUES(?,?,?)", ("finding_demo", "ses_demo", json.dumps(finding)))
    for url in ("/api/v1/actions/evt_ses_demo_denied", "/api/v1/trajectories/session/ses_demo?view=full",
                "/api/v1/export/sessions/ses_demo"):
        response = client.get(url)
        assert response.status_code == 200
        assert marker not in response.text


def test_cursor_anchor_removed_by_retention_is_rejected(live):
    client, path = live
    first = client.get("/api/v1/trajectories/session/ses_demo?limit=1").json()
    anchor = first["segments"][0]["steps"][0]["event_id"]
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM events WHERE event_id=?", (anchor,))
    response = client.get("/api/v1/trajectories/session/ses_demo", params={"limit": 1, "cursor": first["next_cursor"]})
    assert response.status_code == 400 and response.json()["error"]["code"] == "invalid_cursor"


def test_empty_missing_unimplemented_and_explicit_example_modes(tmp_path):
    empty = TestClient(create_app(evidence_dir=tmp_path))
    assert empty.get("/api/v1/health").status_code == 200
    assert empty.get("/api/v1/sessions").json()["items"] == []
    assert empty.get("/api/v1/system/stats").json()["total_events"] == 0
    assert empty.get("/api/v1/metrics/security").json()["actions"]["total"] == 0
    assert empty.get("/api/v1/trajectories/run/run_x").status_code == 501
    assert TestClient(create_app()).get("/api/v1/health").status_code == 503
    sample = TestClient(create_app(example_mode=True))
    assert sample.get("/api/v1/sessions").headers["x-data-source"] == "example"
    assert empty.get("/api/v1/sessions").headers["x-data-source"] == "persisted"


def test_symlink_and_future_schema_fail_closed(tmp_path):
    outside = tmp_path / "outside"
    path = seed(outside)
    root = tmp_path / "root"
    root.mkdir()
    (root / "ses_demo.evidence.db").symlink_to(path)
    assert TestClient(create_app(evidence_dir=root)).get("/api/v1/system/stats").status_code == 503
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA user_version=999")
    assert TestClient(create_app(evidence_dir=outside)).get("/api/v1/system/stats").status_code == 503


def test_offline_demo_uses_actual_gateway_and_independent_verification(tmp_path):
    from persistence.http_api.demo import build_demo
    demo = build_demo(tmp_path)
    client = TestClient(create_app(evidence_dir=Path(demo["evidence_dir"])))
    assert len(client.get("/api/v1/sessions").json()["items"]) == 2
    verification = client.get(f"/api/v1/sessions/{demo['allowed_session']}/verification").json()
    assert verification["verification_status"] == "VERIFIED_SUCCESS"
    blocked = client.get(f"/api/v1/trajectories/session/{demo['blocked_session']}").json()
    assert any(step["decision"] == "BLOCK" for step in blocked["segments"][0]["steps"])
