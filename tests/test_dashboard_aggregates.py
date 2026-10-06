"""Persisted dashboard totals, streaming exports, and backend step attribution."""
import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from persistence.http_api import create_app
from persistence.http_api.demo import build_demo
from persistence.query import ReadQueries
from persistence.settings import PersistenceSettings
from persistence.store import EventStore
from test_http_api_persisted import seed


def test_aggregate_totals_are_not_limited_by_action_page(tmp_path):
    seed(tmp_path, "ses_a")
    seed(tmp_path, "ses_b")
    client = TestClient(create_app(evidence_dir=tmp_path))
    assert len(client.get("/api/v1/actions?limit=1").json()["items"]) == 1
    security = client.get("/api/v1/metrics/security").json()
    assert security["actions"]["total"] == 6
    assert security["actions"]["by_decision"] == {"ALLOW": 2, "BLOCK": 2, "REDACT": 0, "REQUIRE_APPROVAL": 0, "ALERT": 0}
    assert security["detections"]["total"] == 2
    assert security["verification"]["VERIFIED_SUCCESS"] == 2
    perf = client.get("/api/v1/metrics/performance").json()
    assert perf["actions_evaluated"] == 4
    assert next(a for a in perf["by_auditor"] if a["auditor"] == "pattern-match")["runs"] == 2
    assert perf["backend_latency_ms"]["p95"] is None
    usage = client.get("/api/v1/metrics/usage?group_by=session&limit=1").json()
    assert usage["has_more"] and usage["total_buckets"] == 2
    assert usage["totals"]["cost_usd"] is None
    assert len(usage["buckets"]) == 1


def test_metrics_share_precise_time_filters_and_agent_scope(tmp_path):
    seed(tmp_path, "ses_a")
    seed(tmp_path, "ses_b")
    client = TestClient(create_app(evidence_dir=tmp_path))
    params = {"since": "2026-10-04T10:00:01.000001Z", "until": "2026-10-04T10:00:02.000001Z"}
    security = client.get("/api/v1/metrics/security", params=params).json()
    assert security["actions"]["total"] == 2 and security["actions"]["by_decision"]["BLOCK"] == 2
    assert client.get("/api/v1/metrics/performance", params=params).json()["actions_evaluated"] == 2
    for route in ("security", "usage", "performance"):
        response = client.get(f"/api/v1/metrics/{route}", params={"agent_id": "other-agent"})
        assert response.status_code == 200
    assert client.get("/api/v1/metrics/security?agent_id=other-agent").json()["actions"]["total"] == 0


def test_detections_lookup_catalog_pagination_and_all_sources(tmp_path):
    path = seed(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE verification_results SET payload_json=?", (json.dumps({
            "verification_status": "FAILED_POSTCONDITIONS", "verified_at": "2026-10-04T10:00:03Z", "checks": []}),))
    client = TestClient(create_app(evidence_dir=tmp_path))
    first = client.get("/api/v1/detections?limit=1").json()
    second = client.get("/api/v1/detections", params={"limit": 1, "cursor": first["next_cursor"]}).json()
    assert first["has_more"] and not second["has_more"]
    assert {first["items"][0]["source"], second["items"][0]["source"]} == {"gateway", "verification"}
    ident = first["items"][0]["detection_id"]
    assert client.get(f"/api/v1/detections/{ident}").json()["detection_id"] == ident
    assert client.get("/api/v1/detections/unknown").status_code == 404
    assert len(client.get("/api/v1/detections?sources=gateway").json()["items"]) == 1
    catalog = client.get("/api/v1/catalog/detections").json()
    assert catalog["catalog_version"] == "persisted-v1" and len(catalog["entries"]) == 2
    changed = client.get("/api/v1/detections", params={"cursor": first["next_cursor"], "sources": "gateway"})
    assert changed.status_code == 400


def test_risk_attribution_and_control_metrics_use_persisted_backend_assessment(tmp_path):
    manifest = build_demo(tmp_path)
    client = TestClient(create_app(evidence_dir=Path(manifest["evidence_dir"])))
    session = manifest["allowed_session"]
    trace = client.get(f"/api/v1/sessions/{session}/decisions?plugins=trajectory-risk").json()["items"]
    risk = client.get(f"/api/v1/sessions/{session}").json()["risk"]
    assert risk["expected_loss"] == trace[-1]["factors"]["expected_loss"]
    assert risk["failure_probability"] == trace[-1]["factors"]["failure_probability"]
    timeline = client.get(f"/api/v1/trajectories/session/{session}?view=full&kinds=tool_use&limit=1").json()
    step = timeline["segments"][0]["steps"][0]
    decision = next(d for d in trace if d["trigger_event_id"] == step["summary"]["event_id"])
    assert step["risk"]["consequence"] == decision["factors"]["step_consequence"]
    assert step["risk"]["probability"] == decision["factors"]["step_probability"]
    assert step["risk"]["signals"] == decision["factors"]["step_signals"]
    assert step["trace"] and timeline["has_more"]
    perf = client.get("/api/v1/metrics/performance", params={"session_id": session}).json()
    plugin = next(a for a in perf["by_auditor"] if a["auditor"] == "trajectory-risk")
    assert plugin["plane"] == "control_plane" and plugin["runs"] == len(trace)
    assert plugin["p95"] == sorted(d["duration_ms"] for d in trace)[-1]


