"""One-shot free-agent runs for the frontend.

Each run sends one user message to the OpenCode agent with our plugin loaded. It starts
simulation.opencode_runner --free as a subprocess in its own folder on the data volume.
Only the agent's reply and sanitized evidence are served; logs and databases never leave the server.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

REPO = Path(__file__).resolve().parents[1]
APPLICATIONS = [f"APP-{n:04d}" for n in range(1, 16)]  # the runner accepts APP-0001..APP-0015
RUN_ID = re.compile(r"^run_[0-9a-f]{16}$")
TERMINAL = ("finished", "failed")
MAX_WAITING = 3


DEFAULT_APPLICATION = "APP-0001"  # the gateway needs one assigned case per session
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


class RunRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)
    application: str = Field(default=DEFAULT_APPLICATION, pattern=r"^APP-00(0[1-9]|1[0-5])$")


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _read_jsonl(path):
    try:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, ValueError):
        return []


def _evidence_events(artifacts):
    """Sanitized events from the run's evidence store, plus the gateway's reason code for each decision."""
    from persistence.adapters.consumer_v21 import to_consumer_v21
    from persistence.models import ActionEventEnvelope
    from persistence.privacy import sanitize_event

    events = []
    for db in sorted(artifacts.glob("bank-runs/*.evidence.db")):
        try:
            with sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True) as con:
                rows = con.execute("SELECT payload_json FROM events ORDER BY seq").fetchall()
            for (raw,) in rows:
                event = sanitize_event(ActionEventEnvelope.from_json(raw))
                if event.status.value != "PENDING":
                    wire = to_consumer_v21(event)
                    if "interception_metadata" in wire:
                        wire["interception_metadata"]["reason_code"] = event.context.reason_code
                    events.append(wire)
        except (sqlite3.Error, OSError, ValueError, TypeError):
            continue  # the run is writing; the next poll picks it up
    return events


class RunManager:
    def __init__(self):
        self.root = Path(os.environ.get("RUNS_DIR", "/data/pipeline-runs")).resolve()
        self.command = json.loads(os.environ.get("RUNNER_CMD", "null")) or [
            sys.executable, "-m", "simulation.opencode_runner"]
        self.timeout = int(os.environ.get("RUN_TIMEOUT_S", "300"))
        self.daily_cap = int(os.environ.get("RUNS_DAILY_CAP", "100"))
        self.slot = asyncio.Semaphore(1)  # one agent run at a time
        self.tasks = {}
        self.root.mkdir(parents=True, exist_ok=True)
        # A run left open by a previous process (restart, deploy) can never finish.
        for meta in self._all():
            if meta["status"] not in TERMINAL:
                self._save({**meta, "status": "failed", "detail": "INTERRUPTED", "finished_at": _now()})

    def _dir(self, run_id):
        if not RUN_ID.fullmatch(run_id):
            raise HTTPException(404, "unknown run")
        return self.root / run_id

    def _save(self, meta):
        path = self.root / meta["run_id"] / "meta.json"
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(meta), encoding="utf-8")
        os.replace(temp, path)

    def _all(self):
        metas = [_read_json(p) for p in self.root.glob("run_*/meta.json")]
        return sorted((m for m in metas if m), key=lambda m: m["created_at"], reverse=True)

    def _artifacts(self, run_id):
        # The runner creates one "<application>-<random>" folder inside the run folder.
        found = sorted(p for p in self._dir(run_id).glob("APP-*") if p.is_dir())
        return found[0] if found else None

    def list(self):
        return self._all()[:50]

    def get(self, run_id):
        meta = _read_json(self._dir(run_id) / "meta.json")
        if not meta:
            raise HTTPException(404, "unknown run")
        artifacts = self._artifacts(run_id)
        done = artifacts is not None and meta["status"] in TERMINAL
        verification = _read_json(artifacts / "verification.json") if done else None
        reply = None
        if done:
            try:
                text = (artifacts / "reply.txt").read_text(encoding="utf-8", errors="replace")
                reply = ANSI.sub("", text).strip()[:20000]
            except OSError:
                pass
        return {**meta, "reply": reply, "verification": verification}

    def events(self, run_id):
        meta = self.get(run_id)
        artifacts = self._artifacts(run_id)
        if artifacts is None:
            return {"events": [], "findings": []}
        events = _evidence_events(artifacts)
        if not events and meta["status"] in TERMINAL:
            events = _read_jsonl(artifacts / "events.jsonl")
        return {"events": events, "findings": _read_jsonl(artifacts / "findings.jsonl")}

    def start(self, request: RunRequest):
        runs = self._all()
        if sum(m["status"] not in TERMINAL for m in runs) > MAX_WAITING:
            raise HTTPException(429, "too many runs waiting; try again in a minute")
        if sum(m["created_at"][:10] == _now()[:10] for m in runs) >= self.daily_cap:
            raise HTTPException(429, "daily run limit reached")
        run_id = "run_" + secrets.token_hex(8)
        (self.root / run_id).mkdir()
        # The prompt itself is not stored: run records hold metadata only.
        meta = {"run_id": run_id, "application": request.application, "prompt_chars": len(request.prompt),
                "status": "queued", "created_at": _now(), "started_at": None, "finished_at": None,
                "exit_code": None, "detail": None}
        self._save(meta)
        self.tasks[run_id] = asyncio.create_task(self._execute(meta, request.prompt))
        return meta

    async def _execute(self, meta, prompt):
        folder = self.root / meta["run_id"]
        command = [*self.command, meta["application"], "--free", "--output-dir", str(folder),
                   "--timeout", str(self.timeout), "--prompt", prompt]
        try:
            async with self.slot:
                meta = {**meta, "status": "running", "started_at": _now()}
                self._save(meta)
                with open(folder / "runner.log", "wb") as log:
                    process = await asyncio.create_subprocess_exec(*command, cwd=REPO, stdout=log, stderr=log)
                    try:
                        code = await asyncio.wait_for(process.wait(), self.timeout + 120)
                    except asyncio.TimeoutError:
                        process.kill()
                        await process.wait()
                        code = 124
            # Runner exit codes: 0 or 2 mean the agent finished (2: the assigned case was not completed,
            # which is normal for a free-form message); 1 means the run itself failed.
            status, detail = ("finished", None) if code in (0, 2) else ("failed", "TIMEOUT" if code == 124 else "RUNNER_FAILED")
        except OSError:
            code, status, detail = None, "failed", "RUNNER_NOT_STARTED"
        self._save({**meta, "status": status, "detail": detail, "exit_code": code, "finished_at": _now()})
        self.tasks.pop(meta["run_id"], None)

    async def stream(self, run_id):
        sent = 0
        while True:
            run = self.get(run_id)
            data = self.events(run_id)
            for event in data["events"][sent:]:
                yield f"event: action\ndata: {json.dumps(event)}\n\n"
            sent = max(sent, len(data["events"]))
            if run["status"] in TERMINAL:
                yield f"event: result\ndata: {json.dumps({**run, 'findings': data['findings']})}\n\n"
                return
            yield f"event: status\ndata: {json.dumps({'status': run['status']})}\n\n"
            await asyncio.sleep(1)


_manager = None


def manager() -> RunManager:
    global _manager
    if _manager is None:
        _manager = RunManager()
    return _manager


router = APIRouter(prefix="/api/v1")


@router.get("/info")
async def info():
    return {"model": os.environ.get("OPENCODE_MODEL"), "application": DEFAULT_APPLICATION}


@router.get("/applications")
async def applications():
    return {"applications": APPLICATIONS}


@router.post("/runs", status_code=202)
async def start_run(request: RunRequest):
    return manager().start(request)


@router.get("/runs")
async def list_runs():
    return {"runs": manager().list()}


@router.get("/runs/{run_id}")
async def get_run(run_id: str):
    return manager().get(run_id)


@router.get("/runs/{run_id}/events")
async def run_events(run_id: str):
    return manager().events(run_id)


@router.get("/runs/{run_id}/stream")
async def run_stream(run_id: str):
    manager().get(run_id)  # 404 before the stream starts
    return StreamingResponse(manager().stream(run_id), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
