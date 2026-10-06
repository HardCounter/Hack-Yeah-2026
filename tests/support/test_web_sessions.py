"""Session API tests. A fake backend replaces OpenCode and the gateway, so no API key is needed."""
import asyncio
import os
import signal
import sys
import time
from types import SimpleNamespace

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
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "configs"))
    backend = FakeBackend()
    monkeypatch.setattr(sessions, "_manager", sessions.SessionManager(backend))
    with TestClient(app) as test_client:
        test_client.backend = backend
        yield test_client


def new_session(client):
    response = client.post("/opencode-wrapper/api/sessions")
    assert response.status_code == 201
    return response.json()["session_id"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX filesystem permission bits")
def test_sessions_and_process_logs_are_private_under_permissive_umask(client):
    from simulation.opencode_runner import _log
    original = os.umask(0)
    try:
        sid = new_session(client)
        folder = sessions._manager.sessions[sid].folder
        assert folder.stat().st_mode & 0o777 == 0o700
        assert folder.parent.stat().st_mode & 0o777 == 0o700
        for name in ("gateway.log", "opencode.log"):
            with _log(folder, name) as stream:
                stream.write("synthetic private diagnostic")
            assert (folder / name).stat().st_mode & 0o777 == 0o600
    finally:
        os.umask(original)


@pytest.mark.skipif(os.name == "nt", reason="POSIX filesystem permission bits")
def test_existing_session_root_is_made_private(tmp_path, monkeypatch):
    root = tmp_path / "existing"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    monkeypatch.setenv("RUNS_DIR", str(root))
    sessions.SessionManager(FakeBackend())
    assert root.stat().st_mode & 0o777 == 0o700


def test_session_keeps_state_across_messages_and_returns_evidence(client):
    sid = new_session(client)
    first = client.post(f"/opencode-wrapper/api/sessions/{sid}/messages", json={"prompt": "hi"}).json()
    second = client.post(f"/opencode-wrapper/api/sessions/{sid}/messages", json={"prompt": "again"}).json()
    assert first["ok"] and first["reply"] == "you said HI (message 1)"
    assert second["reply"] == "you said AGAIN (message 2)" and len(second["events"]) == 2
    polled = client.get(f"/opencode-wrapper/api/sessions/{sid}/events").json()
    assert len(polled["events"]) == 2 and polled["busy"] is False
    assert polled["findings"][0]["rule_id"] == "gateway.hard_deny"


def test_closing_a_session_stops_its_processes(client):
    sid = new_session(client)
    assert client.delete(f"/opencode-wrapper/api/sessions/{sid}").status_code == 204
    assert client.backend.stopped == [sid]
    assert client.post(f"/opencode-wrapper/api/sessions/{sid}/messages", json={"prompt": "hi"}).status_code == 404
    assert client.delete(f"/opencode-wrapper/api/sessions/{sid}").status_code == 204  # closing twice is harmless


def test_oldest_idle_session_makes_room_for_a_new_one(client):
    first, second = new_session(client), new_session(client)
    client.post(f"/opencode-wrapper/api/sessions/{first}/messages", json={"prompt": "keep me fresh"})
    third = new_session(client)
    assert client.backend.stopped == [second]
    assert client.get(f"/opencode-wrapper/api/sessions/{first}/events").status_code == 200
    assert client.get(f"/opencode-wrapper/api/sessions/{third}/events").status_code == 200


def test_invalid_input_and_unknown_sessions_are_rejected(client):
    sid = new_session(client)
    assert client.post(f"/opencode-wrapper/api/sessions/{sid}/messages", json={}).status_code == 422
    assert client.post(f"/opencode-wrapper/api/sessions/{sid}/messages", json={"prompt": ""}).status_code == 422
    assert client.post(f"/opencode-wrapper/api/sessions/{sid}/messages", json={"prompt": "x" * 4001}).status_code == 422
    assert client.get("/opencode-wrapper/api/sessions/ses_" + "0" * 32 + "/events").status_code == 404
    assert client.get("/opencode-wrapper/api/sessions/..%2F..%2Fetc/events").status_code == 404


def test_daily_cap_blocks_further_messages(client):
    sid = new_session(client)
    for _ in range(4):
        assert client.post(f"/opencode-wrapper/api/sessions/{sid}/messages", json={"prompt": "hi"}).status_code == 200
    assert client.post(f"/opencode-wrapper/api/sessions/{sid}/messages", json={"prompt": "hi"}).status_code == 429


def test_failed_start_is_reported_and_leaves_no_session(client):
    client.backend.fail_start = True
    assert client.post("/opencode-wrapper/api/sessions").status_code == 503
    assert sessions.manager().sessions == {}


def test_shutdown_stops_every_session(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "configs"))
    backend = FakeBackend()
    monkeypatch.setattr(sessions, "_manager", sessions.SessionManager(backend))
    with TestClient(app) as test_client:
        opened = [new_session(test_client), new_session(test_client)]
    assert sorted(backend.stopped) == sorted(opened)


