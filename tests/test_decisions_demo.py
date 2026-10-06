"""Running scenarios: real gateway -> evidence store -> consume plane, then the decision trace via REST.

The same scenarios can be run and inspected by hand:
    uv run --locked python -m persistence.http_api.decisions_demo [--json] [--serve] [--judge]
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from persistence.http_api import create_app
from persistence.http_api.decisions_demo import SCENARIOS, build, render, trace


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    manifest = build(tmp_path_factory.mktemp("decision-scenarios"))
    traces = {name: trace(manifest["evidence_dir"], sid) for name, sid in manifest["sessions"].items()}
    return manifest, traces


def steps(body, plugin="trajectory-risk"):
    return [item for item in body["items"] if item["plugin"] == plugin]


def verdict(body):
    [item] = steps(body, "outcome-verifier")
    return item


def test_every_scenario_traces_each_step_once_without_failures(demo):
    manifest, traces = demo
    for name, body in traces.items():
        risk = steps(body)
        tool_steps = [item for item in risk if item["trigger"]["kind"] == "tool_use"]
        assert len({item["trigger_event_id"] for item in risk}) == len(risk) == len(tool_steps), name
        assert all(item["outcome"] == "decided" and item["reason"] is None for item in body["items"]), name
        assert all(item["reasoning"] and item["reasoning"] != "[OMITTED]" for item in body["items"]), name
        assert verdict(body)["trigger"]["kind"] == "session", name


def test_clean_run_never_escalates_before_the_irreversible_write(demo):
    _, traces = demo
    risk = steps(traces["clean"])
    raised = [item for item in risk if item["decision"] == "LEVEL_RAISED"]
    assert all(item["trigger"]["name"] == "create_client" for item in raised)
    assert all(item["factors"]["level"] in ("low", "medium") for item in risk)
    assert all(item["adjustments"] == [] for item in risk)                       # never asks the gateway to tighten
    assert all(item["finding_ids"] for item in raised)
    assert verdict(traces["clean"])["decision"] == "VERIFIED_SUCCESS"


def test_clean_run_has_no_out_of_scope_signal(demo):
    _, traces = demo
    assert all("out_of_scope_target" not in item["factors"]["signals"] for item in steps(traces["clean"]))


def test_skip_screening_is_stopped_by_the_gateway_and_left_incomplete(demo):
    _, traces = demo
    create = next(item for item in steps(traces["skip-screening"]) if item["trigger"]["name"] == "create_client")
    assert create["trigger"]["decision"] == "BLOCK" and create["decision"] == "NO_CHANGE"
    assert create["factors"]["signals"]["gateway_blocked"] == 1
    result = verdict(traces["skip-screening"])
    assert result["decision"] == "VERIFICATION_INCOMPLETE"
    assert "KYC-CLIENT-COUNT" in result["factors"]["incomplete"]


def test_duplicate_create_second_attempt_is_blocked_and_traced(demo):
    _, traces = demo
    creates = [item for item in steps(traces["duplicate-create"]) if item["trigger"]["name"] == "create_client"]
    assert [item["trigger"]["decision"] for item in creates] == ["ALLOW", "BLOCK"]
    assert [item["decision"] for item in creates] == ["NO_CHANGE", "NO_CHANGE"]
    assert verdict(traces["duplicate-create"])["decision"] == "VERIFIED_SUCCESS"


def test_out_of_scope_read_raises_risk_without_false_document_signals(demo):
    manifest, traces = demo
    risk = steps(traces["out-of-scope"])
    blocked = next(item for item in risk if item["trigger"]["decision"] == "BLOCK")
    assert blocked["factors"]["signals"]["out_of_scope_target"] == 1
    raised = next(item for item in risk if item["factors"]["level"] == "medium")
    assert raised["decision"] == "LEVEL_RAISED"
    assert risk[-1]["factors"]["signals"]["out_of_scope_target"] == 1
    assert all(item["adjustments"] == [] for item in risk)
    client = TestClient(create_app(evidence_dir=Path(manifest["evidence_dir"])))
    detail = client.get(f"/api/v1/sessions/{manifest['sessions']['out-of-scope']}").json()
    assert detail["verification"]["verification_status"] == "VERIFIED_SUCCESS"
    one = client.get(f"/api/v1/decisions/{raised['decision_id']}").json()
    assert one["finding_ids"] == raised["finding_ids"]


def test_rest_and_inspector_show_the_same_trace(demo):
    manifest, traces = demo
    client = TestClient(create_app(evidence_dir=Path(manifest["evidence_dir"])))
    for name, sid in manifest["sessions"].items():
        rest = client.get(f"/api/v1/sessions/{sid}/decisions", params={"limit": 1000}).json()
        assert [i["decision_id"] for i in rest["items"]] == [i["decision_id"] for i in traces[name]["items"]]
    table = render(traces["out-of-scope"])
    assert "LEVEL_RAISED" in table and table.splitlines()[-1].startswith("summary:")


def test_scenario_catalog_is_runnable():
    assert set(SCENARIOS) == {"clean", "skip-screening", "duplicate-create", "out-of-scope"}
