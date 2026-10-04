"""Decision trace persistence and REST: /sessions/{id}/decisions and /decisions/{id}."""
import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from persistence.http_api import create_app
from persistence.privacy import OMITTED, decision_projection
from persistence.store import EventStore

BASE = {"decision_id": "dec_1", "ts": "2026-10-04T10:00:00+00:00", "plugin": "trajectory-risk",
        "plugin_version": "1.0.0", "method": "deterministic", "outcome": "decided", "decision": "NO_CHANGE",
        "reasoning": "expected loss 0.12 -> level low (was low); P(failure) 0.02", "reason": None,
        "factors": {"expected_loss": 0.12, "level": "low", "signals": {"tool_error": 1}},
        "session_id": "sess_1", "run_id": "run_1", "agent_id": "onboarding-agent", "case_id": "APP-0001",
        "trigger_event_id": "evt_1", "trigger_seq": 1, "attempt": 1, "duration_ms": 0.8,
        "finding_ids": [], "adjustments": []}


# --- privacy projection ------------------------------------------------------------------------

@pytest.mark.parametrize("reasoning", [
    "applicant Jan Kowalski <jan@example.com> looks fine",   # e-mail
    "PESEL 44051401359 matched",                            # long digit run
    "born 1990-01-01",                                      # date
    "token=abc123 leaked",                                  # secret marker
    "ünïcode prose",                                        # characters outside the allowlist
    "x" * 241,                                              # too long
])
def test_reasoning_that_could_carry_content_is_omitted(reasoning):
    assert decision_projection({**BASE, "reasoning": reasoning})["reasoning"] == OMITTED


def test_projection_keeps_codes_and_numbers_and_drops_bad_factors():
    safe = decision_projection({**BASE, "factors": {**BASE["factors"], "name": "Jan Kowalski",
                                                   "nested": {"deep": {"x": 1}}, "ok_list": ["A", "B"]}})
    assert safe["reasoning"] == BASE["reasoning"]
    assert safe["factors"] == {"expected_loss": 0.12, "level": "low", "signals": {"tool_error": 1},
                               "ok_list": ["A", "B"]}
    assert safe["ts"] == "2026-10-04T10:00:00.000000Z"


def test_failure_reason_is_required_exactly_for_failures():
    with pytest.raises(ValueError):
        decision_projection({**BASE, "outcome": "failed", "reason": None})
    with pytest.raises(ValueError):
        decision_projection({**BASE, "reason": "ValueError"})
    with pytest.raises(ValueError):  # reason must be a code, not a message
        decision_projection({**BASE, "outcome": "failed", "reason": "ValueError: Jan Kowalski"})
    assert decision_projection({**BASE, "outcome": "failed", "reason": "TIMEOUT"})["reason"] == "TIMEOUT"


def test_store_is_idempotent_first_write_wins(tmp_path):
    async def go():
        store = EventStore(tmp_path / "s.db")
        await store.initialize()
        first = await store.write_plugin_decision(BASE)
        again = await store.write_plugin_decision({**BASE, "ts": "2026-10-04T11:00:00Z", "duration_ms": 9})
        rows = await store.list_plugin_decisions("sess_1")
        await store.close()
        return first, again, rows
    first, again, rows = asyncio.run(go())
    assert (first, again, len(rows)) == (True, False, 1) and rows[0]["duration_ms"] == 0.8


# --- REST over real evidence --------------------------------------------------------------------

@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    from persistence.http_api.demo import build_demo
    manifest = build_demo(tmp_path_factory.mktemp("decisions"))
    return manifest, TestClient(create_app(evidence_dir=Path(manifest["evidence_dir"])))


def test_session_trace_explains_each_plugin_decision(demo):
    manifest, client = demo
    body = client.get(f"/api/v1/sessions/{manifest['allowed_session']}/decisions").json()
    items = body["items"]
    assert {item["plugin"] for item in items} >= {"trajectory-risk", "outcome-verifier"}
    assert [item["ts"] for item in items] == sorted(item["ts"] for item in items)
    risk = next(item for item in items if item["plugin"] == "trajectory-risk")
    assert risk["outcome"] == "decided" and risk["reason"] is None
    assert risk["reasoning"].startswith("expected loss") and "level" in risk["factors"]
    assert risk["trigger"]["event_id"] == risk["trigger_event_id"] and risk["trigger"]["kind"] == "tool_use"
    verdict = next(item for item in items if item["plugin"] == "outcome-verifier")
    assert verdict["decision"] == "VERIFIED_SUCCESS" and "checks passed" in verdict["reasoning"]
    assert verdict["findings"] and verdict["findings"][0]["finding_id"] in verdict["finding_ids"]
    assert {"plugin": "outcome-verifier", "outcome": "decided", "count": 1} in body["summary"]


def test_filters_pagination_and_single_lookup(demo):
    manifest, client = demo
    url = f"/api/v1/sessions/{manifest['allowed_session']}/decisions"
    only = client.get(url, params={"plugins": "outcome-verifier"}).json()["items"]
    assert [item["plugin"] for item in only] == ["outcome-verifier"]
    assert client.get(url, params={"outcomes": "failed"}).json()["items"] == []
    assert client.get(url, params={"outcomes": "bogus"}).status_code == 400
    first = client.get(url, params={"limit": 1}).json()
    assert first["has_more"] and len(first["items"]) == 1
    second = client.get(url, params={"limit": 1, "cursor": first["next_cursor"]}).json()
    assert second["items"][0]["decision_id"] != first["items"][0]["decision_id"]

    one = client.get(f"/api/v1/decisions/{only[0]['decision_id']}").json()
    assert one["decision_id"] == only[0]["decision_id"] and one["trigger"]["kind"] == "session"
    assert client.get("/api/v1/decisions/dec_unknown").status_code == 404
    assert client.get("/api/v1/sessions/nope/decisions").status_code == 404


def test_export_includes_the_decision_trace(demo):
    manifest, client = demo
    lines = [json.loads(line) for line in
             client.get(f"/api/v1/export/sessions/{manifest['allowed_session']}").text.splitlines()]
    traced = [r for r in lines if r["record_type"] == "plugin_decision"]
    assert traced and all("reasoning" in r and "decision" in r for r in traced)


def test_store_without_decision_table_reads_as_empty(tmp_path):
    from test_http_api_persisted import seed
    path = seed(tmp_path / "old")
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE plugin_decisions")
    client = TestClient(create_app(evidence_dir=path.parent))
    response = client.get("/api/v1/sessions/ses_demo/decisions")
    assert response.status_code == 200 and response.json()["items"] == []


def test_read_side_reprojects_a_tampered_row(demo, tmp_path):
    manifest, _ = demo
    import shutil
    target = tmp_path / "copy"
    shutil.copytree(manifest["evidence_dir"], target)
    path = target / f"{manifest['allowed_session']}.evidence.db"
    with sqlite3.connect(path) as conn:
        row = conn.execute("SELECT decision_id,payload_json FROM plugin_decisions LIMIT 1").fetchone()
        payload = json.loads(row[1])
        payload["reasoning"] = "client Jan Kowalski jan@example.com"
        conn.execute("UPDATE plugin_decisions SET payload_json=? WHERE decision_id=?", (json.dumps(payload), row[0]))
    client = TestClient(create_app(evidence_dir=target))
    item = client.get(f"/api/v1/decisions/{row[0]}").json()
    assert item["reasoning"] == OMITTED
