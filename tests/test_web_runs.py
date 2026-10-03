"""Run API tests. A stub replaces simulation.opencode_runner, so no OpenCode or API key is needed."""
import json
import sys
import time

import pytest
from fastapi.testclient import TestClient

from web import runs
from web.main import app

FAKE_RUNNER = '''
import json, pathlib, sys
app, out = sys.argv[1], pathlib.Path(sys.argv[sys.argv.index("--output-dir") + 1])
code = int(pathlib.Path(__file__).with_name("exit_code").read_text())
art = out / f"{app}-fake"
art.mkdir(parents=True)
(art / "gateway.log").write_text("internal log, never served")
if code != 1:
    (art / "events.jsonl").write_text(json.dumps({"seq": 0, "action_type": "tool_call", "case_id": app}) + "\\n")
    (art / "findings.jsonl").write_text(json.dumps({"rule_id": "gateway.hard_deny"}) + "\\n")
status = "VERIFIED_SUCCESS" if code == 0 else "VERIFICATION_INCOMPLETE"
(art / "verification.json").write_text(json.dumps({"verification_status": status, "checks": []}))
sys.exit(code)
'''


@pytest.fixture
def client(tmp_path, monkeypatch):
    script = tmp_path / "fake_runner.py"
    script.write_text(FAKE_RUNNER)
    (tmp_path / "exit_code").write_text("0")
    monkeypatch.setenv("RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("RUNNER_CMD", json.dumps([sys.executable, str(script)]))
    monkeypatch.setenv("RUNS_DAILY_CAP", "3")
    monkeypatch.setattr(runs, "_manager", None)
    with TestClient(app) as test_client:
        test_client.exit_code = tmp_path / "exit_code"
        yield test_client


def wait_done(client, run_id):
    for _ in range(100):
        run = client.get(f"/api/v1/runs/{run_id}").json()
        if run["status"] in runs.TERMINAL:
            return run
        time.sleep(0.1)
    raise AssertionError("run did not finish")


def test_applications_lists_the_fifteen_synthetic_cases(client):
    apps = client.get("/api/v1/applications").json()["applications"]
    assert apps[0] == "APP-0001" and apps[-1] == "APP-0015" and len(apps) == 15


def test_run_finishes_and_serves_only_sanitized_exports(client):
    response = client.post("/api/v1/runs", json={"application": "APP-0001", "prompt": "secret words"})
    assert response.status_code == 202
    run_id = response.json()["run_id"]
    run = wait_done(client, run_id)
    assert run["status"] == "finished" and run["exit_code"] == 0
    assert run["verification"]["verification_status"] == "VERIFIED_SUCCESS"
    data = client.get(f"/api/v1/runs/{run_id}/events").json()
    assert data["events"][0]["case_id"] == "APP-0001"
    assert data["findings"][0]["rule_id"] == "gateway.hard_deny"
    # The prompt is not stored or returned, and internal logs are not reachable.
    assert "secret words" not in json.dumps(run) and run["custom_prompt"] is True
    assert client.get("/api/v1/runs").json()["runs"][0]["run_id"] == run_id
    assert client.get(f"/{run_id}/APP-0001-fake/gateway.log").status_code == 404


def test_stream_ends_with_the_result(client):
    run_id = client.post("/api/v1/runs", json={"application": "APP-0002"}).json()["run_id"]
    wait_done(client, run_id)
    body = client.get(f"/api/v1/runs/{run_id}/stream").text
    assert "event: action" in body and body.rstrip().splitlines()[-2] == "event: result"


def test_failed_runner_is_reported_as_failed(client):
    client.exit_code.write_text("1")
    run_id = client.post("/api/v1/runs", json={"application": "APP-0001"}).json()["run_id"]
    run = wait_done(client, run_id)
    assert run["status"] == "failed" and run["detail"] == "RUNNER_FAILED"
    assert run["verification"]["verification_status"] == "VERIFICATION_INCOMPLETE"


def test_incomplete_verification_is_a_finished_run(client):
    client.exit_code.write_text("2")
    run_id = client.post("/api/v1/runs", json={"application": "APP-0003"}).json()["run_id"]
    run = wait_done(client, run_id)
    assert run["status"] == "finished" and run["exit_code"] == 2


def test_invalid_input_and_unknown_runs_are_rejected(client):
    assert client.post("/api/v1/runs", json={"application": "APP-0016"}).status_code == 422
    assert client.post("/api/v1/runs", json={"application": "../etc"}).status_code == 422
    assert client.post("/api/v1/runs", json={"application": "APP-0001", "prompt": "x" * 4001}).status_code == 422
    assert client.get("/api/v1/runs/run_0000000000000000").status_code == 404
    assert client.get("/api/v1/runs/nope").status_code == 404


def test_daily_cap_blocks_further_runs(client):
    for _ in range(3):
        run_id = client.post("/api/v1/runs", json={"application": "APP-0001"}).json()["run_id"]
        wait_done(client, run_id)
    assert client.post("/api/v1/runs", json={"application": "APP-0001"}).status_code == 429


def test_runs_left_open_by_a_restart_are_marked_failed(client, monkeypatch):
    run_id = client.post("/api/v1/runs", json={"application": "APP-0001"}).json()["run_id"]
    run = wait_done(client, run_id)
    meta = runs.manager().root / run_id / "meta.json"
    meta.write_text(json.dumps({**{k: run[k] for k in run if k != "verification"}, "status": "running"}))
    monkeypatch.setattr(runs, "_manager", None)  # simulate a new process
    assert client.get(f"/api/v1/runs/{run_id}").json()["detail"] == "INTERRUPTED"
