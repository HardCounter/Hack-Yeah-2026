"""The dashboard's suite runner: lists the cases, runs all or one, reports each case's final decision."""
import shutil
import time

import pytest
from fastapi.testclient import TestClient

from web import suite
from web.main import app

client = TestClient(app)

CASES = '''
import pytest

@pytest.mark.area("PII")
def test_redacts_the_number(suite_trace):
    suite_trace.append({"kind": "tool", "name": "read_documents", "decision": "ALLOW", "reason": None})
    suite_trace.append({"kind": "tool", "name": "send_email", "decision": "REDACT", "reason": "PRIVACY_MATCH"})

@pytest.mark.area("LEG")
def test_clean_request_is_held(suite_trace):
    suite_trace.append({"kind": "prompt", "name": "llama3.2", "decision": "REQUIRE_APPROVAL", "reason": "VELOCITY_EXCEEDED"})
    assert "HELD" == "ALLOWED"  # a failing assertion does not change what is reported

def test_without_an_area():
    import json, os
    assert "OPENAI_API_KEY" not in os.environ  # the suite uses stub models and gets no keys
    assert json.load(open(os.environ["SUITE_POLICY"]))["name"] == "standard"
'''


@pytest.fixture
def fake_suite(monkeypatch, tmp_path):
    folder = tmp_path / "suite"
    folder.mkdir()
    shutil.copy(suite.SUITE_PATH / "conftest.py", folder / "conftest.py")
    (folder / "test_cases.py").write_text(CASES)
    monkeypatch.setattr(suite, "SUITE_PATH", folder)
    monkeypatch.setattr(suite, "_run", None)
    monkeypatch.setattr(suite, "_catalogue", None)
    monkeypatch.setattr(suite, "_results", {})
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-the-suite")
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "config"))
    return folder


def finished(deadline_s=60):
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        run = client.get("/api/suite/runs/latest").json()
        if run["status"] != "running":
            return run
        time.sleep(0.1)
    raise AssertionError("suite run did not finish")


def statuses(run):
    return {case["title"]: (case["area"], case["state"], case["status"]) for case in run["cases"]}


def test_cases_are_listed_before_any_run(fake_suite):
    listed = client.get("/api/suite/runs/latest").json()
    assert listed["status"] == "not_run"
    assert statuses(listed) == {"redacts the number": ("PII", "not_run", None), "clean request is held": ("LEG", "not_run", None),
                                "without an area": ("OTHER", "not_run", None)}


def test_run_reports_each_case_with_its_final_decision_and_trace(fake_suite):
    started = client.post("/api/suite/runs")
    assert started.status_code == 202 and started.json()["status"] in ("running", "finished")
    again = client.post("/api/suite/runs").json()  # a second click joins the run, it does not start another
    assert again["started_at"] == started.json()["started_at"]

    run = finished()
    assert run["status"] == "finished" and run["error"] is None and run["duration_ms"] >= 0
    assert run["config"]["name"] == "standard"  # the config selected in the dashboard
    assert statuses(run) == {"redacts the number": ("PII", "done", "REDACT"), "clean request is held": ("LEG", "done", "REQUIRE_APPROVAL"),
                             "without an area": ("OTHER", "done", None)}
    cases = {case["title"]: case for case in run["cases"]}
    assert [(step["name"], step["decision"], step["reason"]) for step in cases["redacts the number"]["steps"]] == [
        ("read_documents", "ALLOW", None), ("send_email", "REDACT", "PRIVACY_MATCH")]
    assert cases["without an area"]["steps"] == []
    assert not {"outcome", "detail", "expected"} & set(cases["clean request is held"])  # no pass/fail verdict


def test_one_case_runs_alone_and_keeps_the_other_results(fake_suite):
    client.post("/api/suite/runs")
    ids = {case["title"]: case["id"] for case in finished()["cases"]}
    (fake_suite / "test_cases.py").write_text(CASES.replace('"decision": "REDACT"', '"decision": "BLOCK"'))

    assert client.post("/api/suite/runs", json={"case": ids["redacts the number"]}).status_code == 202
    run = finished()
    assert run["case"] == ids["redacts the number"]
    assert statuses(run) == {"redacts the number": ("PII", "done", "BLOCK"), "clean request is held": ("LEG", "done", "REQUIRE_APPROVAL"),
                             "without an area": ("OTHER", "done", None)}


@pytest.mark.parametrize("case", ["no_such_case", "-p evil", "../../etc/passwd", "tests/support/test_web.py"])
def test_only_collected_case_ids_are_accepted(fake_suite, case):
    response = client.post("/api/suite/runs", json={"case": case})
    assert response.status_code == 404 and response.json()["error"]["message"] == "unknown test case"
    assert client.get("/api/suite/runs/latest").json()["status"] == "not_run"


def test_a_suite_that_cannot_run_is_reported_as_an_error(fake_suite, monkeypatch):
    client.get("/api/suite/runs/latest")  # collected while the folder still exists
    monkeypatch.setattr(suite, "SUITE_PATH", fake_suite / "missing")
    client.post("/api/suite/runs")
    run = finished()
    assert run["status"] == "error" and "did not run" in run["error"]
    assert {case["state"] for case in run["cases"]} == {"not_run"}
