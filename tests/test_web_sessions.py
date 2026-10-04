"""Session API tests. A fake backend replaces OpenCode and the gateway, so no API key is needed."""
import pytest
from fastapi.testclient import TestClient

from web import sessions
from web.main import app


class FakeBackend:
    def __init__(self):
        self.started, self.stopped, self.fail_start = [], [], False

    async def start(self, session):
        if self.fail_start:
            raise RuntimeError("boom")
        self.started.append(session.id)
        session.state["events"] = []

    async def send(self, session, prompt, timeout):
        session.state["events"].append({"seq": len(session.state["events"]), "action_type": "tool_call"})
        return True, f"you said {prompt.upper()} (message {session.messages + 1})"

    def evidence(self, session):
        return list(session.state["events"]), [{"rule_id": "gateway.hard_deny"}]

    async def stop(self, session):
        self.stopped.append(session.id)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("MAX_SESSIONS", "2")
    monkeypatch.setenv("RUNS_DAILY_CAP", "4")
    backend = FakeBackend()
    monkeypatch.setattr(sessions, "_manager", sessions.SessionManager(backend))
    with TestClient(app) as test_client:
        test_client.backend = backend
        yield test_client


def new_session(client):
    response = client.post("/api/v1/sessions")
    assert response.status_code == 201
    return response.json()["session_id"]


def test_session_keeps_state_across_messages_and_returns_evidence(client):
    sid = new_session(client)
    first = client.post(f"/api/v1/sessions/{sid}/messages", json={"prompt": "hi"}).json()
    second = client.post(f"/api/v1/sessions/{sid}/messages", json={"prompt": "again"}).json()
    assert first["ok"] and first["reply"] == "you said HI (message 1)"
    assert second["reply"] == "you said AGAIN (message 2)" and len(second["events"]) == 2
    polled = client.get(f"/api/v1/sessions/{sid}/events").json()
    assert len(polled["events"]) == 2 and polled["busy"] is False
    assert polled["findings"][0]["rule_id"] == "gateway.hard_deny"


def test_closing_a_session_stops_its_processes(client):
    sid = new_session(client)
    assert client.delete(f"/api/v1/sessions/{sid}").status_code == 204
    assert client.backend.stopped == [sid]
    assert client.post(f"/api/v1/sessions/{sid}/messages", json={"prompt": "hi"}).status_code == 404
    assert client.delete(f"/api/v1/sessions/{sid}").status_code == 204  # closing twice is harmless


def test_oldest_idle_session_makes_room_for_a_new_one(client):
    first, second = new_session(client), new_session(client)
    client.post(f"/api/v1/sessions/{first}/messages", json={"prompt": "keep me fresh"})
    third = new_session(client)
    assert client.backend.stopped == [second]
    assert client.get(f"/api/v1/sessions/{first}/events").status_code == 200
    assert client.get(f"/api/v1/sessions/{third}/events").status_code == 200


def test_invalid_input_and_unknown_sessions_are_rejected(client):
    sid = new_session(client)
    assert client.post(f"/api/v1/sessions/{sid}/messages", json={}).status_code == 422
    assert client.post(f"/api/v1/sessions/{sid}/messages", json={"prompt": ""}).status_code == 422
    assert client.post(f"/api/v1/sessions/{sid}/messages", json={"prompt": "x" * 4001}).status_code == 422
    assert client.get("/api/v1/sessions/ses_" + "0" * 32 + "/events").status_code == 404
    assert client.get("/api/v1/sessions/..%2F..%2Fetc/events").status_code == 404


def test_daily_cap_blocks_further_messages(client):
    sid = new_session(client)
    for _ in range(4):
        assert client.post(f"/api/v1/sessions/{sid}/messages", json={"prompt": "hi"}).status_code == 200
    assert client.post(f"/api/v1/sessions/{sid}/messages", json={"prompt": "hi"}).status_code == 429


def test_failed_start_is_reported_and_leaves_no_session(client):
    client.backend.fail_start = True
    assert client.post("/api/v1/sessions").status_code == 503
    assert sessions.manager().sessions == {}


def test_shutdown_stops_every_session(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNS_DIR", str(tmp_path / "runs"))
    backend = FakeBackend()
    monkeypatch.setattr(sessions, "_manager", sessions.SessionManager(backend))
    with TestClient(app) as test_client:
        opened = [new_session(test_client), new_session(test_client)]
    assert sorted(backend.stopped) == sorted(opened)


def test_info_reports_model_and_scope(client, monkeypatch):
    monkeypatch.setenv("OPENCODE_MODEL", "openai/test-model")
    assert client.get("/api/v1/info").json() == {"model": "openai/test-model", "application": "APP-0001"}
