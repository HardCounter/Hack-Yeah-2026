"""Run the guardrail suite (tests/control_layer) from the dashboard's Tests tab.

One run at a time: the whole suite, or one case named by an id from the collected list. The
command is fixed; a case id is only ever compared with the collected ids. pytest runs in a
child process; tests/control_layer/conftest.py appends one JSON line per case to the report
file, which the status endpoint reads, so the page can fill cases in while the run is going.
A case has no expected result here: it reports the decisions the control layer made (its
trace) and its status is the last of them.
Every case runs under the config that is selected in the dashboard when the run starts.
The suite uses stub models, so the child gets no API keys.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from configuration.service import ConfigService

ROOT = Path(__file__).resolve().parents[1]
SUITE_PATH = ROOT / "tests" / "control_layer"
TIMEOUT_S = 600
PASSED_ENV = ("PATH", "HOME", "LANG", "TMPDIR", "SYSTEMROOT")

# /api/v1 belongs to the read API (docs/rest.md), served by its own process behind the same host.
router = APIRouter(prefix="/api/suite")
_lock = threading.Lock()
# All three are kept in memory, so a restart shows every case as "not run" again.
_catalogue = None  # case id -> {id, title, area}; collected once per process
_results = {}      # case id -> {state, status, steps, ms} from the latest run that included the case
_run = None        # the latest run


class RunRequest(BaseModel):
    case: str | None = None  # omit to run the whole suite


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _pytest(report, *options, **env):
    child_env = {key: os.environ[key] for key in PASSED_ENV if key in os.environ}
    child_env.update(SUITE_REPORT=str(report), PYTHONDONTWRITEBYTECODE="1", **env)
    command = [sys.executable, "-m", "pytest", str(SUITE_PATH), "-q", "-p", "no:cacheprovider", *options]
    return subprocess.run(command, cwd=ROOT, env=child_env, capture_output=True, text=True, timeout=TIMEOUT_S)


def _rows(report):
    try:
        lines = report.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        try:
            yield json.loads(line)
        except ValueError:
            continue  # a line still being written


def _cases():
    """The suite's cases, collected without running them. Empty if collection fails."""
    global _catalogue
    with _lock:
        if _catalogue is None:
            with tempfile.TemporaryDirectory(prefix="guardrail-suite-") as folder:
                report = Path(folder) / "collected.jsonl"
                try:
                    _pytest(report, "--collect-only")
                except (OSError, subprocess.TimeoutExpired):
                    pass
                _catalogue = {row["id"]: {"id": row["id"], "title": row["title"], "area": row["area"]}
                              for row in _rows(report) if row.get("event") == "case"}
        return _catalogue


def _execute(run):
    started = time.monotonic()
    try:
        # The same snapshot a live session pins when it starts (web/sessions.py).
        selected = ConfigService().snapshot_for_intercept()
        policy = run["report"].with_name("policy.json")
        policy.write_text(json.dumps(selected["config"]), encoding="utf-8")
        with _lock:
            run["config"] = {"name": selected["name"], "revision": selected["revision"]}
        only = {"SUITE_CASE": run["case"]} if run["case"] else {}
        done = _pytest(run["report"], SUITE_POLICY=str(policy), **only)
        # pytest: 0 = all passed, 1 = some failed; anything else means the suite itself did not run
        error = None if done.returncode in (0, 1) else f"the suite did not run (pytest exit code {done.returncode})"
        if error:
            print(done.stdout[-2000:], done.stderr[-2000:], file=sys.stderr)
    except subprocess.TimeoutExpired:
        error = f"the suite was stopped after {TIMEOUT_S} s"
    except Exception as exc:  # the active config could not be read, or pytest could not be started
        error = f"the suite could not be started ({type(exc).__name__})"
    with _lock:
        run.update(finished_at=_now(), duration_ms=round((time.monotonic() - started) * 1000), error=error)


def _status():
    catalogue = _cases()
    with _lock:
        run = dict(_run) if _run else None
        if run:
            for row in _rows(run["report"]):
                if row.get("event") == "result" and row.get("id") in run["ids"]:
                    _results[row["id"]] = {"state": "done", "status": row.get("status"),
                                           "steps": row.get("steps") or [], "ms": row["ms"]}
        results = dict(_results)
    running = bool(run) and not run.get("finished_at")
    # state: not_run, pending or done. status: the final decision (ALLOW, BLOCK, REDACT, REQUIRE_APPROVAL,
    # ALERT), or None when the case made no decision.
    cases = [{**case, "ms": None, "status": None, "steps": [],
              "state": "pending" if running and case["id"] in run["ids"] else "not_run",
              **results.get(case["id"], {})} for case in catalogue.values()]
    if run is None:
        return {"status": "not_run", "cases": cases}
    return {
        "status": "running" if running else "error" if run["error"] else "finished",
        "case": run["case"], "started_at": run["started_at"], "finished_at": run.get("finished_at"),
        "duration_ms": run.get("duration_ms"), "error": run.get("error"), "config": run.get("config"),
        "commit": os.environ.get("GIT_SHA", "dev"), "cases": cases,
    }


@router.post("/runs", status_code=202)
def start_run(request: RunRequest | None = None):
    """Start the suite, or one case, unless a run is already going; either way return the current run."""
    global _run
    case = request.case if request else None
    catalogue = _cases()
    if case is not None and case not in catalogue:
        return JSONResponse(status_code=404, content={"error": {"code": "not_found", "message": "unknown test case"}})
    with _lock:
        if _run is None or _run.get("finished_at"):
            if _run and _run.get("workspace"):
                _run["workspace"].cleanup()
            ids = {case} if case else set(catalogue)
            for case_id in ids:
                _results.pop(case_id, None)
            workspace = tempfile.TemporaryDirectory(prefix="guardrail-suite-")
            report = Path(workspace.name) / "report.jsonl"
            _run = {"workspace": workspace, "report": report, "case": case, "ids": ids, "started_at": _now(), "error": None}
            threading.Thread(target=_execute, args=(_run,), daemon=True).start()
    return _status()


@router.get("/runs/latest")
def latest_run():
    return _status()
