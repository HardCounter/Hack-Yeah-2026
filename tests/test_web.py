from fastapi.testclient import TestClient

from intercept.auditors import Pipeline
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


def test_policy_page_is_served():
    response = client.get("/policy.html")
    assert response.status_code == 200
    assert "Save policy" in response.text


def test_presets_are_listed_and_valid(monkeypatch, tmp_path):
    monkeypatch.setenv("POLICY_DIR", str(tmp_path))
    names = [p["name"] for p in client.get("/api/v1/policies").json()]
    assert names == ["lenient", "standard", "strict"]
    for name in names:
        client_policy = client.get(f"/api/v1/policies/{name}").json()
        Pipeline(client_policy["auditors"])  # the gateway accepts every preset


def test_saving_needs_admin_token(monkeypatch, tmp_path):
    monkeypatch.setenv("POLICY_DIR", str(tmp_path))
    policy = client.get("/api/v1/policies/strict").json()
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    assert client.put("/api/v1/policies/mine", json=policy).status_code == 503
    monkeypatch.setenv("ADMIN_TOKEN", "s3cret")
    assert client.put("/api/v1/policies/mine", json=policy, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert not list(tmp_path.iterdir())


def test_save_round_trip_and_rejections(monkeypatch, tmp_path):
    monkeypatch.setenv("POLICY_DIR", str(tmp_path))
    monkeypatch.setenv("ADMIN_TOKEN", "s3cret")
    auth = {"Authorization": "Bearer s3cret"}
    policy = client.get("/api/v1/policies/strict").json()
    policy["budget"]["tokens"] = 1234

    assert client.put("/api/v1/policies/my-policy", json=policy, headers=auth).status_code == 200
    assert client.get("/api/v1/policies/my-policy").json()["budget"]["tokens"] == 1234
    assert {"name": "my-policy", "preset": False, "description": policy["description"]} in client.get("/api/v1/policies").json()

    assert client.put("/api/v1/policies/strict", json=policy, headers=auth).status_code == 409  # presets are read-only
    assert client.put("/api/v1/policies/..%2Fevil", json=policy, headers=auth).status_code >= 400  # never reaches the save code
    assert client.put("/api/v1/policies/Bad Name", json=policy, headers=auth).status_code == 422
    bad = {**policy, "require_approval": ["delete_everything"]}
    assert client.put("/api/v1/policies/bad", json=bad, headers=auth).status_code == 422
    bad = {**policy, "auditors": [{"id": "x", "type": "webhook", "config": {}}]}
    assert client.put("/api/v1/policies/bad", json=bad, headers=auth).status_code == 422
    assert sorted(p.name for p in tmp_path.iterdir()) == ["my-policy.json"]
