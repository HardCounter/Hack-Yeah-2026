"""Real file-backed management API contract, shared by both server applications."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
import stat
import subprocess
import sys

from fastapi.testclient import TestClient
import pytest

from configuration.service import ConfigService
from persistence.http_api import create_app

TOKEN = "synthetic-management-credential"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def api(tmp_path):
    service = ConfigService(tmp_path / "configs")
    app = create_app(frozenset({"http://localhost:5173"}), config_service=service, config_admin_token=TOKEN)
    with TestClient(app) as client:
        yield client, service


def test_update_select_restart_and_revision_contract(api):
    client, service = api
    old = service.snapshot_for_intercept()
    response = client.get("/api/v1/configs/standard")
    assert response.headers["etag"] == f'"{old["revision"]}"'
    config = response.json()
    config["budget"]["tokens"] = 12345
    updated = client.put("/api/v1/configs/standard", json=config, headers=HEADERS)
    assert updated.status_code == 200
    result = updated.json()
    assert set(result) == {"name", "revision", "updated_at", "selected", "active_revision", "requires_selection"}
    assert result["selected"] and result["requires_selection"]
    assert result["active_revision"] == old["revision"]
    assert service.snapshot_for_intercept() == old  # saving does not mutate active snapshot
    selected = client.put("/api/v1/config-selection", json={"name": "standard", "revision": result["revision"]}, headers=HEADERS)
    assert selected.status_code == 200
    assert set(selected.json()) == {"name", "revision", "selected_at", "effective_for"}
    assert selected.json()["effective_for"] == "new_sessions"
    again = client.put("/api/v1/config-selection", json={"name": "standard", "revision": result["revision"]}, headers=HEADERS)
    assert again.json() == selected.json()
    restarted = ConfigService(service.directory)
    assert restarted.snapshot_for_intercept()["config"]["budget"]["tokens"] == 12345
    assert restarted.get_config("standard")["revision"] == result["revision"]


@pytest.mark.parametrize("name", ["lenient", "standard", "strict"])
def test_every_preset_is_editable_and_selectable(api, name):
    client, _ = api
    config = client.get(f"/api/v1/configs/{name}").json()
    config["budget"]["tokens"] += 1
    result = client.put(f"/api/v1/configs/{name}", json=config, headers=HEADERS)
    assert result.status_code == 200
    selection = client.put("/api/v1/config-selection", json={"name": name, "revision": result.json()["revision"]}, headers=HEADERS)
    assert selection.status_code == 200


@pytest.mark.parametrize("method,path,body,headers,status,code", [
    ("PUT", "/api/v1/configs/standard", "{}", {}, 401, "unauthorized"),
    ("PUT", "/api/v1/config-selection", "{}", {}, 401, "unauthorized"),
    ("PUT", "/api/v1/configs/standard", "{}", {**HEADERS, "Content-Type": "text/plain"}, 415, "unsupported_media_type"),
    ("PUT", "/api/v1/configs/standard", "{", HEADERS, 400, "bad_request"),
    ("PUT", "/api/v1/configs/standard", '{"name":"standard","name":"strict"}', HEADERS, 400, "bad_request"),
    ("PUT", "/api/v1/configs/standard", "NaN", HEADERS, 400, "bad_request"),
    ("PUT", "/api/v1/configs/standard", "{}", HEADERS, 422, "invalid_config"),
    ("PUT", "/api/v1/configs/missing", "{}", HEADERS, 404, "config_not_found"),
    ("PUT", "/api/v1/configs/Bad%20Name", "{}", HEADERS, 400, "bad_request"),
    ("PUT", "/api/v1/config-selection", "{}", HEADERS, 422, "invalid_config"),
    ("PUT", "/api/v1/configs/standard", " " * 64001, HEADERS, 413, "config_too_large"),
    ("POST", "/api/v1/configs/standard", "{}", HEADERS, 405, "method_not_allowed"),
    ("GET", "/api/v1/config-selection", "", HEADERS, 405, "method_not_allowed"),
])
def test_management_errors_are_sanitized(api, method, path, body, headers, status, code):
    client, _ = api
    headers = {"Content-Type": "application/json", **headers}
    response = client.request(method, path, content=body, headers=headers)
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert set(response.json()["error"]) == {"code", "message", "details"}


@pytest.mark.parametrize("mutate", [
    lambda c: c["budget"].update(tokens=True),
    lambda c: c["budget"].update(cost_usd=float("inf")),
    lambda c: c.update(allowed_tools=["unknown_tool"]),
    lambda c: c.update(name="strict"),
    lambda c: c.update(allowed_models=["provider/model"]),
    lambda c: c.update(max_output_tokens=8193),
    lambda c: c["intercept"]["velocity_guard"].update(window_s=0),
    lambda c: c["intercept"]["semantic_guard"].update(approve_threshold=0.99),
    lambda c: c["intercept"]["feedback"].update(allow_agent_scope=True),
    lambda c: c.update(auditors=[{"id": "x", "type": "webhook", "config": {}}]),
    lambda c: c.update({"synthetic-private-secret": "do not echo"}),
])
def test_invalid_policy_never_replaces_selected_state(api, mutate):
    client, service = api
    before = service.path.read_bytes()
    config = client.get("/api/v1/configs/standard").json()
    mutate(config)
    # Python's serializer deliberately exercises nonstandard Infinity input too.
    response = client.put("/api/v1/configs/standard", content=json.dumps(config),
                          headers={**HEADERS, "Content-Type": "application/json"})
    assert response.status_code in (400, 422)
    assert "synthetic-private-secret" not in response.text and "do not echo" not in response.text
    assert service.path.read_bytes() == before


def test_stale_selection_disabled_auth_and_corrupt_state(api, monkeypatch):
    client, service = api
    response = client.put("/api/v1/config-selection", json={"name": "standard", "revision": "sha256:" + "0" * 64}, headers=HEADERS)
    assert response.status_code == 409
    client.app.state.config_admin_token = ""
    assert client.put("/api/v1/config-selection", json={}, headers=HEADERS).status_code == 401
    service.path.write_text("{broken", encoding="utf-8")
    assert client.get("/api/v1/configs").status_code == 503


def test_failed_replace_does_not_change_state(api, monkeypatch):
    client, service = api
    before = service.path.read_bytes()
    config = client.get("/api/v1/configs/standard").json()
    config["budget"]["tokens"] += 1
    def fail(*_):
        raise OSError("synthetic-private-error")
    monkeypatch.setattr(os, "replace", fail)
    response = client.put("/api/v1/configs/standard", json=config, headers=HEADERS)
    assert response.status_code == 503
    assert "synthetic-private-error" not in response.text
    assert service.path.read_bytes() == before
    assert not list(service.directory.glob(".config-*"))


def test_directory_sync_failure_rolls_back_selection(api, monkeypatch):
    client, service = api
    before = service.path.read_bytes()
    config = service.get_config("strict")
    real_fsync = os.fsync
    def fail_directory(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("synthetic sync failure")
        return real_fsync(fd)
    monkeypatch.setattr(os, "fsync", fail_directory)
    response = client.put("/api/v1/config-selection", json={"name": "strict", "revision": config["revision"]}, headers=HEADERS)
    assert response.status_code == 503
    assert service.path.read_bytes() == before
    assert not list(service.directory.glob(".config-*"))


def test_multiple_services_serialize_updates_without_lost_configs(tmp_path):
    service = ConfigService(tmp_path)
    service.snapshot_for_intercept()
    def update(name):
        writer = ConfigService(tmp_path)
        config = writer.get_config(name)["config"]
        config["budget"]["tokens"] = 999
        return writer.update(name, config)
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(update, ("lenient", "standard", "strict")))
    assert all(result["revision"] for result in results)
    assert all(service.get_config(name)["config"]["budget"]["tokens"] == 999
               for name in ("lenient", "standard", "strict"))


def test_separate_processes_share_lock_and_persist_edits(tmp_path):
    ConfigService(tmp_path).snapshot_for_intercept()
    program = """
