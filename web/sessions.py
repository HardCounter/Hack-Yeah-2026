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
from web.security import positive_int

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


async def _kill(process):
    """Stop a process group we started (the OpenCode server spawns children)."""
    if process is None:
        return
    for gentle in (True, False):
        sig = signal.SIGTERM if gentle else getattr(signal, "SIGKILL", signal.SIGTERM)
        try:
            if hasattr(os, "killpg"):
                if process.returncode is not None:
                    try:
                        os.getpgid(process.pid)
                    except ProcessLookupError:
                        pass  # an orphan group can survive its leader
                    else:
                        return  # the reaped leader's PID now belongs to another process
                os.killpg(process.pid, sig)
            elif process.returncode is None:
                process.terminate() if gentle else process.kill()
        except ProcessLookupError:
            break
        except PermissionError:
            if process.returncode is None:
                process.terminate() if gentle else process.kill()
        if gentle:
            # Always kill the group after the grace period, even if its leader has exited.
            await asyncio.sleep(0.2)
    try:
        await asyncio.wait_for(process.wait(), 2)
    except asyncio.TimeoutError:
        log.error("child process did not exit after group termination")


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
        # One folder for every session's evidence store: the read API serves this directory.
        self.runs = root / "bank-runs"
        self.runs.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.runs.chmod(0o700)

    def _build_bank(self):
        from simulation import agent  # noqa: F401  sets up the data import path
        import generate
        dataset = self.root / "_bank" / secrets.token_hex(4)
        dataset.parent.mkdir(parents=True, exist_ok=True)
        generate.build(dataset)
        return dataset / "bank.db"

    async def start(self, session):
        from simulation.opencode_runner import _child_env, _log, free_port
        from simulation.pipeline_support import prepare_project, request
        if self.bank is None:  # one shared synthetic bank per app start; the gateway copies it per session
            self.bank = await asyncio.to_thread(self._build_bank)
        folder = session.folder
        contract = f"contract_{APPLICATION.replace('-', '')}_{secrets.token_hex(8)}"
        token, admin = secrets.token_hex(32), secrets.token_hex(32)
        endpoint = f"http://127.0.0.1:{free_port()}"
        project = folder / "project"
        from configuration.service import ConfigService
        config_service = ConfigService()
        snapshot = await asyncio.to_thread(config_service.snapshot_for_intercept)
        policy_path = folder / "selected-policy.json"
        policy_path.write_text(json.dumps(snapshot["config"], indent=2) + "\n", encoding="utf-8")
        policy_path.chmod(0o600)
        prepare_project(project, REPO, APPLICATION, contract, self.model, endpoint, free=True, prompts="enforce",
                        policy_config=snapshot["config"])
        env = _child_env(folder / "config", token, admin)
        env.pop("CONFIG_ADMIN_TOKEN", None)  # policy administration belongs to the web service
        # Let the gateway read the operator-owned config directory, not a session workspace.
        env["CONFIG_DIR"] = str(config_service.directory)
        for name in ("HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):  # isolate OpenCode's own storage
            env[name] = str(folder / name.lower())
            Path(env[name]).mkdir(parents=True, exist_ok=True)
        env["PWD"] = str(project)
        env["OPENCODE_SERVER_PASSWORD"] = secrets.token_urlsafe(24)
        state = session.state
        env["CONTROL_LOG"], env["CONTROL_LOG_FILE"] = "file", str(folder / "control-layer.log")
        state.update(env=env, project=project, evidence=self.runs / f"{session.id}.evidence.db")
        state["policy_snapshot"] = snapshot
        state["gateway_log"] = _log(folder, "gateway.log")
        state["gateway"] = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "intercept.service.local", "--bank-db", str(self.bank), "--application", APPLICATION,
            "--contract-id", contract, "--runs-dir", str(self.runs), "--port", endpoint.rsplit(":", 1)[1],
            "--policy", str(policy_path), "--policy-revision", snapshot["revision"].removeprefix("sha256:"),
            "--catalog-all", cwd=REPO, env=env, stdout=state["gateway_log"], stderr=asyncio.subprocess.STDOUT,
            start_new_session=True)
        port = free_port()
        state["server_log"] = _log(folder, "opencode.log")
        state["server"] = await asyncio.create_subprocess_exec(
            self.binary, "serve", "--hostname", "127.0.0.1", "--port", str(port), cwd=project, env=env,
            stdin=asyncio.subprocess.DEVNULL, stdout=state["server_log"], stderr=asyncio.subprocess.STDOUT,
            start_new_session=True)
        state["url"] = f"http://127.0.0.1:{port}"
        gateway, server = state["gateway"], state["server"]

        def wait_ready():
            deadline = time.monotonic() + min(30, positive_int("SESSION_START_TIMEOUT_S", 45))
            while True:
                if gateway.returncode is not None or server.returncode is not None:
                    raise RuntimeError("session process exited during startup")
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
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=state["server_log"],
            start_new_session=True)
        state["runner"] = process

        async def reply():
            # Drain stdout while retaining a bounded reply; a noisy agent cannot exhaust RAM.
            kept = bytearray()
            while chunk := await process.stdout.read(8192):
                if len(kept) < 80000:
                    kept.extend(chunk[:80000 - len(kept)])
            await process.wait()
            return bytes(kept)

        try:
            out = await asyncio.wait_for(reply(), timeout)
        finally:
            await _kill(process)
            state.pop("runner", None)
        return process.returncode == 0, ANSI.sub("", out.decode("utf-8", errors="replace")).strip()[:20000]

    def evidence(self, session):
        return _evidence(session.state["evidence"], session.id)

    async def stop(self, session):
        for name in ("runner", "server", "gateway"):
            await _kill(session.state.pop(name, None))
        for name in ("server_log", "gateway_log"):
            if session.state.get(name):
                session.state.pop(name).close()


