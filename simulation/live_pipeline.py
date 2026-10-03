"""Start the complete governed pipeline and wait for an interactive OpenCode session to connect.

    uv run python -m simulation.live_pipeline APP-0001 [--model provider/model] [--port 8080]

Starts `intercept.service.local` (Layer 1 gateway + Layer 2 evidence + Layer 3 consumers) on a fresh
synthetic bank, traces every step to a file (CONTROL_LOG=file), prepares an isolated OpenCode project
whose plugin exposes only gateway-backed tools, and writes the connection details to the private
var/intercept.env read by scripts/run_opencode_intercepted.sh. Ctrl+C finishes the bound session,
runs the independent outcome verification, and exports artifacts to the run directory.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ENV_FILE = REPO / "var" / "intercept.env"


def _port_free(port: int) -> bool:
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _default_model() -> str | None:
    try:
        return json.loads((REPO / "opencode.json").read_text(encoding="utf-8")).get("model")
    except (OSError, ValueError):
        return None


def _private_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write(text)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("application", nargs="?", default="APP-0001")
    parser.add_argument("--model", default=_default_model(),
                        help="provider/model OpenCode should use (default: model in the repo opencode.json)")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--bank-db", type=Path, help="existing synthetic bank (default: generate a fresh one)")
    parser.add_argument("--runs-dir", type=Path, default=REPO / "var" / "live-runs")
    args = parser.parse_args(argv)
    if not args.model:
        parser.error("--model is required (no model in the repository opencode.json)")
    if not _port_free(args.port):
        parser.error(f"port {args.port} is in use; pass --port")

    from simulation.pipeline_support import discover_bound_session, export_run, prepare_project, request

    run_dir = args.runs_dir.resolve() / f"{args.application}-{datetime.now():%Y%m%d-%H%M%S}"
    bank_runs = run_dir / "bank-runs"
    bank_runs.mkdir(parents=True)
    trace_log = run_dir / "control-layer.log"
    contract_id = f'contract_{args.application.replace("-", "")}_{secrets.token_hex(6)}'
    token, admin = secrets.token_hex(32), secrets.token_hex(32)
    endpoint = f"http://127.0.0.1:{args.port}"
    # prepare_project requires a directory outside the repository (no repo context for the agent).
    project = Path(tempfile.mkdtemp(prefix=f"opencode-{args.application}-"))

    if args.bank_db:
        bank = args.bank_db.resolve()
    else:
        from simulation import agent  # noqa: F401  (sets up the data/ import path)
        import generate
        generate.build(run_dir / "dataset")
        bank = run_dir / "dataset" / "bank.db"
    prepare_project(project, REPO, args.application, contract_id, args.model, endpoint)

    env = {**os.environ, "INTERCEPT_TOKEN": token, "INTERCEPT_ADMIN_TOKEN": admin,
           "CONTROL_LOG": "file", "CONTROL_LOG_FILE": str(trace_log)}
    gateway_out = open(run_dir / "gateway.log", "w", encoding="utf-8")
    gateway = subprocess.Popen(
        [sys.executable, "-m", "intercept.service.local", "--bank-db", str(bank), "--application",
         args.application, "--contract-id", contract_id, "--runs-dir", str(bank_runs), "--port", str(args.port)],
        cwd=REPO, env=env, stdout=gateway_out, stderr=subprocess.STDOUT, start_new_session=True)
    status = 1
    try:
        deadline = time.monotonic() + 30
        while True:
            if gateway.poll() is not None:
                print(f"error: gateway exited; see {run_dir / 'gateway.log'}", file=sys.stderr)
                return 1
            try:
                request(endpoint, "/v1/tools/catalog", {}, token)
                break
            except RuntimeError:
                if time.monotonic() > deadline:
                    print("error: gateway did not become ready", file=sys.stderr)
                    return 1
                time.sleep(0.2)
        _private_write(ENV_FILE, f"INTERCEPT_TOKEN={token}\nINTERCEPT_ADMIN_TOKEN={admin}\n"
                                 f"INTERCEPT_PORT={args.port}\nINTERCEPT_DEMO_DIR={project}\n")
        print(f"""================================================================================
 Control layer is running: gateway -> evidence store -> consumers (application {args.application})
================================================================================
 Trace (one JSON line per step):  tail -f {trace_log}
 Terminal 2:                       scripts/run_opencode_intercepted.sh
 In OpenCode (new session):        /intercept-run Process application {args.application}

 OpenCode only sees the governed KYC tools; each call is decided, persisted and analysed
 by the control layer. Press Ctrl+C here when the agent is done to finish the session,
 run the independent outcome verification and save artifacts to:
   {run_dir}
================================================================================""", flush=True)
        try:
            while gateway.poll() is None:
                time.sleep(0.5)
            print("error: gateway stopped unexpectedly", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            print("\nFinishing...", flush=True)
        session_id = discover_bound_session(bank_runs, contract_id, args.application)
        if session_id is None:
            print("No session was bound (was /intercept-run used?); nothing to verify.")
            return 2
        result = request(endpoint, "/v1/session/finish", {"session_id": session_id}, token, timeout=30)
        export_run(bank_runs, session_id, run_dir)
        _private_write(run_dir / "verification.json", json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2))
        print(f"Trace: {trace_log}\nArtifacts: {run_dir}")
        status = 0 if result.get("verification_status") == "VERIFIED_SUCCESS" else 2
        return status
    finally:
        if gateway.poll() is None:
            os.killpg(gateway.pid, signal.SIGTERM)
            try:
                gateway.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(gateway.pid, signal.SIGKILL)
        gateway_out.close()
        ENV_FILE.unlink(missing_ok=True)  # tokens are only valid for this run
        shutil.rmtree(project, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
