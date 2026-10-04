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


def test_opencode_wrapper_page_is_served_under_its_own_path():
    response = client.get("/opencode-wrapper/")
    assert response.status_code == 200
    assert "opencode-wrapper" in response.text
    assert client.get("/opencode-wrapper", follow_redirects=False).status_code in (301, 307, 308)
