"""Free-agent sessions for the OpenCode wrapper page.

A session is one long-lived OpenCode server plus one control gateway, both started when the page
opens. Messages go to the running server, so only the first one pays the start-up cost. All sessions
start from the same pre-built synthetic bank; the gateway gives each session its own copy.
Only the agent's reply and sanitized evidence are served; logs and databases never leave the server.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import sqlite3
import sys
import time

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field

REPO = Path(__file__).resolve().parents[1]
APPLICATION = "APP-0001"  # the gateway needs one assigned case per session
AGENT = "onboarding-agent"
SESSION_ID = re.compile(r"^ses_[0-9a-f]{32}$")
log = logging.getLogger("uvicorn.error")
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


class MessageRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)


class Session:
    def __init__(self, session_id, folder):
        self.id = session_id
        self.folder = folder
        self.created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.last_used = time.monotonic()
        self.order = 0  # recency rank; clock ticks are too coarse to order on
        self.busy = False
        self.messages = 0
        self.state = {}  # backend-owned: processes, ports, environment


def _evidence(db, session_id):
    """Sanitized events (with the gateway's reason code) and findings from a session's evidence store."""
    from persistence.adapters.consumer_v21 import to_consumer_v21
    from persistence.models import ActionEventEnvelope
    from persistence.privacy import sanitize_event

    events, findings = [], []
    try:
        with sqlite3.connect(Path(db).resolve().as_uri() + "?mode=ro", uri=True) as con:
            rows = con.execute("SELECT payload_json FROM events WHERE session_id=? ORDER BY seq", (session_id,)).fetchall()
            for (raw,) in rows:
                event = sanitize_event(ActionEventEnvelope.from_json(raw))
                if event.status.value != "PENDING":
                    wire = to_consumer_v21(event)
                    if "interception_metadata" in wire:
                        wire["interception_metadata"]["reason_code"] = event.context.reason_code
                    events.append(wire)
            for finding_id, raw in con.execute(
                    "SELECT finding_id,payload_json FROM consumer_findings WHERE session_id=? ORDER BY rowid", (session_id,)):
                findings.append({"finding_id": finding_id, **json.loads(raw)})
    except (sqlite3.Error, OSError, ValueError, TypeError):
        pass  # the session is writing or has no evidence yet; the next poll picks it up
    return events, findings


def _kill(process):
    """Stop a process group we started (the OpenCode server spawns children)."""
    if process is None or process.returncode is not None:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        time.sleep(0.2)


class OpenCodeBackend:
    """Real sessions: intercept.service.local as the gateway and `opencode serve` as the agent server."""

    def __init__(self, root):
        from simulation.opencode_runner import opencode_binary
        self.root = root
        self.binary = opencode_binary(os.environ.get("OPENCODE_BIN"))
        self.model = os.environ.get("OPENCODE_MODEL") or ""
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[^\s]+", self.model):
            raise RuntimeError("OPENCODE_MODEL must be set as provider/model")
        self.bank = None

    def _build_bank(self):
        from simulation import agent  # noqa: F401  sets up the data import path
        import generate
        dataset = self.root / "_bank" / secrets.token_hex(4)
        dataset.parent.mkdir(parents=True, exist_ok=True)
        generate.build(dataset)
        return dataset / "bank.db"

    async def start(self, session):
        from simulation.opencode_runner import _child_env, free_port
        from simulation.pipeline_support import prepare_project, request
        if self.bank is None:  # one shared synthetic bank per app start; the gateway copies it per session
            self.bank = await asyncio.to_thread(self._build_bank)
        folder = session.folder
        contract = f"contract_{APPLICATION.replace('-', '')}_{secrets.token_hex(8)}"
        token, admin = secrets.token_hex(32), secrets.token_hex(32)
        endpoint = f"http://127.0.0.1:{free_port()}"
        project = folder / "project"
        prepare_project(project, REPO, APPLICATION, contract, self.model, endpoint, free=True)
        env = _child_env(folder / "config", token, admin)
        for name in ("HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):  # isolate OpenCode's own storage
            env[name] = str(folder / name.lower())
            Path(env[name]).mkdir(parents=True, exist_ok=True)
        env["PWD"] = str(project)
        env["OPENCODE_SERVER_PASSWORD"] = secrets.token_urlsafe(24)
        state = session.state
        state.update(env=env, project=project, evidence=folder / "bank-runs" / f"{session.id}.evidence.db")
        state["gateway_log"] = open(folder / "gateway.log", "wb")
        state["gateway"] = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "intercept.service.local", "--bank-db", str(self.bank), "--application", APPLICATION,
            "--contract-id", contract, "--runs-dir", str(folder / "bank-runs"), "--port", endpoint.rsplit(":", 1)[1],
            "--catalog-all", cwd=REPO, env=env, stdout=state["gateway_log"], stderr=asyncio.subprocess.STDOUT,
            start_new_session=True)
        port = free_port()
        state["server_log"] = open(folder / "opencode.log", "wb")
        state["server"] = await asyncio.create_subprocess_exec(
            self.binary, "serve", "--hostname", "127.0.0.1", "--port", str(port), cwd=project, env=env,
            stdin=asyncio.subprocess.DEVNULL, stdout=state["server_log"], stderr=asyncio.subprocess.STDOUT,
            start_new_session=True)
        state["url"] = f"http://127.0.0.1:{port}"

        def wait_ready():
            deadline = time.monotonic() + 30
            while True:
                try:
                    request(endpoint, "/v1/tools/catalog", {}, token)
                    socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                    break
                except (OSError, ValueError, RuntimeError):
                    if time.monotonic() > deadline:
                        raise RuntimeError("session start timed out") from None
                    time.sleep(0.05)
            # The trusted backend binds the session to its contract; the model never chooses identity.
            request(endpoint, "/v1/runs/bind", {"session_id": session.id, "contract_id": contract}, admin)

        await asyncio.to_thread(wait_ready)

    async def send(self, session, prompt, timeout):
        state = session.state
        process = await asyncio.create_subprocess_exec(
            self.binary, "run", "--server", state["url"], "--auto", "--session", session.id, "--agent", AGENT,
            "--model", self.model, prompt, cwd=state["project"], env=state["env"],
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=state["server_log"])
        try:
            out, _ = await asyncio.wait_for(process.communicate(), timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return False, ""
        return process.returncode == 0, ANSI.sub("", out.decode("utf-8", errors="replace")).strip()[:20000]

    def evidence(self, session):
        return _evidence(session.state["evidence"], session.id)

    async def stop(self, session):
        for name in ("server", "gateway"):
            await asyncio.to_thread(_kill, session.state.get(name))
        for name in ("server_log", "gateway_log"):
            if session.state.get(name):
                session.state[name].close()


class SessionManager:
    def __init__(self, backend=None):
        self.root = Path(os.environ.get("RUNS_DIR", "/data/pipeline-runs")).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_sessions = int(os.environ.get("MAX_SESSIONS", "6"))
        self.idle_seconds = int(os.environ.get("SESSION_IDLE_S", "900"))
        self.message_timeout = int(os.environ.get("RUN_TIMEOUT_S", "120"))
        self.daily_cap = int(os.environ.get("RUNS_DAILY_CAP", "500"))
        self.backend = backend or OpenCodeBackend(self.root)
        self.sessions = {}
        self.day, self.sent_today = None, 0
        self.clock = 0

    def _touch(self, session):
        self.clock += 1
        session.order, session.last_used = self.clock, time.monotonic()

    def get(self, session_id) -> Session:
        session = self.sessions.get(session_id) if SESSION_ID.fullmatch(session_id) else None
        if session is None:
            raise HTTPException(404, "unknown or expired session")
        return session

    async def close(self, session_id):
        session = self.sessions.pop(session_id, None)
        if session is not None:
            await self.backend.stop(session)

    async def create(self):
        now = time.monotonic()
        for old in [s for s in self.sessions.values() if not s.busy and now - s.last_used > self.idle_seconds]:
            await self.close(old.id)
        if len(self.sessions) >= self.max_sessions:
            idle = sorted((s for s in self.sessions.values() if not s.busy), key=lambda s: s.order)
            if not idle:
                raise HTTPException(429, "all session slots are busy; try again in a minute")
            await self.close(idle[0].id)  # the least recently used session makes room
        session_id = "ses_" + secrets.token_hex(16)
        folder = self.root / session_id
        folder.mkdir()
        session = Session(session_id, folder)
        self._touch(session)
        self.sessions[session_id] = session
        try:
            await self.backend.start(session)
        except Exception:
            log.exception("session start failed")
            await self.close(session_id)
            raise HTTPException(503, "the session could not be started") from None
        return session

    async def send(self, session_id, prompt):
        session = self.get(session_id)
        if session.busy:
            raise HTTPException(409, "the agent is still working on the previous message")
        today = datetime.now(timezone.utc).date()
        if today != self.day:
            self.day, self.sent_today = today, 0
        if self.sent_today >= self.daily_cap:
            raise HTTPException(429, "daily message limit reached")
        self.sent_today += 1
        session.busy = True
        try:
            ok, reply = await self.backend.send(session, prompt, self.message_timeout)
        finally:
            session.busy = False
            self._touch(session)
        session.messages += 1
        events, findings = self.backend.evidence(session)
        return {"ok": ok, "reply": reply, "events": events, "findings": findings}

    def evidence(self, session_id):
        session = self.get(session_id)
        events, findings = self.backend.evidence(session)
        return {"events": events, "findings": findings, "busy": session.busy}

    async def shutdown(self):
        for session_id in list(self.sessions):
            await self.close(session_id)


_manager = None


def manager() -> SessionManager:
    global _manager
    if _manager is None:
        _manager = SessionManager()
    return _manager


async def shutdown():
    if _manager is not None:
        await _manager.shutdown()


router = APIRouter(prefix="/api/v1")


@router.get("/info")
async def info():
    return {"model": os.environ.get("OPENCODE_MODEL"), "application": APPLICATION}


@router.post("/sessions", status_code=201)
async def create_session():
    session = await manager().create()
    return {"session_id": session.id, "created_at": session.created_at}


@router.post("/sessions/{session_id}/messages")
async def send_message(session_id: str, request: MessageRequest):
    return await manager().send(session_id, request.prompt)


@router.get("/sessions/{session_id}/events")
async def session_events(session_id: str):
    return manager().evidence(session_id)


@router.delete("/sessions/{session_id}", status_code=204)
async def close_session(session_id: str):
    await manager().close(session_id)
    return Response(status_code=204)
