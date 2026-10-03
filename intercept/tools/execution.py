"""Opt-in controlled execution over the synthetic banking tool registry."""
import asyncio
import json
from pathlib import Path
import sys


class ToolExecutor:
    def __init__(self, database, agent="onboarding-agent"):
        self.database = str(Path(database).resolve())
        self.agent = agent  # Operator-configured; never supplied by the model.
        if agent not in ("onboarding-agent", "admin-agent"):
            raise ValueError("unsupported simulation identity")
        if not Path(self.database).is_file():
            raise ValueError("synthetic bank database must exist")

    async def execute(self, action):
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "intercept.tools.worker",
            cwd=str(Path(__file__).resolve().parents[2]), env={},
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            body = {"tool": action["tool"], "arguments": action["arguments"],
                    "session_id": action["session_id"], "agent": self.agent,
                    "database": self.database}
            stdout, _ = await process.communicate(json.dumps(body).encode())
            if process.returncode != 0 or len(stdout) > 65536:
                raise RuntimeError("tool execution outcome unknown")
            result = json.loads(stdout)
            if not isinstance(result, dict):
                raise RuntimeError("invalid tool execution response")
            return result
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    async def catalog(self, tools):
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "intercept.tools.worker", cwd=str(Path(__file__).resolve().parents[2]),
            env={}, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            body = json.dumps({"operation": "catalog", "tools": tools, "agent": self.agent}).encode()
            stdout, _ = await asyncio.wait_for(process.communicate(body), 2)
            if process.returncode != 0 or len(stdout) > 65536:
                raise RuntimeError("tool catalog unavailable")
            result = json.loads(stdout)
            if not isinstance(result, list):
                raise RuntimeError("invalid tool catalog")
            return result
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
