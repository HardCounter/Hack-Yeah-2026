"""Start the complete governed pipeline and wait for an interactive OpenCode session to connect.

    uv run python -m simulation.live_pipeline APP-0001 [--model provider/model] [--port 8080] [--api-port 8790]

Starts two processes on a fresh synthetic bank:
  * `intercept.service.local`: Layer 1 gateway. Binding a session (/intercept-run) composes the
    governed runtime: Layer 2 evidence store `bank-runs/<session_id>.evidence.db` and the Layer 3
    consume plane (trajectory-risk, outcome verifier, feedback to Layer 1).
  * `persistence.http_api`: the read-only REST API for dashboards (docs/rest.md) over `bank-runs/`.
Every step is traced to a file (CONTROL_LOG=file). An isolated OpenCode project exposes only
gateway-backed tools. Gateway connection details go to the private var/intercept.env read by
scripts/run_opencode_intercepted.sh. The REST API has no authentication and listens on loopback only.
Ctrl+C finishes the bound session, runs the independent outcome verification, and exports artifacts
to the run directory.
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


def _env_owner_port() -> int | None:
    """Port recorded in var/intercept.env by a running pipeline or receiver, if any."""
    try:
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if line.startswith("INTERCEPT_PORT="):
                return int(line.split("=", 1)[1])
    except (OSError, ValueError):
        pass
    return None


def _release_env_file(token: str) -> None:
    """Delete var/intercept.env only if it is still ours (never another run's connection details)."""
    try:
        if f"INTERCEPT_TOKEN={token}\n" in ENV_FILE.read_text(encoding="utf-8"):
            ENV_FILE.unlink()
    except OSError:
        pass


def _api_ready(base: str) -> bool:
    """Liveness plus one data read, so a broken app fails here and not in the dashboard."""
    from urllib.error import URLError
    from urllib.request import urlopen
    try:
        with urlopen(f"{base}/health", timeout=2) as response:
            if json.load(response).get("status") != "ok":
                return False
        with urlopen(f"{base}/system/stats", timeout=2) as response:
            return response.status == 200
    except (URLError, OSError, ValueError):
        return False


def _stop(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)


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
    parser.add_argument("--api-port", type=int, default=8790, help="port of the read-only REST API")
    parser.add_argument("--no-api", action="store_true", help="do not start the REST API")
    parser.add_argument("--cors-origin", action="append", default=[],
                        help="dashboard origin allowed to call the REST API from a browser; repeatable")
    args = parser.parse_args(argv)
    if not args.model:
        parser.error("--model is required (no model in the repository opencode.json)")
    if not _port_free(args.port):
        parser.error(f"port {args.port} is in use; pass --port")
    owner = _env_owner_port()
    if owner is not None and not _port_free(owner):
        parser.error(f"another pipeline/receiver is running (port {owner}, {ENV_FILE}); stop it first")
    if not args.no_api and (args.api_port == args.port or not _port_free(args.api_port)):
        parser.error(f"REST API port {args.api_port} is in use; pass --api-port or --no-api")

    from simulation.pipeline_support import discover_bound_session, export_run, prepare_project, request

    run_dir = args.runs_dir.resolve() / f"{args.application}-{datetime.now():%Y%m%d-%H%M%S}"
    bank_runs = run_dir / "bank-runs"
    bank_runs.mkdir(parents=True)
    trace_log = run_dir / "control-layer.log"
    contract_id = f'contract_{args.application.replace("-", "")}_{secrets.token_hex(6)}'
    token, admin = secrets.token_hex(32), secrets.token_hex(32)
    endpoint = f"http://127.0.0.1:{args.port}"
    api_base = f"http://127.0.0.1:{args.api_port}/api/v1"
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
    # Management credentials belong to the REST API only, never the gateway/agent process.
    gateway_env = {k: v for k, v in env.items() if k != "CONFIG_ADMIN_TOKEN"}
    gateway_out = open(run_dir / "gateway.log", "w", encoding="utf-8")
    gateway = subprocess.Popen(
        [sys.executable, "-m", "intercept.service.local", "--bank-db", str(bank), "--application",
         args.application, "--contract-id", contract_id, "--runs-dir", str(bank_runs), "--port", str(args.port)],
        cwd=REPO, env=gateway_env, stdout=gateway_out, stderr=subprocess.STDOUT, start_new_session=True)
    api, api_out = None, None
    status = 1
    try:
        if not args.no_api:
            # Separate process; it never needs the gateway credentials, so they are not passed on.
            api_env = {k: v for k, v in env.items() if not k.startswith("INTERCEPT_")}
            api_out = open(run_dir / "read-api.log", "w", encoding="utf-8")
            api = subprocess.Popen(
                [sys.executable, "-m", "persistence.http_api", "--evidence-dir", str(bank_runs),
                 "--port", str(args.api_port), *[a for o in args.cors_origin for a in ("--cors-origin", o)]],
                cwd=REPO, env=api_env, stdout=api_out, stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + 30
        gateway_up, api_up = False, api is None
        while not (gateway_up and api_up):
            if gateway.poll() is not None:
                print(f"error: gateway exited; see {run_dir / 'gateway.log'}", file=sys.stderr)
                return 1
            if api is not None and api.poll() is not None:
                print(f"error: REST API exited; see {run_dir / 'read-api.log'}", file=sys.stderr)
                return 1
            if not gateway_up:
                try:
                    request(endpoint, "/v1/tools/catalog", {}, token)
                    gateway_up = True
                except RuntimeError:
                    pass
            if not api_up:
                api_up = _api_ready(api_base)
            if not (gateway_up and api_up):
                if time.monotonic() > deadline:
                    print("error: " + ("gateway" if not gateway_up else "REST API") + " did not become ready",
                          file=sys.stderr)
                    return 1
                time.sleep(0.2)
        _private_write(ENV_FILE, f"INTERCEPT_TOKEN={token}\nINTERCEPT_ADMIN_TOKEN={admin}\n"
                                 f"INTERCEPT_PORT={args.port}\nINTERCEPT_DEMO_DIR={project}\n")
        api_line = (f" REST API (dashboards):           {api_base}   (docs: {api_base}/docs)\n"
                    f"   query it:  curl {api_base}/sessions\n"
                    if api is not None else " REST API:                        disabled (--no-api)\n")
        print(f"""================================================================================
 Control layer is running (application {args.application}):
   Layer 1 gateway  ->  Layer 2 evidence store  ->  Layer 3 consume plane  ->  REST API
   The evidence store and consumers are created when /intercept-run binds the session:
   {bank_runs}/<session_id>.evidence.db
================================================================================
 Trace (one JSON line per step):  tail -f {trace_log}
{api_line}
 Terminal 2:                       scripts/run_opencode_intercepted.sh
 In OpenCode (new session):        /intercept-run Process application {args.application}

 OpenCode only sees the governed KYC tools; each call is decided, persisted and analysed
 by the control layer. Press Ctrl+C here when the agent is done to finish the session,
 run the independent outcome verification and save artifacts to:
   {run_dir}
================================================================================""", flush=True)
        try:
            api_warned = False
            while gateway.poll() is None:
                if api is not None and api.poll() is not None and not api_warned:
                    # The control layer keeps enforcing; only the dashboard view is lost.
                    print(f"warning: REST API stopped; see {run_dir / 'read-api.log'}", file=sys.stderr)
                    api_warned = True
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
        _stop(gateway)
        _stop(api)
        gateway_out.close()
        if api_out is not None:
            api_out.close()
        _release_env_file(token)  # tokens are only valid for this run
        shutil.rmtree(project, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
