"""Real file-backed management API contract, shared by both server applications."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
import subprocess
import sys

from fastapi.testclient import TestClient
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import pytest

from configuration.service import ConfigService
from configuration import service as config_storage
from configuration.api import install_config_api



@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setenv("CONFIG_ALLOWED_ORIGINS", "http://localhost:5173")
    service = ConfigService(tmp_path / "configs")
    app = FastAPI(openapi_url="/api/v1/openapi.json")
    install_config_api(app, config_service=service)
    app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173"], allow_methods=["GET", "PUT"],
                       allow_headers=["Authorization", "X-Admin-Token", "Content-Type"])
    with TestClient(app, headers={"X-Admin-Token": "test-admin-token"}) as client:
        yield client, service


def test_update_select_restart_and_revision_contract(api):
    client, service = api
    old = service.snapshot_for_intercept()
    response = client.get("/api/v1/configs/standard")
    assert response.headers["etag"] == f'"{old["revision"]}"'
    config = response.json()
    config["budget"]["tokens"] = 12345
    updated = client.put("/api/v1/configs/standard", json=config)
    assert updated.status_code == 200
    result = updated.json()
    assert set(result) == {"name", "revision", "updated_at", "selected", "active_revision", "requires_selection"}
    assert result["selected"] and result["requires_selection"]
    assert result["active_revision"] == old["revision"]
    assert service.snapshot_for_intercept() == old  # saving does not mutate active snapshot
    selected = client.put("/api/v1/config-selection", json={"name": "standard", "revision": result["revision"]})
    assert selected.status_code == 200
    assert set(selected.json()) == {"name", "revision", "selected_at", "effective_for"}
    assert selected.json()["effective_for"] == "new_sessions"
    again = client.put("/api/v1/config-selection", json={"name": "standard", "revision": result["revision"]})
    assert again.json() == selected.json()
    restarted = ConfigService(service.directory)
    assert restarted.snapshot_for_intercept()["config"]["budget"]["tokens"] == 12345
    assert restarted.get_config("standard")["revision"] == result["revision"]


def test_active_view_keeps_pinned_snapshot_until_selection(api):
    client, service = api
    original = client.get("/api/v1/configs/standard").json()
    active = service.snapshot_for_intercept()
    edited = {**original, "budget": {**original["budget"], "tokens": original["budget"]["tokens"] + 1}}
    saved = client.put("/api/v1/configs/standard", json=edited).json()
    listed = {item["name"]: item for item in client.get("/api/v1/configs").json()}
    assert listed["standard"]["revision"] == saved["revision"]
    assert listed["standard"]["active_revision"] == active["revision"]
    assert listed["strict"]["active_revision"] is None
    pinned = client.get("/api/v1/configs/standard?view=active")
    assert pinned.json() == original and pinned.headers["etag"] == f'"{active["revision"]}"'
    assert client.get("/api/v1/configs/standard?view=saved").json() == edited
    assert client.get("/api/v1/configs/strict?view=active").status_code == 409
    assert client.put("/api/v1/config-selection", json={"name": "standard", "revision": saved["revision"]}).status_code == 200
    pinned = client.get("/api/v1/configs/standard?view=active")
    assert pinned.json() == edited and pinned.headers["etag"] == f'"{saved["revision"]}"'


@pytest.mark.parametrize("name", ["lenient", "standard", "strict"])
def test_every_preset_is_editable_and_selectable(api, name):
    client, _ = api
    config = client.get(f"/api/v1/configs/{name}").json()
    config["budget"]["tokens"] += 1
    result = client.put(f"/api/v1/configs/{name}", json=config)
    assert result.status_code == 200
    selection = client.put("/api/v1/config-selection", json={"name": name, "revision": result.json()["revision"]})
    assert selection.status_code == 200


@pytest.mark.parametrize("method,path,body,headers,status,code", [
    ("PUT", "/api/v1/configs/standard", "{}", {"Content-Type": "text/plain"}, 415, "unsupported_media_type"),
    ("PUT", "/api/v1/configs/standard", "{", {}, 400, "bad_request"),
    ("PUT", "/api/v1/configs/standard", '{"name":"standard","name":"strict"}', {}, 400, "bad_request"),
    ("PUT", "/api/v1/configs/standard", "NaN", {}, 400, "bad_request"),
    ("PUT", "/api/v1/configs/standard", "{}", {}, 422, "invalid_config"),
    ("PUT", "/api/v1/configs/missing", "{}", {}, 404, "config_not_found"),
    ("PUT", "/api/v1/configs/Bad%20Name", "{}", {}, 400, "bad_request"),
    ("PUT", "/api/v1/config-selection", "{}", {}, 422, "invalid_config"),
    ("PUT", "/api/v1/configs/standard", " " * 64001, {}, 413, "config_too_large"),
    ("POST", "/api/v1/configs/standard", "{}", {}, 405, "method_not_allowed"),
    ("GET", "/api/v1/config-selection", "", {}, 405, "method_not_allowed"),
], ids=["wrong-media-type", "malformed-json", "duplicate-name", "nonfinite-json", "invalid-policy",
        "missing-config", "invalid-name", "invalid-selection", "oversize-body", "wrong-method", "selection-get"])
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
    lambda c: c["intercept"]["pattern_match"].update(patterns=["("]),
    lambda c: c["intercept"]["pattern_match"].update(patterns=[r"(x)\1"]),
    lambda c: c["intercept"]["pattern_match"].update(fields=["raw_credentials"]),
    lambda c: c["intercept"]["pattern_match"].update(action="ALLOW"),
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
                          headers={"Content-Type": "application/json"})
    assert response.status_code in (400, 422)
    assert "synthetic-private-secret" not in response.text and "do not echo" not in response.text
    assert service.path.read_bytes() == before


def test_stale_selection_and_corrupt_state(api, monkeypatch):
    client, service = api
    response = client.put("/api/v1/config-selection", json={"name": "standard", "revision": "sha256:" + "0" * 64})
    assert response.status_code == 409
    service.path.write_text("{broken", encoding="utf-8")
    assert client.get("/api/v1/configs").status_code == 503


def test_failed_replace_does_not_change_state(api, monkeypatch):
    client, service = api
    before = service.path.read_bytes()
    config = client.get("/api/v1/configs/standard").json()
    config["budget"]["tokens"] += 1
    def fail(*_):
        raise OSError("synthetic-private-error")
    monkeypatch.setattr(config_storage, "_replace_file", fail)
    response = client.put("/api/v1/configs/standard", json=config)
    assert response.status_code == 503
    assert "synthetic-private-error" not in response.text
    assert service.path.read_bytes() == before
    assert not list(service.directory.glob(".config-*"))


def test_directory_sync_failure_rolls_back_selection(api, monkeypatch):
    client, service = api
    before = service.path.read_bytes()
    config = service.get_config("strict")
    def fail_directory(_directory):
        raise OSError("synthetic sync failure")
    monkeypatch.setattr(config_storage, "_sync_directory", fail_directory)
    response = client.put("/api/v1/config-selection", json={"name": "strict", "revision": config["revision"]})
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
        assert operation["security"] == [{"AdminToken": []}, {"AdminBearer": []}]
        assert "requestBody" in operation
        assert {"200", "400", "404", "405", "409", "413", "415", "422", "503"} <= set(operation["responses"])
    assert "PolicyConfig" in schema["components"]["schemas"]


def test_config_writes_require_server_secret_and_client_credential(api, monkeypatch):
    client, service = api
    config = client.get("/api/v1/configs/standard").json()
    before = service.path.read_bytes()
    client.headers.pop("X-Admin-Token")
    assert client.put("/api/v1/configs/standard", json=config).status_code == 401
    assert client.put("/api/v1/configs/standard", json=config, headers={"X-Admin-Token": "wrong"}).status_code == 401
    assert client.put("/api/v1/configs/standard", json=config,
                      headers={"Authorization": "Bearer test-admin-token"}).status_code == 200
    monkeypatch.delenv("CONFIG_ADMIN_TOKEN")
    blocked = client.put("/api/v1/configs/standard", json=config)
    assert blocked.status_code == 503 and blocked.json()["error"]["code"] == "config_writes_disabled"
    assert service.path.read_bytes() == before
    assert client.get("/api/v1/configs").status_code == 200


@pytest.mark.parametrize("headers", [{"Origin": "https://evil.example"}, {"Origin": "null"},
                                     {"Sec-Fetch-Site": "cross-site"}, {"Origin": "http://["}])
def test_cross_origin_config_mutation_is_rejected(api, headers):
    client, service = api
    before = service.path.read_bytes()
    assert client.put("/api/v1/config-selection", json={}, headers=headers).status_code == 403
    assert service.path.read_bytes() == before


def test_allowed_config_browser_origin_and_rate_limit(api, monkeypatch):
    client, _ = api
    monkeypatch.setenv("CONFIG_WRITE_RATE_LIMIT", "2")
    config = client.get("/api/v1/configs/standard").json()
    for origin in ("http://testserver", "http://localhost:5173"):
        assert client.put("/api/v1/configs/standard", json=config, headers={"Origin": origin}).status_code == 200
    limited = client.put("/api/v1/configs/standard", json=config)
    assert limited.status_code == 429 and limited.headers["Retry-After"] == "60"


def test_read_only_config_install_does_not_initialize_or_write(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_ADMIN_TOKEN", "test-admin-token")
    service = ConfigService(tmp_path / "config")
    app = FastAPI()
    install_config_api(app, config_service=service, allow_writes=False)
    with TestClient(app) as client:
        assert not service.directory.exists()
        assert client.get("/api/v1/configs").status_code == 503
        assert not service.directory.exists()
        service.snapshot_for_intercept()
        before = {path.name: path.read_bytes() for path in service.directory.iterdir()}
        assert client.get("/api/v1/configs").status_code == 200
        assert client.put("/api/v1/config-selection", json={}, headers={"X-Admin-Token": "test-admin-token"}).status_code == 405
        assert {path.name: path.read_bytes() for path in service.directory.iterdir()} == before


@pytest.mark.parametrize("trusted_proxy,status", [("172.30.0.2", 200), ("172.30.0.3", 403)])
def test_tls_proxy_origin_check_requires_trusted_forwarded_scheme(tmp_path, monkeypatch, trusted_proxy, status):
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    monkeypatch.setenv("CONFIG_ADMIN_TOKEN", "test-admin-token")
    app = FastAPI()
    service = ConfigService(tmp_path / "config")
    install_config_api(app, config_service=service)
    proxy_app = ProxyHeadersMiddleware(app, trusted_hosts=trusted_proxy)
    with TestClient(proxy_app, client=("172.30.0.2", 35000)) as client:
        config = client.get("/api/v1/configs/standard").json()
        result = client.put("/api/v1/configs/standard", json=config, headers={
            "Host": "demo.example", "Origin": "https://demo.example", "X-Forwarded-Proto": "https",
            "X-Admin-Token": "test-admin-token"})
        assert result.status_code == status


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


def test_state_from_an_older_schema_is_upgraded_instead_of_failing(api):
    client, service = api
    edited = client.get("/api/v1/configs/strict").json()
    edited["budget"]["tokens"] = 4321
    assert client.put("/api/v1/configs/strict", json=edited).status_code == 200
    state = json.loads(service.path.read_text())
    for entry in (state["configs"]["lenient"], state["configs"]["standard"], state["selection"]):
        del entry["config"]["intercept"]["semantic_guard"]["allowed_models"]  # written before the field existed
    service.path.write_text(json.dumps(state))

    listed = {c["name"]: c for c in client.get("/api/v1/configs").json()}
    assert listed["standard"]["selected"] and not listed["standard"]["requires_selection"]
    assert client.get("/api/v1/configs/standard").json()["intercept"]["semantic_guard"]["allowed_models"]
    assert client.get("/api/v1/configs/strict").json()["budget"]["tokens"] == 4321  # a valid edit survives
    assert ConfigService(service.directory).snapshot_for_intercept()["name"] == "standard"
