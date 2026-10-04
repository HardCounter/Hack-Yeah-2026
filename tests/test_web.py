from fastapi.testclient import TestClient

from intercept.policy.auditors import Pipeline
from web.main import app

client = TestClient(app)


def test_healthz_reports_ok_and_commit(monkeypatch):
    monkeypatch.setenv("GIT_SHA", "abc1234")
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "commit": "abc1234"}


def test_index_page_is_served():
    response = client.get("/")
    assert response.status_code == 200
    assert "AI Control Layer" in response.text


def test_presets_are_listed_and_valid(monkeypatch, tmp_path):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    names = [p["name"] for p in client.get("/api/v1/configs").json()]
    assert names == ["lenient", "standard", "strict"]
    for name in names:
        preset = client.get(f"/api/v1/configs/{name}").json()
        Pipeline(preset["auditors"])  # the gateway accepts every preset


def test_save_round_trip_and_rejections(monkeypatch, tmp_path):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    config = client.get("/api/v1/configs/strict").json()
    config["budget"]["tokens"] = 1234

    assert client.put("/api/v1/configs/my-policy", json=config).status_code == 200
    assert client.get("/api/v1/configs/my-policy").json()["budget"]["tokens"] == 1234
    assert {"name": "my-policy", "preset": False, "description": config["description"]} in client.get("/api/v1/configs").json()

    assert client.put("/api/v1/configs/strict", json=config).status_code == 409  # presets are read-only
    assert client.put("/api/v1/configs/..%2Fevil", json=config).status_code >= 400  # never reaches the save code
    assert client.put("/api/v1/configs/Bad Name", json=config).status_code == 422
    assert client.put("/api/v1/configs/Strict", json=config).status_code == 409  # preset names in any case
    unordered = {**config, "semantic_guard": {**config["semantic_guard"], "approve_threshold": 0.99}}
    assert client.put("/api/v1/configs/bad", json=unordered).status_code == 422  # approve above block
    bad = {**config, "require_approval": ["delete_everything"]}
    assert client.put("/api/v1/configs/bad", json=bad).status_code == 422
    bad = {**config, "auditors": [{"id": "x", "type": "webhook", "config": {}}]}
    assert client.put("/api/v1/configs/bad", json=bad).status_code == 422
    assert client.put("/api/v1/configs/My-Policy2", json=config).status_code == 200  # uppercase allowed
    assert sorted(p.name for p in tmp_path.iterdir()) == ["My-Policy2.json", "my-policy.json"]


def test_opencode_wrapper_page_is_served_under_its_own_path():
    response = client.get("/opencode-wrapper/")
    assert response.status_code == 200
    assert "opencode-wrapper" in response.text
    assert client.get("/opencode-wrapper", follow_redirects=False).status_code in (301, 307, 308)
