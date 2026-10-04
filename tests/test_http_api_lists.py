"""Persisted list contracts and request-wide, privacy-safe scan bounds."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import sqlite3

from fastapi.testclient import TestClient
import pytest

from persistence.http_api import create_app
from persistence.query import QueryError, ReadQueries
import persistence.query as query_module
from test_http_api_persisted import seed


def signal(path, ident, *, ts="2026-10-04T10:00:01Z", applied=True, action="BLOCK_TOOLS", reason="RISK_HIGH"):
    session = path.name.removesuffix(".evidence.db")
    data = {"signal_id": ident, "session_id": session, "ts": ts, "action": action,
            "ttl_seconds": 3600, "source_plugin": "trajectory-risk", "trigger_event_id": f"evt_{session}_denied",
            "reason": reason, "policy_modifications": {"blocked_tools": ["read_application"]}}
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO policy_signals(signal_id,session_id,signal_json,applied) VALUES(?,?,?,?)",
                     (ident, session, json.dumps(data), int(applied)))


def assert_error(call, status, code):
    with pytest.raises(QueryError) as error:
        call()
    assert (error.value.status, error.value.code) == (status, code)


def test_actions_http_filters_intents_and_privacy(tmp_path):
    seed(tmp_path)
    client = TestClient(create_app(evidence_dir=tmp_path))
    response = client.get("/api/v1/actions")
    assert response.status_code == 200
    assert response.headers["x-data-source"] == "persisted"
    assert len(response.json()["items"]) == 3
    selected = client.get("/api/v1/actions", params={"session_id": "ses_demo", "run_id": "run_ses_demo",
        "case_id": "APP-0001", "agent_id": "onboarding-agent", "action_id": "act_denied", "name": "read_application",
        "kinds": "tool_use", "statuses": "blocked", "decisions": "BLOCK"}).json()["items"]
    assert [row["event_id"] for row in selected] == ["evt_ses_demo_denied"]
    assert selected[0]["max_severity"] == "high"
    all_rows = client.get("/api/v1/actions?include_intents=true&action_id=act_allowed").json()["items"]
    assert {row["status"] for row in all_rows} == {"completed", "pending"}
    assert next(row for row in all_rows if row["status"] == "pending")["seq"] is None
    assert client.get("/api/v1/actions?statuses=pending").json()["items"] == []
    raw = json.dumps(all_rows)
    assert "private marker" not in raw and "private objective" not in raw


def test_actions_side_effects_and_microsecond_bounds(tmp_path):
    path = seed(tmp_path)
    with sqlite3.connect(path) as conn:
        data = json.loads(conn.execute("SELECT payload_json FROM events WHERE event_id='evt_ses_demo_allowed'").fetchone()[0])
        data["action_details"]["side_effect"] = "write"
        conn.execute("UPDATE events SET payload_json=? WHERE event_id='evt_ses_demo_allowed'", (json.dumps(data),))
    queries = ReadQueries(tmp_path)
    rows = queries.actions(side_effects=["write"], since="2026-10-04T12:00:01+02:00", until="2026-10-04T10:00:02Z")["items"]
    assert [row["event_id"] for row in rows] == ["evt_ses_demo_allowed"]
    assert queries.actions(side_effects=["write"], since="2026-10-04T10:00:01.000001Z")["items"] == []


def test_equal_timestamp_order_uses_store_then_insertion_not_event_id(tmp_path):
    for session in ("ses_b", "ses_a"):
        path = seed(tmp_path, session)
        with sqlite3.connect(path) as conn:
            for row in conn.execute("SELECT event_id,payload_json FROM events").fetchall():
                data = json.loads(row[1])
                data["ts"] = "2026-10-04T10:00:00Z"
                conn.execute("UPDATE events SET payload_json=? WHERE event_id=?", (json.dumps(data), row[0]))
    queries = ReadQueries(tmp_path)
    expected = queries.actions()["items"]
    assert [row["session_id"] for row in expected] == ["ses_a"] * 3 + ["ses_b"] * 3
    assert expected[0]["kind"] == "session"
    cursor, actual = None, []
    while True:
        page = queries.actions(limit=1, cursor=cursor)
        actual.extend(page["items"])
        cursor = page["next_cursor"]
        if not page["has_more"]:
            break
    assert actual == expected


def test_actions_cursor_filter_binding_and_pruned_anchor(tmp_path):
    path = seed(tmp_path)
    queries = ReadQueries(tmp_path)
    first = queries.actions(limit=1)
    for change in ({"include_intents": True}, {"statuses": ["completed"]}, {"session_id": "ses_demo"}):
        assert_error(lambda: queries.actions(cursor=first["next_cursor"], **change), 400, "invalid_cursor")
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM events WHERE event_id=?", (first["items"][0]["event_id"],))
    assert_error(lambda: queries.actions(cursor=first["next_cursor"]), 400, "invalid_cursor")


def test_interventions_filters_privacy_application_and_single_clock(tmp_path, monkeypatch):
    path = seed(tmp_path)
    signal(path, "sig_z", reason="PRIVATE free text must not be returned")
    signal(path, "sig_a", applied=False, action="ALERT")
    signal(path, "sig_old", ts="2026-10-03T10:00:00Z")
    calls = []
    class FrozenClock(datetime):
        @classmethod
        def now(cls, tz=None):
            calls.append(tz)
            return datetime(2026, 10, 4, 10, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(query_module, "datetime", FrozenClock)
    queries = ReadQueries(tmp_path)
    rows = queries.interventions()["items"]
    assert len(calls) == 1
    assert [row["signal_id"] for row in rows] == ["sig_old", "sig_z", "sig_a"]
    assert rows[1]["reason"] is None
    assert [row["signal_id"] for row in queries.interventions(active=True, applied=True, source_plugin="trajectory-risk",
        actions=["BLOCK_TOOLS"], since="2026-10-04T10:00:01Z", until="2026-10-04T10:00:02Z")["items"]] == ["sig_z"]
    assert {row["signal_id"] for row in queries.interventions(active=False)["items"]} == {"sig_old", "sig_a"}
    assert queries.interventions(applied=False)["items"][0]["signal_id"] == "sig_a"
    first = queries.interventions(limit=1)
    second = queries.interventions(limit=1, cursor=first["next_cursor"])
    assert second["items"][0]["signal_id"] == "sig_z"
    assert_error(lambda: queries.interventions(cursor=first["next_cursor"], applied=True), 400, "invalid_cursor")


def test_interventions_http_and_application_link(tmp_path):
    path = seed(tmp_path)
    signal(path, "sig_applied")
    with sqlite3.connect(path) as conn:
        data = json.loads(conn.execute("SELECT payload_json FROM events WHERE event_id='evt_ses_demo_allowed'").fetchone()[0])
        data["action_type"] = "CONTROL"
        data["action_details"]["wire_details"] = {"change": "adjustment_applied", "signal_id": "sig_applied"}
        conn.execute("UPDATE events SET payload_json=? WHERE event_id='evt_ses_demo_allowed'", (json.dumps(data),))
    response = TestClient(create_app(evidence_dir=tmp_path)).get("/api/v1/interventions?applied=true")
    assert response.status_code == 200 and response.headers["x-data-source"] == "persisted"
    assert response.json()["items"][0]["applied_event_id"] == "evt_ses_demo_allowed"


@pytest.mark.parametrize("method", ["actions", "interventions"])
def test_empty_unknown_expired_and_relevant_path_only(tmp_path, method):
    queries = ReadQueries(tmp_path)
    call = getattr(queries, method)
    assert call()["items"] == []
    assert_error(lambda: call(session_id="unknown"), 404, "not_found")
    path = seed(tmp_path)
    (tmp_path / "ses_corrupt.evidence.db").write_text("invalid sqlite")
    assert len(call(session_id="ses_demo")["items"]) == (0 if method == "interventions" else 3)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE audit_runs SET lifecycle='EXPIRED'")
    assert_error(lambda: call(session_id="ses_demo"), 410, "evidence_expired")


def test_unrelated_expired_action_scope_is_skipped(tmp_path):
    path = seed(tmp_path, "ses_a")
    seed(tmp_path, "ses_b")
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE audit_runs SET lifecycle='EXPIRED'")
    queries = ReadQueries(tmp_path)
    assert len(queries.actions(run_id="run_ses_b")["items"]) == 3
    assert_error(queries.actions, 410, "evidence_expired")


@pytest.mark.parametrize("method", ["actions", "interventions", "sessions"])
def test_raw_row_limit_is_shared_across_tables_stores_and_concurrent_requests(tmp_path, monkeypatch, method):
    seed(tmp_path, "ses_a")
    seed(tmp_path, "ses_b")
    queries = ReadQueries(tmp_path)
    monkeypatch.setattr(query_module, "MAX_ROWS", 12)
    assert_error(getattr(queries, method), 503, "store_unavailable")
    # One store fits; concurrent requests must not share mutable budget state.
    with ThreadPoolExecutor(max_workers=4) as pool:
        pages = list(pool.map(lambda _: queries.actions(session_id="ses_a"), range(8)))
    assert all(len(page["items"]) == 3 for page in pages)


def test_raw_byte_limit_is_shared_and_counts_discarded_content(tmp_path, monkeypatch):
    paths = [seed(tmp_path, session) for session in ("ses_a", "ses_b")]
    queries = ReadQueries(tmp_path)
    sizes = []
    for path in paths:
        with sqlite3.connect(path) as conn:
            data = json.loads(conn.execute("SELECT contract_json FROM task_contracts").fetchone()[0])
            data["objective"] = "SYNTHETIC_PRIVATE_PROSE " * 300
            conn.execute("UPDATE task_contracts SET contract_json=?", (json.dumps(data),))
        with queries.connect(path) as conn:
            queries._load_session(conn, path.name.removesuffix(".evidence.db"))
            sizes.append(conn._budget["bytes"])
    monkeypatch.setattr(query_module, "MAX_READ_BYTES", max(sizes) + 100)
    assert len(queries.actions(session_id="ses_a")["items"]) == 3
    assert_error(queries.actions, 503, "store_unavailable")


def test_unsupported_legacy_vocabulary_fails_closed_even_when_filtered_out(tmp_path):
    path = seed(tmp_path)
    with sqlite3.connect(path) as conn:
        data = json.loads(conn.execute("SELECT payload_json FROM events WHERE event_id='evt_ses_demo_allowed'").fetchone()[0])
        data["action_type"] = "GATEWAY_CHECK"
        conn.execute("UPDATE events SET payload_json=? WHERE event_id='evt_ses_demo_allowed'", (json.dumps(data),))
    queries = ReadQueries(tmp_path)
    assert_error(lambda: queries.actions(statuses=["blocked"]), 503, "store_unavailable")
    assert_error(queries.interventions, 503, "store_unavailable")


@pytest.mark.parametrize("filters", [{"kinds": ["TOOL_CALL"]}, {"statuses": ["EXECUTED"]},
    {"decisions": ["DENY"]}, {"side_effects": ["none"]}, {"since": "2026-10-04"}, {"name": "raw prose"}])
def test_action_filter_validation(tmp_path, filters):
    assert_error(lambda: ReadQueries(tmp_path).actions(**filters), 400, "invalid_filter")


def test_bundle_event_rowids_match_store_insertion_order(tmp_path):
    path = seed(tmp_path)
    bundles = list(ReadQueries(tmp_path)._iter_bundles("ses_demo"))
    assert len(bundles) == 1 and bundles[0][0] == path
    with sqlite3.connect(path) as conn:
        expected = dict(conn.execute("SELECT event_id,rowid FROM events"))
    assert bundles[0][1]["_rowids"] == expected


def test_duplicate_session_store_does_not_double_count_evidence(tmp_path):
    path = seed(tmp_path)
    (tmp_path / "duplicate.evidence.db").write_bytes(path.read_bytes())
    queries = ReadQueries(tmp_path)
    for call in (queries.actions, queries.interventions, queries.sessions,
                 lambda: queries.timeseries(metric="actions", bucket="1m",
                                           since="2026-10-04T10:00:00Z",
                                           until="2026-10-04T10:01:00Z")):
        assert_error(call, 503, "store_unavailable")