def test_older_sessions_have_unknown_risk_and_missing_measurements(tmp_path):
    seed(tmp_path)
    client = TestClient(create_app(evidence_dir=tmp_path))
    session = client.get("/api/v1/sessions").json()["items"][0]
    assert session["risk"] is None
    action = client.get("/api/v1/actions/evt_ses_demo_allowed").json()
    assert action["risk"] is None
    assert action["summary"]["usage"]["latency_ms"] is None
    assert action["summary"]["usage"]["input_tokens"] is None


def test_all_action_export_streams_filtered_evidence_and_integrity_footer(tmp_path):
    seed(tmp_path, "ses_a")
    seed(tmp_path, "ses_b")
    client = TestClient(create_app(evidence_dir=tmp_path))
    response = client.get("/api/v1/export/actions?kinds=tool_use&decisions=BLOCK")
    assert response.status_code == 200 and "attachment" in response.headers["content-disposition"]
    lines = response.content.splitlines(keepends=True)
    records = [json.loads(line) for line in lines]
    assert len(records) == 3 and records[-1]["rows"] == 2
    assert all(r["interception_metadata"]["final_decision"] == "BLOCK" for r in records[:-1])
    assert records[-1]["sha256"] == hashlib.sha256(b"".join(lines[:-1])).hexdigest()
    assert "private marker" not in response.text
    output = ReadQueries(tmp_path).export_actions(kinds=["tool_use"])
    try:
        assert output.read(10)
    finally:
        output.close()


def test_export_agent_filter_matches_individual_events_in_mixed_agent_session(tmp_path):
    path = seed(tmp_path)
    with sqlite3.connect(path) as conn:
        event_id = "evt_ses_demo_denied"
        payload = json.loads(conn.execute("SELECT payload_json FROM events WHERE event_id=?", (event_id,)).fetchone()[0])
        payload["agent_id"] = "secondary-agent"
        conn.execute("UPDATE events SET agent_id=?, payload_json=? WHERE event_id=?",
                     ("secondary-agent", json.dumps(payload), event_id))
    client = TestClient(create_app(evidence_dir=tmp_path))
    for agent, count in (("onboarding-agent", 2), ("secondary-agent", 1), ("absent-agent", 0)):
        response = client.get("/api/v1/export/actions", params={"agent_id": agent})
        assert response.status_code == 200
        lines = response.content.splitlines(keepends=True)
        records = [json.loads(line) for line in lines]
        assert records[-1]["rows"] == count
        assert all(record["agent_id"] == agent for record in records[:-1])
        assert records[-1]["sha256"] == hashlib.sha256(b"".join(lines[:-1])).hexdigest()


def test_export_preflight_returns_envelope_for_quota_and_expiration(tmp_path):
    path = seed(tmp_path, settings=PersistenceSettings(max_export_rows=1))
    client = TestClient(create_app(evidence_dir=tmp_path))
    response = client.get("/api/v1/export/actions")
    assert response.status_code == 413 and response.json()["error"]["code"] == "export_quota_exceeded"
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE audit_runs SET lifecycle='EXPIRED'")
    assert client.get("/api/v1/export/actions").status_code == 410
    assert client.get("/api/v1/metrics/security").status_code == 410


def test_control_failures_and_durations_count_real_records(tmp_path):
    path = seed(tmp_path)
    from test_decision_trace_api import BASE
    async def write():
        store = EventStore(path)
        await store.initialize()
        await store.write_plugin_decision({**BASE, "session_id": "ses_demo", "run_id": "run_ses_demo",
                                           "trigger_event_id": "evt_ses_demo_allowed", "outcome": "failed",
                                           "reason": "TIMEOUT", "duration_ms": 23})
        await store.close()
    asyncio.run(write())
    perf = TestClient(create_app(evidence_dir=tmp_path)).get("/api/v1/metrics/performance").json()
    plugin = next(a for a in perf["by_auditor"] if a["auditor"] == "trajectory-risk")
    assert plugin["runs"] == 1 and plugin["failed"] == 1 and plugin["p95"] == 23
    assert plugin["acted"] == 0