def test_info_reports_model_and_scope(client, monkeypatch):
    monkeypatch.setenv("OPENCODE_MODEL", "openai/test-model")
    assert client.get("/opencode-wrapper/api/info").json() == {"model": "openai/test-model", "application": "APP-0001"}


def test_http_and_validation_errors_use_sanitized_envelope(client):
    response = client.get("/opencode-wrapper/api/sessions/ses_" + "0" * 32 + "/events")
    assert response.json() == {"error": {"code": "not_found", "message": "unknown or expired session", "details": {}}}
    sid = new_session(client)
    invalid = client.post(f"/opencode-wrapper/api/sessions/{sid}/messages", json={"prompt": {"private-secret": "do not echo"}})
    assert invalid.status_code == 422 and invalid.json()["error"]["code"] == "invalid_request"
    assert "private-secret" not in invalid.text and "do not echo" not in invalid.text


def test_cross_origin_browser_mutators_are_rejected(client):
    assert client.post("/opencode-wrapper/api/sessions", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/opencode-wrapper/api/sessions", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.post("/opencode-wrapper/api/sessions", headers={"Origin": "http://testserver"}).status_code == 201


def test_session_start_rate_limit(client, monkeypatch):
    monkeypatch.setenv("SESSION_START_RATE_LIMIT", "1")
    new_session(client)
    limited = client.post("/opencode-wrapper/api/sessions")
    assert limited.status_code == 429 and limited.headers["Retry-After"] == "60"


def test_idle_sessions_expire_without_another_create(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("SESSION_IDLE_S", "1")
    backend = FakeBackend()
    manager = sessions.SessionManager(backend)
    monkeypatch.setattr(sessions, "_manager", manager)
    with TestClient(app) as client:
        sid = new_session(client)
        manager.sessions[sid].last_used -= 2
        deadline = time.monotonic() + 3
        while sid in manager.sessions and time.monotonic() < deadline:
            time.sleep(0.05)
        assert sid not in manager.sessions and sid in backend.stopped
        assert client.get(f"/opencode-wrapper/api/sessions/{sid}/events").status_code == 404


def test_backend_failure_closes_session_and_returns_service_error(client, monkeypatch):
    sid = new_session(client)

    async def fail(*args):
        raise RuntimeError("private backend diagnostic")

    monkeypatch.setattr(client.backend, "send", fail)
    result = client.post(f"/opencode-wrapper/api/sessions/{sid}/messages", json={"prompt": "hello"})
    assert result.status_code == 503 and "private backend diagnostic" not in result.text
    assert sid in client.backend.stopped and sid not in sessions.manager().sessions


def test_message_timeout_closes_session(client, monkeypatch):
    sid = new_session(client)

    async def timeout(*args):
        raise asyncio.TimeoutError

    monkeypatch.setattr(client.backend, "send", timeout)
    result = client.post(f"/opencode-wrapper/api/sessions/{sid}/messages", json={"prompt": "hello"})
    assert result.status_code == 504 and result.json()["error"]["code"] == "session_timeout"
    assert sid in client.backend.stopped and sid not in sessions.manager().sessions


def test_start_timeout_and_cancellation_cleanup_partial_sessions(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNS_DIR", str(tmp_path))
    backend = FakeBackend()

    async def hang(session):
        backend.started.append(session.id)
        await asyncio.sleep(10)

    monkeypatch.setattr(backend, "start", hang)

    async def exercise():
        manager = sessions.SessionManager(backend)
        manager.start_timeout = 0.01
        with pytest.raises(sessions.HTTPException) as exc:
            await manager.create()
        assert exc.value.status_code == 503 and not manager.sessions
        manager.start_timeout = 10
        task = asyncio.create_task(manager.create())
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not manager.sessions and backend.stopped == backend.started

    asyncio.run(exercise())


@pytest.mark.skipif(not hasattr(sessions.os, "killpg"), reason="POSIX process groups")
def test_process_group_cleanup_runs_even_after_leader_exits(monkeypatch):
    signals = []

    async def waited():
        return 0

    monkeypatch.setattr(sessions.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    def no_leader(_pid):
        raise ProcessLookupError
    monkeypatch.setattr(sessions.os, "getpgid", no_leader)
    process = SimpleNamespace(pid=12345, returncode=0, wait=waited)
    asyncio.run(sessions._kill(process))
    assert signals == [(12345, signal.SIGTERM), (12345, signal.SIGKILL)]


@pytest.mark.skipif(not hasattr(sessions.os, "killpg"), reason="POSIX process groups")
def test_process_cleanup_avoids_a_reused_leader_pid(monkeypatch):
    def unexpected_signal(*args):
        pytest.fail("must not signal a reused PID")

    monkeypatch.setattr(sessions.os, "killpg", unexpected_signal)
    monkeypatch.setattr(sessions.os, "getpgid", lambda _pid: 12345)
    asyncio.run(sessions._kill(SimpleNamespace(pid=12345, returncode=0)))


def test_invalid_rate_environment_uses_safe_default(client, monkeypatch):
    monkeypatch.setenv("SESSION_START_RATE_LIMIT", "invalid")
    assert client.post("/opencode-wrapper/api/sessions").status_code == 201


def test_process_cleanup_without_posix_group_support(monkeypatch):
    calls = []
    monkeypatch.delattr(sessions.os, "killpg", raising=False)

    async def waited():
        return 0

    process = SimpleNamespace(pid=12345, returncode=None, wait=waited,
                              terminate=lambda: calls.append("terminate"), kill=lambda: calls.append("kill"))
    asyncio.run(sessions._kill(process))
    assert calls == ["terminate", "kill"]


@pytest.mark.skipif(not hasattr(sessions.os, "killpg"), reason="POSIX process groups")
def test_process_group_termination_reaches_a_real_child(tmp_path):
    marker = tmp_path / "child-stopped"
    child_program = """
import pathlib, signal, sys, time
def stopped(*args):
    pathlib.Path(sys.argv[1]).write_text('stopped')
    sys.exit(0)
signal.signal(signal.SIGTERM, stopped)
print('ready', flush=True)
while True:
    time.sleep(1)
"""
    parent_program = """
import signal, subprocess, sys, time
child = subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]], stdout=subprocess.PIPE)
child.stdout.readline()
def stopped(*args):
    child.wait(timeout=2)
    sys.exit(0)
signal.signal(signal.SIGTERM, stopped)
print('ready', flush=True)
while True:
    time.sleep(1)
"""

    async def exercise():
        process = await asyncio.create_subprocess_exec(sys.executable, "-c", parent_program, child_program, str(marker),
                                                       stdout=asyncio.subprocess.PIPE, start_new_session=True)
        try:
            await asyncio.wait_for(process.stdout.readline(), 5)
            await sessions._kill(process)
            assert process.returncode == 0 and marker.read_text() == "stopped"
        finally:
            await sessions._kill(process)

    asyncio.run(exercise())


def test_expiry_keeps_busy_sessions_and_capacity_rejects_new_work(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNS_DIR", str(tmp_path))
    monkeypatch.setenv("MAX_SESSIONS", "1")
    backend = FakeBackend()

    async def exercise():
        manager = sessions.SessionManager(backend)
        session = await manager.create()
        session.busy = True
        session.last_used -= manager.idle_seconds + 1
        await manager.expire()
        assert session.id in manager.sessions
        with pytest.raises(sessions.HTTPException) as exc:
            await manager.create()
        assert exc.value.status_code == 429 and backend.stopped == []
        session.busy = False
        await manager.expire()
        assert not manager.sessions and backend.stopped == [session.id]

    asyncio.run(exercise())


def test_unconfigured_backend_is_reported_as_service_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(sessions, "_manager", None)

    def unavailable():
        raise RuntimeError("private model diagnostic")

    monkeypatch.setattr(sessions, "SessionManager", unavailable)
    with TestClient(app) as client:
        result = client.post("/opencode-wrapper/api/sessions")
        assert result.status_code == 503 and result.json()["error"]["code"] == "service_unavailable"
        assert "private model diagnostic" not in result.text


@pytest.mark.parametrize("program,timeout,expected", [
    ("import sys; sys.stdout.write('x' * 1000000)", 5, "x" * 20000),
    ("import time; time.sleep(30)", 0.05, None),
])
def test_runner_bounds_reply_and_reaps_timed_out_process(tmp_path, monkeypatch, program, timeout, expected):
    backend = object.__new__(sessions.OpenCodeBackend)
    backend.binary, backend.model = "unused-opencode", "provider/model"
    session = sessions.Session("ses_" + "a" * 32, tmp_path)
    processes = []
    original_spawn = asyncio.create_subprocess_exec

    async def spawn(*args, **kwargs):
        assert kwargs["start_new_session"] is True
        process = await original_spawn(sys.executable, "-c", program, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(sessions.asyncio, "create_subprocess_exec", spawn)

    async def exercise():
        with (tmp_path / "server.log").open("wb") as server_log:
            session.state.update(url="http://unused", project=tmp_path, env={}, server_log=server_log)
            if expected is None:
                with pytest.raises(asyncio.TimeoutError):
                    await backend.send(session, "hello", timeout)
            else:
                assert await backend.send(session, "hello", timeout) == (True, expected)
        assert "runner" not in session.state and processes[0].returncode is not None

    asyncio.run(exercise())