import sys
from configuration.service import ConfigService
service = ConfigService(sys.argv[1])
config = service.get_config(sys.argv[2])["config"]
config["budget"]["tokens"] = 777
service.update(sys.argv[2], config)
"""
    def update(name):
        return subprocess.run([sys.executable, "-c", program, str(tmp_path), name],
                              capture_output=True, timeout=15).returncode
    with ThreadPoolExecutor(max_workers=3) as pool:
        assert list(pool.map(update, ("lenient", "standard", "strict"))) == [0, 0, 0]
    reader = ConfigService(tmp_path)
    assert all(reader.get_config(name)["config"]["budget"]["tokens"] == 777
               for name in ("lenient", "standard", "strict"))


def test_corrupt_selected_metadata_fails_closed(api):
    client, service = api
    state = json.loads(service.path.read_text())
    state["selection"]["selected_at"] = "not-a-timestamp"
    service.path.write_text(json.dumps(state))
    assert client.get("/api/v1/configs").status_code == 503


def test_deleted_existing_state_does_not_reseed_defaults(api):
    client, service = api
    service.path.unlink()
    assert client.get("/api/v1/configs").status_code == 503
    assert not service.path.exists()


def test_cors_and_openapi_contract(api):
    client, _ = api
    response = client.options("/api/v1/config-selection", headers={
        "Origin": "http://localhost:5173", "Access-Control-Request-Method": "PUT",
        "Access-Control-Request-Headers": "authorization,content-type"})
    assert response.status_code == 200
    schema = client.get("/api/v1/openapi.json").json()
    for path in ("/api/v1/configs/{name}", "/api/v1/config-selection"):
        operation = schema["paths"][path]["put"]
        assert operation["security"] == [{"ConfigManagementBearer": []}]
        assert "requestBody" in operation
        assert {"200", "400", "401", "404", "405", "409", "413", "415", "422", "503"} <= set(operation["responses"])
    assert "PolicyConfig" in schema["components"]["schemas"]


def test_service_returns_detached_snapshot_and_does_not_overwrite_defaults(tmp_path):
    service = ConfigService(tmp_path)
    selected = service.snapshot_for_intercept()
    selected["config"]["budget"]["tokens"] = 1
    assert service.snapshot_for_intercept()["config"]["budget"]["tokens"] == 20000
    config = service.get_config("lenient")["config"]
    config["budget"]["tokens"] = 1234
    service.update("lenient", config)
    assert ConfigService(tmp_path).get_config("lenient")["config"]["budget"]["tokens"] == 1234


def test_reserved_auditor_identifier_does_not_relax_general_secret_filter():
    from persistence.privacy import token
    assert token("secret-scanner", required=True) == "secret-scanner"
    for value in ("synthetic-secret", "secret-scanner-extra", "password", "ghp_synthetic"):
        with pytest.raises(ValueError):
            token(value, required=True)