class SessionManager:
    def __init__(self, backend=None):
        self.root = Path(os.environ.get("RUNS_DIR", "/data/pipeline-runs")).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.max_sessions = positive_int("MAX_SESSIONS", 6)
        self.idle_seconds = positive_int("SESSION_IDLE_S", 900)
        self.message_timeout = positive_int("RUN_TIMEOUT_S", 120)
        self.start_timeout = positive_int("SESSION_START_TIMEOUT_S", 45)
        self.daily_cap = positive_int("RUNS_DAILY_CAP", 500)
        self.backend = backend or OpenCodeBackend(self.root)
        self.sessions = {}
        self.day, self.sent_today = None, 0
        self.clock = 0
        self.lifecycle_lock = asyncio.Lock()

    def _touch(self, session):
        self.clock += 1
        session.order, session.last_used = self.clock, time.monotonic()

    def get(self, session_id) -> Session:
        session = self.sessions.get(session_id) if SESSION_ID.fullmatch(session_id) else None
        if session is None:
            raise HTTPException(404, "unknown or expired session")
        return session

    async def close(self, session_id):
        async with self.lifecycle_lock:
            await self._close(session_id)

    async def _close(self, session_id):
        session = self.sessions.pop(session_id, None)
        if session is not None:
            await self.backend.stop(session)

    async def expire(self):
        async with self.lifecycle_lock:
            now = time.monotonic()
            for old in [s for s in self.sessions.values() if not s.busy and now - s.last_used > self.idle_seconds]:
                await self._close(old.id)

    async def create(self):
        async with self.lifecycle_lock:
            return await self._create()

    async def _create(self):
        now = time.monotonic()
        for old in [s for s in self.sessions.values() if not s.busy and now - s.last_used > self.idle_seconds]:
            await self._close(old.id)
        if len(self.sessions) >= self.max_sessions:
            idle = sorted((s for s in self.sessions.values() if not s.busy), key=lambda s: s.order)
            if not idle:
                raise HTTPException(429, "all session slots are busy; try again in a minute")
            await self._close(idle[0].id)  # the least recently used session makes room
        session_id = "ses_" + secrets.token_hex(16)
        folder = self.root / session_id
        folder.mkdir(mode=0o700)
        session = Session(session_id, folder)
        session.busy = True
        self._touch(session)
        self.sessions[session_id] = session
        try:
            await asyncio.wait_for(self.backend.start(session), self.start_timeout)
        except asyncio.CancelledError:
            await self._close(session_id)
            raise
        except Exception as exc:
            log.error("session start failed: %s", type(exc).__name__)
            await self._close(session_id)
            raise HTTPException(503, "the session could not be started") from None
        session.busy = False
        self._touch(session)
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
            ok, reply = await asyncio.wait_for(self.backend.send(session, prompt, self.message_timeout),
                                              self.message_timeout + 3)
            if not ok:
                await self.close(session_id)
                raise HTTPException(503, "the agent could not finish; start a new session")
        except asyncio.TimeoutError:
            await self.close(session_id)
            raise HTTPException(504, "the agent timed out; start a new session") from None
        except asyncio.CancelledError:
            await self.close(session_id)
            raise
        except HTTPException:
            raise
        except Exception:
            log.exception("session message failed")
            await self.close(session_id)
            raise HTTPException(503, "the agent could not finish; start a new session") from None
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
            try:
                await self.close(session_id)
            except Exception:
                log.exception("session shutdown failed")


_manager = None
_reaper = None


def manager() -> SessionManager:
    global _manager
    if _manager is None:
        try:
            _manager = SessionManager()
        except Exception:
            log.exception("session backend unavailable")
            raise HTTPException(503, "the session backend is unavailable; check the configured model and executable") from None
    return _manager


async def startup():
    global _reaper

    async def reap():
        while True:
            await asyncio.sleep(min(30, max(0.5, positive_int("SESSION_IDLE_S", 900) / 2)))
            if _manager is not None:
                try:
                    await _manager.expire()
                except Exception:
                    log.exception("session expiry failed")

    _reaper = asyncio.create_task(reap())


async def shutdown():
    global _reaper, _manager
    if _reaper is not None:
        _reaper.cancel()
        try:
            await _reaper
        except asyncio.CancelledError:
            pass
        _reaper = None
    if _manager is not None:
        await _manager.shutdown()
        _manager = None


# /api/v1 belongs to the read API (docs/rest.md), served by its own process behind the same host.
router = APIRouter(prefix="/opencode-wrapper/api")


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
