"""The offline dashboard serves real evidence while keeping provider dispatch disabled."""
from fastapi.testclient import TestClient
import signal
import pytest

from persistence.http_api.demo import build_demo
from web.demo import create_demo_app


def test_offline_dashboard_reads_evidence_and_disables_live_mutations(tmp_path, monkeypatch):
    from web import sessions
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CONFIG_ADMIN_TOKEN", "")

    def forbidden():
        raise AssertionError("offline dashboard must never start a provider-backed session")

    monkeypatch.setattr(sessions, "SessionManager", forbidden)
    manifest = build_demo(tmp_path / "evidence")
    with TestClient(create_demo_app(manifest["evidence_dir"])) as client:
        assert client.get("/").status_code == 200
        assert client.get("/healthz").json()["status"] == "ok"
        assert client.get("/api/v1/health").json()["status"] == "ok"
        response = client.get(f"/api/v1/sessions/{manifest['allowed_session']}/verification")
        assert response.status_code == 200
        assert "VERIFIED_SUCCESS" in response.text
        assert client.get("/api/v1/sessions").status_code == 200
        assert client.post("/opencode-wrapper/api/sessions").json()["error"]["code"] == "offline_demo"
        config = client.get("/api/v1/configs/standard").json()
        assert client.put("/api/v1/configs/standard", json=config).status_code == 503


def test_offline_demo_cleans_workspace_on_supervisor_shutdown(monkeypatch):
    import uvicorn
    from web import demo
    from persistence.http_api import demo as evidence_demo
    roots = []

    def build(root):
        roots.append(root)
        return {"evidence_dir": root / "evidence"}

    def shutdown(*args, **kwargs):
        assert kwargs["host"] == "127.0.0.1"
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)

    for name in ("CONFIG_DIR", "RUNS_DIR", "CONFIG_ADMIN_TOKEN"):
        monkeypatch.setenv(name, "disposable-test-value")
    monkeypatch.setattr(evidence_demo, "build_demo", build)
    monkeypatch.setattr(demo, "create_demo_app", lambda _: object())
    monkeypatch.setattr(uvicorn, "run", shutdown)
    original = signal.getsignal(signal.SIGTERM)
    with pytest.raises(SystemExit) as stopped:
        demo.main([])
    assert stopped.value.code == 0
    assert roots and not roots[0].exists()
    assert signal.getsignal(signal.SIGTERM) == original
