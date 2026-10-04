"""Local OpenCode HTTP adapter for the integrated synthetic KYC runtime.

Run with ``uv run python -m intercept.service.local --help``. The trusted operator pins
one application and contract; model requests cannot select a bank or authority.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sqlite3

from contracts import ActionProposal
from intercept.policy.runs import IDENTIFIER, Policy
from intercept.service.server import Gateway
from simulation.governed import GovernedRuntime, POLICY_PATH
from tracing import get_logger


class LocalService:
    def __init__(self, *, bank_path, runs_dir, app_id, contract_id, policy_path=POLICY_PATH, catalog_all=False,
                 config_service=None, policy_revision=None):
        # Import the simulation's existing registry through its established path.
        from simulation import agent
        self.registry = agent.registry
        self.bank_path = Path(bank_path).resolve()
        self.runs_dir = Path(runs_dir).resolve()
        self.app_id, self.contract_id = app_id, contract_id
        self.config = json.loads(Path(policy_path).read_text())
        self.config_service = config_service
        self.policy_revision = policy_revision
        self.catalog_all = catalog_all
        if config_service is not None:
            self.config = config_service.snapshot_for_intercept()["config"]
        # Free-agent mode advertises every tool; the gateway still decides each call.
        self.catalog_names = list(self.registry.REGISTRY) if catalog_all else self.config["allowed_tools"]
        self.runtime = None
        self.lock = asyncio.Lock()
        if not self.bank_path.is_file() or not IDENTIFIER.fullmatch(contract_id):
            raise ValueError("trusted synthetic bank and contract ID required")

    async def handle_request(self, path, payload):
        async with self.lock:
            if path == "/v1/tools/catalog":
                if payload != {}:
                    raise ValueError("invalid catalog request")
                tools = self.registry.openai_tools(self.catalog_names)
                return {"tools": [{"name": t["function"]["name"], "description": t["function"]["description"],
                                   "input": t["function"]["parameters"]} for t in tools]}
            if path == "/v1/runs/bind":
                if not isinstance(payload, dict) or set(payload) != {"session_id", "contract_id"}:
                    raise ValueError("invalid binding")
                session_id = payload["session_id"]
                if (payload["contract_id"] != self.contract_id or not isinstance(session_id, str)
                        or not IDENTIFIER.fullmatch(session_id) or not session_id.startswith("ses")):
                    raise ValueError("binding conflicts with trusted deployment")
                if self.runtime is not None:
                    if self.runtime.contract.session_id != session_id:
                        raise ValueError("this demo service is bound to another session")
                else:
                    # Select once at trusted binding, not per tool call. Never change a live contract.
                    policy_revision = None
                    if self.config_service is not None:
                        snapshot = await asyncio.to_thread(self.config_service.snapshot_for_intercept)
                        self.config = snapshot["config"]
                        policy_revision = snapshot["revision"].removeprefix("sha256:")
                        if not self.catalog_all:
                            self.catalog_names = self.config["allowed_tools"]
                    self.runs_dir.mkdir(parents=True, exist_ok=True)
                    bank_copy = self.runs_dir / f"{session_id}.bank.db"
                    if bank_copy.exists():
                        raise ValueError("run copy already exists; choose a new session")
                    with sqlite3.connect(f"{self.bank_path.as_uri()}?mode=ro", uri=True) as source:
                        with sqlite3.connect(bank_copy) as dest:
                            source.backup(dest)
                    ctx = self.registry.Ctx(agent="onboarding-agent", session_id=session_id, db=bank_copy)
                    self.runtime = await GovernedRuntime.create(ctx, self.app_id, policy_config=self.config,
                                                                contract_id=self.contract_id,
                                                                policy_revision=policy_revision or self.policy_revision)
                    get_logger().log("intercept", "session.bound", session=session_id, contract=self.contract_id,
                                     case=self.app_id)
                return {"session_id": session_id, "contract_id": self.contract_id,
                        "policy_version": self.runtime.contract.policy_version}
            if self.runtime is None:
                raise ValueError("no trusted session is bound")
            if path == "/v1/tools/execute":
                Policy.validate_action(payload)
                bound = self.runtime.contract
                if payload["session_id"] != bound.session_id:
                    raise ValueError("session hint conflicts with authenticated binding")
                spec = self.registry.REGISTRY.get(payload["tool"])
                digest = hashlib.sha256(f"{bound.session_id}|{payload['call_id']}".encode()).hexdigest()
                proposal = ActionProposal(action_id=f"act_{digest[:32]}", session_id=bound.session_id,
                    agent_id=bound.agent_id, tool=payload["tool"], arguments=payload["arguments"],
                    side_effect=spec.side_effect if spec else "read", transport="inproc")
                decision, result = await self.runtime.gateway.execute(proposal)
                await self.runtime.settle()
                response = decision.to_dict()
                response.update(session_id=bound.session_id, call_id=payload["call_id"], tool=payload["tool"],
                                tool_result=result, verification="NOT_VERIFIED")
                return response
            if path == "/v1/prompts/evaluate":
                return await self._evaluate_prompt(payload)
            if path == "/v1/session/finish":
                if payload != {"session_id": self.runtime.contract.session_id}:
                    raise ValueError("finish conflicts with trusted binding")
                result = await self.runtime._finish()
                get_logger().log("intercept", "session.verified", session=self.runtime.contract.session_id,
                                 status=result.verification_status)
                return result.to_dict()
            # This integrated path executes tools inside the trusted gateway.
            # Legacy before/after observation mode remains the separate service.
            raise ValueError("integrated service requires gateway-backed tool execution")

    async def _evaluate_prompt(self, payload):
        """Judge one OpenCode model request with the runtime's PromptGateway (adapter prompts: "enforce").

        OpenCode calls the provider itself, so the gateway's backend dispatches nothing: the gateway still
        applies session admission, the model allowlist, the token budget and the input scanners, and
        records the decision as evidence. A user prompt is judged as part of the next model request, which
        carries it, so the prompt hook is admitted here without a second budget charge.
        """
        if not isinstance(payload, dict) or not isinstance(payload.get("request_id"), str):
            raise ValueError("invalid prompt request")
        contract = self.runtime.contract
        if payload.get("session_id") != contract.session_id:
            raise ValueError("session hint conflicts with authenticated binding")
        reply = {"session_id": contract.session_id, "request_id": payload["request_id"],
                 "policy_version": contract.policy_version}
        if payload.get("action_type") == "prompt":
            return {**reply, "decision": "ALLOW"}
        if payload.get("action_type") != "llm_request":
            raise ValueError("invalid prompt request")
        model = payload.get("model")
        model = model.get("id") if isinstance(model, dict) else None
        messages = payload.get("messages")
        tools = payload.get("tools")
        if not isinstance(messages, list) or not isinstance(tools, list):
            raise ValueError("invalid prompt request")

        async def admitted(*_):  # the provider call happens in OpenCode once this request is allowed
            return {"message": {"role": "assistant", "content": ""}}

        decision, _ = await self.runtime.prompt_gateway.execute(model, messages, tools, admitted)
        await self.runtime.settle()
        # OpenCode sends its own copy of the request, so a redaction cannot be applied there: fail closed.
        verdict = "ALLOW" if decision.decision in ("ALLOW", "ALERT") else decision.decision
        return {**reply, "decision": verdict, "reason_code": decision.reason_code}

    async def close(self):
        if self.runtime:
            await self.runtime.aclose()


async def serve(args):
    from configuration.service import ConfigService
    service = LocalService(bank_path=args.bank_db, runs_dir=args.runs_dir, app_id=args.application,
                           contract_id=args.contract_id, policy_path=args.policy or POLICY_PATH,
                           catalog_all=args.catalog_all,
                           config_service=None if args.policy else ConfigService(args.config_dir),
                           policy_revision=args.policy_revision)
    gateway = Gateway(None, os.environ.get("INTERCEPT_TOKEN", ""), None,
                      admin_token=os.environ.get("INTERCEPT_ADMIN_TOKEN"), service=service)
    server = await asyncio.start_server(gateway.handle, "127.0.0.1", args.port, limit=8192)
    try:
        async with server:
            await server.serve_forever()
    finally:
        await service.close()


def main():
    parser = argparse.ArgumentParser(description="Integrated local synthetic KYC gateway for OpenCode")
    parser.add_argument("--bank-db", required=True, type=Path)
    parser.add_argument("--application", required=True)
    parser.add_argument("--contract-id", required=True)
    parser.add_argument("--runs-dir", type=Path, default=Path("data/runs"))
    config_source = parser.add_mutually_exclusive_group()
    config_source.add_argument("--policy", type=Path, help="explicit legacy policy; bypass selected backend config")
    config_source.add_argument("--config-dir", type=Path, help="shared backend config directory (default: CONFIG_DIR)")
    parser.add_argument("--policy-revision", help="trusted revision for an explicit policy snapshot")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--catalog-all", action="store_true",
                        help="advertise every registered tool (free-agent demo); policy still decides each call")
    asyncio.run(serve(parser.parse_args()))


if __name__ == "__main__":
    main()