def test_evidence_api_disables_config_mutators_by_default(tmp_path):
    client = TestClient(create_app(evidence_dir=tmp_path))
    response = client.put("/api/v1/config-selection", json={"name": "policy"})
    assert response.status_code == 405


def test_cross_origin_reads_and_explicit_management_headers(tmp_path):
    origin = "http://localhost:5173"
    headers = {"Origin": origin, "Access-Control-Request-Method": "PUT",
               "Access-Control-Request-Headers": "X-Admin-Token,Content-Type"}
    read_only = TestClient(create_app(frozenset({origin}), evidence_dir=tmp_path))
    assert read_only.options("/api/v1/config-selection", headers=headers).status_code == 400
    read = read_only.get("/api/v1/sessions", headers={"Origin": origin})
    assert read.headers["access-control-allow-origin"] == origin
    assert "X-Data-Source" in read.headers["access-control-expose-headers"]
    management = TestClient(create_app(frozenset({origin}), evidence_dir=tmp_path, config_writes=True))
    preflight = management.options("/api/v1/config-selection", headers=headers)
    assert preflight.status_code == 200 and "PUT" in preflight.headers["access-control-allow-methods"]


def test_missing_samples_do_not_discard_recorded_zero_measurements(tmp_path, monkeypatch):
    path = seed(tmp_path)
    with sqlite3.connect(path) as conn:
        row = conn.execute("SELECT payload_json FROM events WHERE event_id='evt_ses_demo_allowed'").fetchone()
        payload = json.loads(row[0])
        payload["context"]["actual_usage"] = {"latency_ms": 0, "input_tokens": 0, "output_tokens": 0}
        conn.execute("UPDATE events SET payload_json=? WHERE event_id='evt_ses_demo_allowed'", (json.dumps(payload),))
    queries = ReadQueries(tmp_path)
    original = queries._summary
    def summary(event):
        value = original(event)
        value["interception_overhead_ms"] = None if value["event_id"].endswith("allowed") else 0
        return value
    monkeypatch.setattr(queries, "_summary", summary)
    perf = queries.performance_overview(since=None, until=None)
    security = queries.security_overview(since=None, until=None)
    assert perf["backend_latency_ms"]["p95"] == 0
    assert perf["interception_overhead_ms"]["p95"] == 0
    assert security["interception_overhead_ms"]["p95"] == 0
    assert perf["overhead_share"] is None
    measured = queries.action("evt_ses_demo_allowed")["summary"]["usage"]
    assert measured["input_tokens"] == measured["output_tokens"] == measured["latency_ms"] == 0


def test_usage_aggregation_counts_resolved_model_usage_and_excludes_rejections(tmp_path):
    from contracts import TaskContract
    from persistence.events import build_action_event
    from persistence.governed import GovernedPersistence
    path = seed(tmp_path)
    with sqlite3.connect(path) as conn:
        contract = TaskContract.from_dict(json.loads(conn.execute("SELECT contract_json FROM task_contracts").fetchone()[0]))
    async def write():
        store = EventStore(path)
        await store.initialize()
        boundary = GovernedPersistence(store)
        await boundary.append(build_action_event(contract=contract, action_type="llm_call", action_id="model_done",
            name="local-model", status="ALLOW", actual_usage={"input_tokens": 10, "output_tokens": 20, "latency_ms": 0}))
        await boundary.append(build_action_event(contract=contract, action_type="llm_call", action_id="model_rejected",
            name="local-model", status="BLOCK", actual_usage={"input_tokens": 10000, "output_tokens": 10000}))
        await store.close()
    asyncio.run(write())
    client = TestClient(create_app(evidence_dir=tmp_path))
    report = client.get("/api/v1/metrics/usage?group_by=model").json()
    assert report["totals"]["input_tokens"] == 10 and report["totals"]["output_tokens"] == 20
    assert report["totals"]["total_tokens"] == 30 and report["totals"]["latency_ms"] == 0
    assert report["totals"]["cost_usd"] is None
    assert report["buckets"][0]["model_calls"] == 2 and report["buckets"][0]["blocked"] == 1


def test_explicit_example_mode_projects_additive_risk_contract():
    client = TestClient(create_app(example_mode=True))
    response = client.get("/api/v1/sessions")
    assert response.headers["x-data-source"] == "example"
    session = response.json()["items"][0]
    assert session["risk"]["source"] == "example"
    assert "verification" in session and "active_interventions" in session
    timeline = client.get(f"/api/v1/trajectories/session/{session['session_id']}?view=full&kinds=tool_use").json()
    if timeline["segments"][0]["steps"]:
        assert timeline["segments"][0]["steps"][0]["risk"]["source"] == "example"
