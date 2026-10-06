"""Serve the dashboard with actual synthetic evidence, without Docker or a model.

    uv run --locked python -m web.demo --port 8000

All banking and configuration state is disposable. Live agent sessions and policy
edits are disabled; the dashboard's test runner still executes local stub-backed tests.
"""
from pathlib import Path
import argparse
import os
import signal
import tempfile

from fastapi.responses import JSONResponse


def create_demo_app(evidence_dir):
    from persistence.http_api import create_app
    from web.main import app as web_app

    evidence_app = create_app(evidence_dir=Path(evidence_dir))

    class Dashboard:
        async def __call__(self, scope, receive, send):
            path = scope.get("path", "")
            if path.startswith("/opencode-wrapper/api/"):
                response = JSONResponse({"error": {
                    "code": "offline_demo", "message": "Live agent sessions are disabled in the offline demo.",
                    "details": {},
                }}, status_code=503)
                await response(scope, receive, send)
                return
            config = (path in {"/api/v1/configs", "/api/v1/config-selection"}
                      or path.startswith("/api/v1/configs/"))
            target = evidence_app if path.startswith("/api/v1") and not config else web_app
            await target(scope, receive, send)

    return Dashboard()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")

    import uvicorn
    from configuration.service import ConfigService
    from persistence.http_api.demo import build_demo

    with tempfile.TemporaryDirectory(prefix="sentinel-demo-") as folder:
        root = Path(folder)
        os.environ["CONFIG_DIR"] = str(root / "config")
        os.environ["RUNS_DIR"] = str(root / "sessions")
        os.environ["CONFIG_ADMIN_TOKEN"] = ""
        ConfigService().snapshot_for_intercept()
        manifest = build_demo(root)
        print(f"Offline dashboard: http://127.0.0.1:{args.port}\n"
              "Real synthetic evidence; no provider calls. State is removed on exit.", flush=True)
        # Uvicorn replays SIGTERM after graceful shutdown. Exit through Python so the
        # temporary workspace is cleaned even when stopped by a process supervisor.
        def terminate(_signal, _frame):
            raise SystemExit(0)

        previous = signal.signal(signal.SIGTERM, terminate)
        try:
            uvicorn.run(create_demo_app(manifest["evidence_dir"]), host="127.0.0.1",
                        port=args.port, access_log=False)
        finally:
            signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    main()
