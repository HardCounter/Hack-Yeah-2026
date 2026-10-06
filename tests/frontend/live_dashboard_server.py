"""Isolated dashboard server backed by actual governed synthetic evidence.

Run with the project's Python, then pass its printed URL to live-dashboard-smoke.mjs.
No provider/model calls are made. All config and banking state lives in a temporary directory.
"""
from pathlib import Path
import os
import socket
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def main():
    import uvicorn
    from configuration.service import ConfigService
    from persistence.http_api import create_app
    from persistence.http_api.demo import build_demo
    from web.main import app as web_app

    with tempfile.TemporaryDirectory(prefix="dashboard-smoke-") as folder:
        root = Path(folder)
        os.environ["CONFIG_DIR"] = str(root / "config")
        os.environ["RUNS_DIR"] = str(root / "sessions")
        os.environ["CONFIG_ADMIN_TOKEN"] = "synthetic-smoke-admin-token"
        os.environ["GIT_SHA"] = "integration-smoke"
        ConfigService().snapshot_for_intercept()
        manifest = build_demo(root)
        evidence_app = create_app(evidence_dir=Path(manifest["evidence_dir"]))

        class Dashboard:
            async def __call__(self, scope, receive, send):
                path = scope.get("path", "")
                config = (path == "/api/v1/config-selection" or path == "/api/v1/configs"
                          or path.startswith("/api/v1/configs/"))
                # Match Caddy's public routing; web owns the lifespan and configuration writes.
                target = evidence_app if path.startswith("/api/v1") and not config else web_app
                await target(scope, receive, send)

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(128)
            url = f"http://127.0.0.1:{listener.getsockname()[1]}"
            print(url, flush=True)
            uvicorn.Server(uvicorn.Config(Dashboard(), access_log=False, log_level="warning")).run(sockets=[listener])


if __name__ == "__main__":
    main()
