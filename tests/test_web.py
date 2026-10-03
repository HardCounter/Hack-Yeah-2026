from fastapi.testclient import TestClient

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
