"""Contract checks for the read API stub (docs/rest.md): routes, auth, errors, pagination."""
import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from persistence.http_api import create_app

ORIGIN = "http://localhost:5173"
client = TestClient(create_app(frozenset({ORIGIN})))


def get(path, method="GET"):
    response = client.request(method, path)
    if response.headers["content-type"].startswith("application/x-ndjson"):
        return response.status_code, response.text
    return response.status_code, response.json()


@pytest.mark.parametrize("path, keys", [
    ("/api/v1/sessions", {"items", "next_cursor", "has_more"}),
    ("/api/v1/sessions/sess_a", {"session_id", "contract", "run", "verification", "active_interventions"}),
    ("/api/v1/sessions/sess_a/usage", {"session_id", "usage", "budgets", "tool_calls", "model_calls"}),
    ("/api/v1/sessions/sess_a/verification", {"session_id", "verification_status", "checks", "verified_at"}),
    ("/api/v1/trajectories/session/sess_a", {"scope", "ordering", "segments", "totals", "risk"}),
    ("/api/v1/trajectories/run/run_1?view=full", {"scope", "ordering", "segments"}),
    ("/api/v1/actions", {"items"}),
    ("/api/v1/actions/evt_sess_a_0007", {"summary", "event", "context", "related", "detections"}),
    ("/api/v1/detections", {"items"}),
    ("/api/v1/detections/fnd_x", {"detection_id", "name", "ts", "reason", "severity", "source"}),
    ("/api/v1/catalog/detections", {"catalog_version", "entries"}),
    ("/api/v1/metrics/usage?group_by=day", {"group_by", "totals", "buckets"}),
    ("/api/v1/metrics/security", {"actions", "detections", "block_rate", "interception_overhead_ms"}),
    ("/api/v1/metrics/timeseries?metric=blocked&bucket=1m", {"metric", "points"}),
    ("/api/v1/interventions", {"items"}),
    ("/api/v1/system/stats", {"total_events", "pending_deliveries"}),
])
def test_every_documented_endpoint_returns_its_model(path, keys):
    status, body = get(path)
    assert status == 200
    assert keys <= set(body)


def test_detection_events_carry_name_ts_reason():
    _, body = get("/api/v1/detections?order=asc")
    times = [d["ts"] for d in body["items"]]
    assert times == sorted(times)
    for d in body["items"]:
        assert {"name", "ts", "reason", "severity", "source", "session_id"} <= set(d)


def test_trajectory_is_ordered_by_seq_and_filters_apply():
    _, body = get("/api/v1/trajectories/session/sess_a?kinds=tool_use&statuses=blocked")
    steps = body["segments"][0]["steps"]
    assert [s["name"] for s in steps] == ["run_code"]
    _, body = get("/api/v1/trajectories/session/sess_a")
    seqs = [s["seq"] for s in body["segments"][0]["steps"]]
    assert seqs == sorted(seqs)


def test_no_authentication_is_required_and_credentials_are_ignored():
    assert get("/api/v1/health")[0] == 200
    assert client.get("/api/v1/sessions", headers={"Authorization": "Bearer anything"}).status_code == 200


@pytest.mark.parametrize("path, status, code", [
    ("/api/v1/nope", 404, "not_found"),
    ("/api/v1/trajectories/planet/x", 404, "not_found"),
    ("/api/v1/actions?kinds=teleport", 400, "invalid_filter"),
    ("/api/v1/sessions?limit=0", 400, "bad_request"),
    ("/api/v1/sessions?cursor=garbage", 400, "invalid_cursor"),
    ("/api/v1/detections?since=2026-10-03T10:00:00", 400, "bad_request"),
    ("/api/v1/metrics/timeseries", 400, "bad_request"),
    ("/api/v1/trajectories/run/r1?from_seq=2", 400, "bad_request"),
])
def test_errors_use_the_documented_envelope(path, status, code):
    got, body = get(path)
    assert got == status
    assert body["error"]["code"] == code and isinstance(body["error"]["message"], str)


def test_writes_are_rejected():
    status, body = get("/api/v1/sessions", method="POST")
    assert status == 405 and body["error"]["code"] == "method_not_allowed"


def test_cursor_pagination_walks_all_items_once():
    seen, path = [], "/api/v1/actions?limit=3"
    while True:
        _, body = get(path)
        seen += [a["event_id"] for a in body["items"]]
        if not body["has_more"]:
            break
        path = f"/api/v1/actions?limit=3&cursor={body['next_cursor']}"
    assert len(seen) == len(set(seen)) == 10


def test_export_is_ndjson_with_verifiable_footer():
    status, body = get("/api/v1/export/sessions/sess_a")
    assert status == 200
    lines = body.splitlines()
    footer = json.loads(lines[-1])
    assert footer["record_type"] == "export_footer" and footer["rows"] == len(lines) - 1
    assert footer["sha256"] == hashlib.sha256("".join(l + "\n" for l in lines[:-1]).encode()).hexdigest()
    assert json.loads(lines[0])["record_type"] == "session"


def test_cors_preflight_and_headers_for_allowed_origin_only():
    pre = client.options("/api/v1/sessions", headers={"Origin": ORIGIN, "Access-Control-Request-Method": "GET"})
    assert pre.status_code == 200 and pre.headers["access-control-allow-origin"] == ORIGIN
    ok = client.get("/api/v1/sessions", headers={"Origin": ORIGIN})
    assert ok.headers["access-control-allow-origin"] == ORIGIN
    other = client.get("/api/v1/sessions", headers={"Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in other.headers


def test_openapi_lists_every_documented_route():
    paths = set(client.get("/api/v1/openapi.json").json()["paths"])
    assert len([p for p in paths if p.startswith("/api/v1/")]) == 20
    assert {"/api/v1/configs", "/api/v1/configs/{name}", "/api/v1/config-selection"} <= paths


def test_stats_count_the_real_per_session_evidence_stores(tmp_path):
    (tmp_path / "ses_a.evidence.db").touch()
    (tmp_path / "ses_a.evidence.ledger.db").touch()
    (tmp_path / "ses_a.bank.db").touch()
    local = TestClient(create_app(evidence_dir=tmp_path))
    assert local.get("/api/v1/system/stats").json()["evidence_stores"] == 1
    assert get("/api/v1/system/stats")[1]["evidence_stores"] is None
